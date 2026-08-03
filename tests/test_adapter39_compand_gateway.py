from __future__ import annotations

import asyncio
import http.client
import json
import secrets
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import httpx
import uvicorn
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401 - adds src/ to sys.path
from switchboard.application.commands.compand_gateway import (
    GatewayPolicy,
    GatewayRejection,
    GatewayRequest,
    plan_gateway_request,
)
from switchboard.domain.compand import (
    ClientCredentialRegistry,
    GatewayMode,
    GatewaySecurityError,
)
from switchboard.services.compand.app import create_app
from switchboard.services.compand.settings import CompandGatewaySettings


FIXTURES = ROOT / "fixtures/compand/openai-responses/codex-cli-0.144.5"
EXCHANGES = json.loads((FIXTURES / "wire/exchanges.json").read_text(encoding="utf-8"))
CLIENT_TOKEN = "compand-client-secret"
UPSTREAM_TOKEN = "openai-upstream-secret"
FROZEN_USER_AGENT = (
    "codex_exec/0.144.5 (Mac OS 26.3.0; arm64) "
    "dumb (codex_exec; 0.144.5)"
)


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


class FailingFixtureStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self.prefix = prefix
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        raise httpx.ReadError("upstream read failed after partial response")

    async def aclose(self) -> None:
        self.closed = True


def upstream_response(
    status_code: int = 200,
    *,
    headers: Any = None,
    body: bytes = b"",
) -> httpx.Response:
    return httpx.Response(status_code, headers=headers, stream=FixtureStream(body))


def fixture_bytes(relative_path: str) -> bytes:
    return (FIXTURES / relative_path).read_bytes()


def settings(
    *,
    mode: GatewayMode = GatewayMode.PASSTHROUGH,
    upstream_key: str = UPSTREAM_TOKEN,
    max_request_bytes: int = 16 * 1024 * 1024,
    frozen_tuple_config_attested: bool = True,
) -> CompandGatewaySettings:
    return CompandGatewaySettings(
        upstream_origin="https://api.openai.com",
        upstream_api_key=upstream_key,
        client_credentials={"tenant-a": CLIENT_TOKEN},
        mode=mode,
        max_request_bytes=max_request_bytes,
        source_version="adapter39-test",
        frozen_tuple_config_attested=frozen_tuple_config_attested,
    )


def request_headers(exchange: dict[str, Any]) -> dict[str, str]:
    headers = {name: value for name, value in exchange["request"]["selected_headers"]}
    headers["authorization"] = f"Bearer {CLIENT_TOKEN}"
    return headers


@contextmanager
def running_http_server(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def running_gateway(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        raise RuntimeError("uvicorn gateway did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        sock.close()


def socket_exchange(
    port: int,
    method: str,
    path_query: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, http.client.HTTPMessage, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path_query, body=body, headers=headers)
    response = connection.getresponse()
    result = (response.status, response.headers, response.read())
    connection.close()
    return result


class Adapter39CompandGatewayTest(unittest.TestCase):
    def test_all_proto9_exchanges_preserve_bytes_headers_status_and_sse_order(
        self,
    ) -> None:
        exchange_by_path = {item["request"]["path_query"]: item for item in EXCHANGES}
        upstream_captures = []

        class FixtureUpstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                exchange = exchange_by_path[self.path]
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                upstream_captures.append(
                    {
                        "method": self.command,
                        "path_query": self.path,
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                response = exchange["response"]
                response_body = fixture_bytes(response["body_file"])
                self.send_response_only(response["status"])
                for name, value in response["selected_headers"]:
                    self.send_header(name, value)
                self.send_header("content-length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
                self.close_connection = True

            def log_message(self, _format: str, *args: Any) -> None:
                return None

        telemetry = []
        coverage = []
        egress = []
        with running_http_server(FixtureUpstream) as upstream_port:
            cfg = CompandGatewaySettings(
                upstream_origin=f"http://127.0.0.1:{upstream_port}",
                upstream_api_key=UPSTREAM_TOKEN,
                client_credentials={"tenant-a": CLIENT_TOKEN},
                source_version="adapter39-test",
                allow_http_loopback=True,
                frozen_tuple_config_attested=True,
            )
            app = create_app(
                cfg,
                telemetry_sink=telemetry.append,
                coverage_sink=coverage.append,
                egress_observer=egress.append,
            )
            with running_gateway(app) as gateway_port:
                for exchange in EXCHANGES:
                    with self.subTest(exchange=exchange["exchange_id"]):
                        expected_request = exchange["request"]
                        expected_response = exchange["response"]
                        request_body = fixture_bytes(expected_request["body_file"])
                        response_body = fixture_bytes(expected_response["body_file"])
                        selected = dict(expected_request["selected_headers"])

                        direct_status, direct_headers, direct_body = socket_exchange(
                            upstream_port,
                            expected_request["method"],
                            expected_request["path_query"],
                            request_body,
                            {**selected, "authorization": f"Bearer {UPSTREAM_TOKEN}"},
                        )
                        gateway_status, gateway_headers, gateway_body = socket_exchange(
                            gateway_port,
                            expected_request["method"],
                            expected_request["path_query"],
                            request_body,
                            {**selected, "authorization": f"Bearer {CLIENT_TOKEN}"},
                        )

                        self.assertEqual(gateway_status, direct_status)
                        self.assertEqual(gateway_status, expected_response["status"])
                        self.assertEqual(gateway_body, direct_body)
                        self.assertEqual(gateway_body, response_body)
                        for name, value in expected_response["selected_headers"]:
                            self.assertEqual(direct_headers[name], value)
                            self.assertEqual(gateway_headers[name], value)

                        direct_capture, gateway_capture = upstream_captures[-2:]
                        self.assertEqual(
                            gateway_capture["method"], direct_capture["method"]
                        )
                        self.assertEqual(
                            gateway_capture["path_query"], direct_capture["path_query"]
                        )
                        self.assertEqual(
                            gateway_capture["body"], direct_capture["body"]
                        )
                        self.assertEqual(gateway_capture["body"], request_body)
                        for name, value in expected_request["selected_headers"]:
                            self.assertEqual(gateway_capture["headers"][name], value)
                        self.assertEqual(
                            gateway_capture["headers"]["authorization"],
                            f"Bearer {UPSTREAM_TOKEN}",
                        )
                        self.assertNotIn(CLIENT_TOKEN, str(gateway_capture))

        self.assertEqual(len(telemetry), len(EXCHANGES))
        self.assertEqual(len(coverage), len(EXCHANGES))
        self.assertEqual(len(egress), len(EXCHANGES))

    def test_shadow_scan_observes_shape_without_mutating_or_retaining_content(
        self,
    ) -> None:
        marker = "private-prompt-marker"
        tool_marker = "private-tool-output-marker"
        body = json.dumps(
            {
                "model": "gpt-5.4",
                "input": [{"role": "user", "content": marker}, {"output": tool_marker}],
                "tools": [{"type": "function", "name": "shell"}],
                "stream": True,
                "store": False,
                "reasoning": {"effort": "high"},
            },
            separators=(",", ":"),
        ).encode()
        captured: dict[str, bytes] = {}

        def upstream(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return upstream_response(
                200, headers={"content-type": "application/json"}, body=b"{}"
            )

        telemetry = []
        coverage = []
        egress = []
        cfg = settings(mode=GatewayMode.SCAN)
        app = create_app(
            cfg,
            transport=httpx.MockTransport(upstream),
            telemetry_sink=telemetry.append,
            coverage_sink=coverage.append,
            egress_observer=egress.append,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                content=body,
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "accept": "text/event-stream",
                    "content-type": "application/json",
                    "user-agent": FROZEN_USER_AGENT,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["body"], body)
        self.assertEqual(telemetry[0].scan.input_item_count, 2)
        self.assertEqual(telemetry[0].scan.tool_count, 1)
        self.assertTrue(telemetry[0].scan.stream_requested)
        self.assertEqual(coverage[0].client_version, "0.144.5")
        self.assertEqual(coverage[0].tuple_status, "certified")
        serialized = json.dumps(
            {
                "telemetry": [item.model_dump(by_alias=True) for item in telemetry],
                "coverage": [item.model_dump(by_alias=True) for item in coverage],
                "egress": [item.model_dump(by_alias=True) for item in egress],
                "settings_repr": repr(cfg),
            },
            sort_keys=True,
        )
        for secret in (marker, tool_marker, CLIENT_TOKEN, UPSTREAM_TOKEN):
            self.assertNotIn(secret, serialized)

    def test_frozen_tuple_classifier_fails_unknown_for_any_unverifiable_change(
        self,
    ) -> None:
        exact_body = {
            "model": "gpt-5.4",
            "store": False,
            "stream": True,
            "reasoning": {"effort": "high"},
        }
        exact_headers = (
            (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
            (b"accept", b"text/event-stream"),
            (b"content-type", b"application/json"),
            (b"user-agent", FROZEN_USER_AGENT.encode()),
        )

        def classify(
            *,
            body: object = exact_body,
            headers: tuple[tuple[bytes, bytes], ...] = exact_headers,
            attested: bool = True,
            query: bytes = b"",
        ) -> str:
            encoded = (
                json.dumps(body, separators=(",", ":")).encode()
                if not isinstance(body, bytes)
                else body
            )
            plan = plan_gateway_request(
                GatewayRequest(
                    method="POST",
                    path="/v1/responses",
                    query=query,
                    headers=headers,
                    body=encoded,
                ),
                policy=GatewayPolicy(
                    mode=GatewayMode.PASSTHROUGH,
                    frozen_tuple_config_attested=attested,
                ),
                credentials=ClientCredentialRegistry({"tenant-a": CLIENT_TOKEN}),
            )
            return plan.tuple_status

        self.assertEqual(classify(), "certified")
        self.assertEqual(classify(body={**exact_body, "model": "gpt-5.5"}), "unknown")
        wrong_platform = tuple(
            (name, b"codex_exec/0.144.5 (Windows 11; x86_64)")
            if name == b"user-agent"
            else (name, value)
            for name, value in exact_headers
        )
        self.assertEqual(classify(headers=wrong_platform), "unknown")
        spoofed_prefix = tuple(
            (name, b"other/1 codex_exec/0.144.5 (Mac OS 26.3.0; arm64)")
            if name == b"user-agent"
            else (name, value)
            for name, value in exact_headers
        )
        self.assertEqual(classify(headers=spoofed_prefix), "unknown")
        opaque_headers = tuple(
            (name, b"application/octet-stream")
            if name == b"content-type"
            else (name, value)
            for name, value in exact_headers
        )
        self.assertEqual(classify(body=b"opaque", headers=opaque_headers), "unknown")
        self.assertEqual(classify(query=b"feature=changed"), "unknown")
        self.assertEqual(classify(attested=False), "unknown")

    def test_duplicate_tuple_fields_are_unknown_and_pass_through_unchanged(
        self,
    ) -> None:
        bodies = [
            b'{"model":"gpt-5.5","model":"gpt-5.4","store":false,'
            b'"stream":true,"reasoning":{"effort":"high"}}',
            b'{"model":"gpt-5.4","store":true,"store":false,'
            b'"stream":true,"reasoning":{"effort":"high"}}',
            b'{"model":"gpt-5.4","store":false,"stream":false,"stream":true,'
            b'"reasoning":{"effort":"high"}}',
            b'{"model":"gpt-5.4","store":false,"stream":true,'
            b'"reasoning":{"effort":"low"},"reasoning":{"effort":"high"}}',
            b'{"model":"gpt-5.4","store":false,"stream":true,'
            b'"reasoning":{"effort":"low","effort":"high"}}',
        ]
        captured = []
        coverage = []

        def upstream(request: httpx.Request) -> httpx.Response:
            captured.append(request.content)
            return upstream_response(200, body=b"{}")

        app = create_app(
            settings(),
            transport=httpx.MockTransport(upstream),
            coverage_sink=coverage.append,
        )
        with TestClient(app) as client:
            for body in bodies:
                with self.subTest(body=body):
                    response = client.post(
                        "/v1/responses",
                        content=body,
                        headers={
                            "authorization": f"Bearer {CLIENT_TOKEN}",
                            "accept": "text/event-stream",
                            "content-type": "application/json",
                            "user-agent": FROZEN_USER_AGENT,
                        },
                    )
                    self.assertEqual(response.status_code, 200)

        self.assertEqual(captured, bodies)
        self.assertEqual(
            [receipt.tuple_status for receipt in coverage],
            ["unknown"] * len(bodies),
        )

    def test_client_and_upstream_credentials_are_separate_and_revocable(self) -> None:
        calls = []

        def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            return upstream_response(200, body=b"ok")

        registry = ClientCredentialRegistry({"tenant-a": CLIENT_TOKEN})
        app = create_app(
            settings(), credentials=registry, transport=httpx.MockTransport(upstream)
        )
        with TestClient(app) as client:
            accepted = client.post(
                "/v1/responses",
                content=b"{}",
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
            )
            self.assertTrue(registry.revoke("tenant-a"))
            revoked = client.post(
                "/v1/responses",
                content=b"{}",
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(calls, [f"Bearer {UPSTREAM_TOKEN}"])
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(
            revoked.json()["error"]["classification"], "client_credential_revoked"
        )
        self.assertNotIn(CLIENT_TOKEN, revoked.text)

    def test_authentication_and_revocation_are_linearizable(self) -> None:
        registry = ClientCredentialRegistry({"tenant-a": CLIENT_TOKEN})
        comparison_barrier = threading.Barrier(2)
        allow_comparison = threading.Event()
        revoke_started = threading.Event()
        revoke_returned = threading.Event()
        result = []
        original_compare_digest = secrets.compare_digest

        def blocked_compare_digest(left: bytes, right: bytes) -> bool:
            comparison_barrier.wait(timeout=5)
            allow_comparison.wait(timeout=5)
            return original_compare_digest(left, right)

        def authenticate() -> None:
            result.append(registry.authenticate(CLIENT_TOKEN))

        def revoke() -> None:
            revoke_started.set()
            registry.revoke("tenant-a")
            revoke_returned.set()

        with patch(
            "switchboard.domain.compand.gateway.secrets.compare_digest",
            side_effect=blocked_compare_digest,
        ):
            authentication_thread = threading.Thread(target=authenticate)
            revocation_thread = threading.Thread(target=revoke)
            authentication_thread.start()
            comparison_barrier.wait(timeout=5)
            revocation_thread.start()
            self.assertTrue(revoke_started.wait(timeout=5))
            self.assertFalse(revoke_returned.wait(timeout=0.05))
            allow_comparison.set()
            authentication_thread.join(timeout=5)
            revocation_thread.join(timeout=5)

        self.assertFalse(authentication_thread.is_alive())
        self.assertFalse(revocation_thread.is_alive())
        self.assertTrue(result[0].accepted)
        self.assertTrue(revoke_returned.is_set())
        self.assertFalse(registry.authenticate(CLIENT_TOKEN).accepted)

    def test_duplicate_client_tokens_fail_closed_before_revocation_is_ambiguous(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            GatewaySecurityError, "client credential tokens must be unique"
        ):
            ClientCredentialRegistry(
                {"tenant-a": CLIENT_TOKEN, "tenant-b": CLIENT_TOKEN},
                revoked_ids={"tenant-a"},
            )

    def test_environment_credentials_reject_non_string_values_and_bearer_none(
        self,
    ) -> None:
        invalid_values = (None, True, 7, [], {}, "")
        for invalid_value in invalid_values:
            with (
                self.subTest(value=invalid_value),
                patch.dict(
                    "os.environ",
                    {
                        "COMPAND_CLIENT_CREDENTIALS_JSON": json.dumps(
                            {"tenant": invalid_value}
                        )
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "values must be non-empty strings"),
            ):
                CompandGatewaySettings.from_env()

        request = GatewayRequest(
            method="POST",
            path="/v1/responses",
            query=b"",
            headers=(
                (b"authorization", b"Bearer None"),
                (b"content-type", b"application/json"),
            ),
            body=b"{}",
        )
        with self.assertRaises(GatewayRejection) as caught:
            plan_gateway_request(
                request,
                policy=GatewayPolicy(mode=GatewayMode.PASSTHROUGH),
                credentials=ClientCredentialRegistry({"tenant-a": CLIENT_TOKEN}),
            )
        self.assertEqual(caught.exception.classification, "client_auth_failed")

    def test_programmatic_non_string_credentials_fail_closed_through_create_app(
        self,
    ) -> None:
        invalid_tokens = (True, 7, ["token"], object())
        for invalid_token in invalid_tokens:
            cfg = CompandGatewaySettings(
                upstream_api_key=UPSTREAM_TOKEN,
                client_credentials={"tenant-a": invalid_token},
            )
            with (
                self.subTest(token_type=type(invalid_token).__name__),
                self.assertRaisesRegex(
                    GatewaySecurityError,
                    "client credentials require non-empty string ids and tokens",
                ),
            ):
                create_app(cfg)

        for invalid_upstream in (True, 7, ["token"], object()):
            cfg = CompandGatewaySettings(
                upstream_api_key=invalid_upstream,
                client_credentials={"tenant-a": CLIENT_TOKEN},
            )
            with (
                self.subTest(upstream_type=type(invalid_upstream).__name__),
                self.assertRaisesRegex(
                    GatewaySecurityError,
                    "upstream OpenAI credential must be a string",
                ),
            ):
                create_app(cfg)

        invalid_ids = (True, 7, object(), "   ")
        for invalid_id in invalid_ids:
            cfg = CompandGatewaySettings(
                upstream_api_key=UPSTREAM_TOKEN,
                client_credentials={invalid_id: CLIENT_TOKEN},
            )
            with (
                self.subTest(credential_id_type=type(invalid_id).__name__),
                self.assertRaisesRegex(
                    GatewaySecurityError,
                    "client credentials require non-empty string ids and tokens",
                ),
            ):
                create_app(cfg)

    def test_create_app_rejects_client_upstream_overlap_before_transport(self) -> None:
        calls = []

        def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return upstream_response(200, body=b"unexpected")

        transport = httpx.MockTransport(upstream)
        with self.assertRaisesRegex(
            GatewaySecurityError,
            "client and upstream credentials must be separate identities",
        ):
            create_app(settings(upstream_key=CLIENT_TOKEN), transport=transport)

        injected = ClientCredentialRegistry({"tenant-injected": UPSTREAM_TOKEN})
        with self.assertRaisesRegex(
            GatewaySecurityError,
            "client and upstream credentials must be separate identities",
        ):
            create_app(settings(), credentials=injected, transport=transport)
        self.assertEqual(calls, [])

    def test_auth_malformed_size_route_and_missing_upstream_fail_before_transport(
        self,
    ) -> None:
        calls = []

        def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return upstream_response(200, body=b"unexpected")

        cases = [
            (
                settings(),
                "/v1/responses",
                b"{}",
                {"content-type": "application/json"},
                401,
                "client_auth_failed",
            ),
            (
                settings(),
                "/v1/responses",
                b"{",
                {
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
                400,
                "malformed_json",
            ),
            (
                settings(max_request_bytes=2),
                "/v1/responses",
                b"{}x",
                {
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/octet-stream",
                },
                413,
                "request_size_policy_failed",
            ),
            (
                settings(),
                "/v1/chat/completions",
                b"{}",
                {
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
                404,
                "unsupported_route",
            ),
            (
                settings(upstream_key=""),
                "/v1/responses",
                b"{}",
                {
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
                503,
                "upstream_credential_unavailable",
            ),
        ]
        for cfg, path, body, headers, status, classification in cases:
            with self.subTest(classification=classification):
                app = create_app(cfg, transport=httpx.MockTransport(upstream))
                with TestClient(app) as client:
                    response = client.post(path, content=body, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertEqual(
                    response.json()["error"]["classification"], classification
                )
        self.assertEqual(calls, [])

    def test_percent_encoded_route_alias_is_rejected_before_transport(self) -> None:
        calls = []

        def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return upstream_response(200, body=b"unexpected")

        app = create_app(settings(), transport=httpx.MockTransport(upstream))
        with TestClient(app) as client:
            response = client.post(
                "/v1/%72esponses",
                content=b"{}",
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["classification"], "noncanonical_path")
        self.assertEqual(calls, [])

    def test_ambiguous_auth_and_content_length_are_explicit_admission_failures(
        self,
    ) -> None:
        registry = ClientCredentialRegistry({"tenant-a": CLIENT_TOKEN})
        base_headers = ((b"content-type", b"application/json"),)
        requests = [
            GatewayRequest(
                method="POST",
                path="/v1/responses",
                query=b"",
                headers=base_headers
                + (
                    (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
                    (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
                ),
                body=b"{}",
            ),
            GatewayRequest(
                method="POST",
                path="/v1/responses",
                query=b"",
                headers=base_headers
                + (
                    (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
                    (b"content-length", b"99"),
                ),
                body=b"{}",
            ),
            GatewayRequest(
                method="POST",
                path="/v1/responses",
                query=b"",
                headers=base_headers
                + (
                    (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
                    (b"proxy-authorization", b"Basic opaque"),
                ),
                body=b"{}",
            ),
        ]
        expected = [
            "ambiguous_client_auth",
            "content_length_mismatch",
            "security_policy_failed",
        ]
        for request, classification in zip(requests, expected, strict=True):
            with (
                self.subTest(classification=classification),
                self.assertRaises(GatewayRejection) as caught,
            ):
                plan_gateway_request(
                    request,
                    policy=GatewayPolicy(mode=GatewayMode.PASSTHROUGH),
                    credentials=registry,
                )
            self.assertEqual(caught.exception.classification, classification)

    def test_transport_failure_preserves_cause_class_and_redacts_credentials(
        self,
    ) -> None:
        def upstream(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"dial failed with {CLIENT_TOKEN} and {UPSTREAM_TOKEN}", request=request
            )

        telemetry = []
        app = create_app(
            settings(),
            transport=httpx.MockTransport(upstream),
            telemetry_sink=telemetry.append,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                content=b"{}",
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                },
            )
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["classification"], "upstream_transport_failed")
        self.assertIn("ConnectError", error["cause"])
        self.assertIn("[REDACTED]", error["cause"])
        self.assertNotIn(CLIENT_TOKEN, response.text)
        self.assertNotIn(UPSTREAM_TOKEN, response.text)
        self.assertEqual(telemetry[0].outcome, "transport_failed")

    def test_midstream_read_failure_preserves_partial_body_and_failure_telemetry(
        self,
    ) -> None:
        prefix = b'data: {"type":"response.output_text.delta"}\n\n'
        failing_stream = FailingFixtureStream(prefix)

        def upstream(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=failing_stream,
            )

        telemetry = []
        coverage = []
        app = create_app(
            settings(),
            transport=httpx.MockTransport(upstream),
            telemetry_sink=telemetry.append,
            coverage_sink=coverage.append,
        )

        async def invoke_gateway() -> list[dict[str, Any]]:
            messages: list[dict[str, Any]] = []

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                messages.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/responses",
                "raw_path": b"/v1/responses",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", b"2"),
                ],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "root_path": "",
                "state": {},
            }
            try:
                with self.assertRaises(httpx.ReadError):
                    await app(scope, receive, send)
            finally:
                await app.state.compand_gateway_runtime.http_client.aclose()
            return messages

        messages = asyncio.run(invoke_gateway())
        body_messages = [
            message for message in messages if message["type"] == "http.response.body"
        ]
        self.assertEqual(b"".join(message["body"] for message in body_messages), prefix)
        self.assertTrue(all(message["more_body"] for message in body_messages))
        self.assertTrue(failing_stream.closed)
        self.assertEqual(len(coverage), 1)
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0].outcome, "transport_failed")
        self.assertEqual(
            telemetry[0].classification, "upstream_stream_read_failed"
        )
        self.assertEqual(telemetry[0].response_bytes, len(prefix))

    def test_upstream_origin_security_boundary_rejects_routing_and_embedded_secrets(
        self,
    ) -> None:
        invalid = (
            "http://api.openai.com",
            "https://example.com",
            "https://user:secret@api.openai.com",
            "https://api.openai.com/v1",
            "https://api.openai.com?target=other",
        )
        for origin in invalid:
            with self.subTest(origin=origin), self.assertRaises(GatewaySecurityError):
                create_app(
                    CompandGatewaySettings(
                        upstream_origin=origin,
                        upstream_api_key=UPSTREAM_TOKEN,
                        client_credentials={"tenant-a": CLIENT_TOKEN},
                    )
                )

    def test_unsupported_binary_shape_is_unchanged_passthrough(self) -> None:
        body = b"\x00\xffopaque-signed-or-unknown"
        captured = []

        def upstream(request: httpx.Request) -> httpx.Response:
            captured.append(request.content)
            return upstream_response(200, body=body)

        app = create_app(settings(), transport=httpx.MockTransport(upstream))
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                content=body,
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/octet-stream",
                },
            )
        self.assertEqual(captured, [body])
        self.assertEqual(response.content, body)

    def test_models_control_query_is_forwarded_and_excluded_from_inference_coverage(
        self,
    ) -> None:
        capture = {}
        coverage = []
        egress = []

        def upstream(request: httpx.Request) -> httpx.Response:
            capture["url"] = str(request.url)
            capture["authorization"] = request.headers["authorization"]
            return upstream_response(
                200,
                headers={
                    "content-type": "application/json",
                    "x-request-id": "req_models",
                },
                body=b'{"object":"list","data":[]}',
            )

        app = create_app(
            settings(),
            transport=httpx.MockTransport(upstream),
            coverage_sink=coverage.append,
            egress_observer=egress.append,
        )
        with TestClient(app) as client:
            response = client.get(
                "/v1/models?client_version=0.144.5",
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "user-agent": FROZEN_USER_AGENT,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            capture["url"], "https://api.openai.com/v1/models?client_version=0.144.5"
        )
        self.assertEqual(capture["authorization"], f"Bearer {UPSTREAM_TOKEN}")
        self.assertEqual(coverage[0].certified_feature, "models")
        self.assertEqual(coverage[0].tuple_status, "certified")
        self.assertEqual(egress[0].classification, "excluded")

    def test_continuation_and_retry_bodies_remain_frozen_across_one_gateway_process(
        self,
    ) -> None:
        relative_paths = [
            "requests/normal.json",
            "requests/tool-output.json",
            "requests/manual-continuation.json",
            "requests/previous-response.json",
            "requests/conversation.json",
            "requests/retry.json",
            "requests/retry.json",
        ]
        observed = []

        def upstream(request: httpx.Request) -> httpx.Response:
            observed.append(request.content)
            return upstream_response(200, body=b"{}")

        app = create_app(settings(), transport=httpx.MockTransport(upstream))
        with TestClient(app) as client:
            for index, relative_path in enumerate(relative_paths):
                response = client.post(
                    f"/v1/responses?attempt={index}",
                    content=fixture_bytes(relative_path),
                    headers={
                        "authorization": f"Bearer {CLIENT_TOKEN}",
                        "content-type": "application/json",
                    },
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [fixture_bytes(path) for path in relative_paths])

    def test_encoded_json_is_an_unsupported_unchanged_shape_not_parser_input(
        self,
    ) -> None:
        body = b"not-cleartext-json"
        observed = []
        telemetry = []

        def upstream(request: httpx.Request) -> httpx.Response:
            observed.append(request.content)
            return upstream_response(200, body=b"{}")

        app = create_app(
            settings(mode=GatewayMode.SCAN),
            transport=httpx.MockTransport(upstream),
            telemetry_sink=telemetry.append,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                content=body,
                headers={
                    "authorization": f"Bearer {CLIENT_TOKEN}",
                    "content-type": "application/json",
                    "content-encoding": "fixture-unknown",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [body])
        self.assertEqual(telemetry[0].scan.json_kind, "not_json")


if __name__ == "__main__":
    unittest.main()
