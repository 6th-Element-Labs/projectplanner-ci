from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from path_setup import ROOT


FIXTURES = ROOT / "fixtures/compand/openai-responses/codex-cli-0.144.5"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
WIRE_EXCHANGES = json.loads((FIXTURES / "wire/exchanges.json").read_text(encoding="utf-8"))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def fixture_bytes(relative_path: str) -> bytes:
    return (FIXTURES / relative_path).read_bytes()


def selected_headers(headers: Any, names: list[str]) -> list[list[str]]:
    return [[name, headers.get(name)] for name in names]


@contextmanager
def running_server(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def replay_through_no_transform_proxy(exchange: dict[str, Any]) -> dict[str, Any]:
    request = exchange["request"]
    response = exchange["response"]
    request_body = fixture_bytes(request["body_file"])
    response_body = fixture_bytes(response["body_file"])
    upstream_capture: dict[str, Any] = {}
    proxy_capture: dict[str, Any] = {}
    request_header_names = [name for name, _ in request["selected_headers"]]
    response_header_names = [name for name, _ in response["selected_headers"]]

    class UpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            upstream_capture.update(
                method=self.command,
                path_query=self.path,
                selected_headers=selected_headers(self.headers, request_header_names),
                body=body,
            )
            self.send_response_only(response["status"])
            for name, value in response["selected_headers"]:
                self.send_header(name, value)
            self.send_header("content-length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            self.close_connection = True

        def log_message(self, _format: str, *args: Any) -> None:
            pass

    with running_server(UpstreamHandler) as upstream_port:
        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                proxy_capture.update(
                    method=self.command,
                    path_query=self.path,
                    selected_headers=selected_headers(self.headers, request_header_names),
                    body=body,
                )
                connection = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=5)
                connection.request(
                    self.command,
                    self.path,
                    body=body,
                    headers={name: self.headers[name] for name in request_header_names},
                )
                upstream_response = connection.getresponse()
                upstream_body = upstream_response.read()
                proxy_capture.update(
                    response_status=upstream_response.status,
                    response_headers=selected_headers(upstream_response.headers, response_header_names),
                    response_body=upstream_body,
                )
                self.send_response_only(upstream_response.status)
                for name in response_header_names:
                    self.send_header(name, upstream_response.headers[name])
                self.send_header("content-length", str(len(upstream_body)))
                self.end_headers()
                self.wfile.write(upstream_body)
                self.close_connection = True
                connection.close()

            def log_message(self, _format: str, *args: Any) -> None:
                pass

        with running_server(ProxyHandler) as proxy_port:
            client = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
            client.request(
                request["method"],
                request["path_query"],
                body=request_body,
                headers={name: value for name, value in request["selected_headers"]},
            )
            client_response = client.getresponse()
            client_body = client_response.read()
            result = {
                "status": client_response.status,
                "headers": selected_headers(client_response.headers, response_header_names),
                "body": client_body,
                "proxy": proxy_capture,
                "upstream": upstream_capture,
            }
            client.close()
            return result


def parse_sse(relative_path: str) -> list[tuple[str, dict[str, Any], bytes]]:
    frames = []
    for raw_frame in fixture_bytes(relative_path).split(b"\n\n"):
        if not raw_frame:
            continue
        lines = raw_frame.splitlines()
        event = lines[0].removeprefix(b"event: ").decode("utf-8")
        data_line = lines[1].removeprefix(b"data: ")
        frames.append((event, json.loads(data_line), raw_frame))
    return frames


def typed_command_output_is_eligible(receipt: dict[str, Any]) -> bool:
    exit_status = receipt.get("exit_status")
    output = receipt.get("output")
    output_bytes = output.encode("utf-8") if isinstance(output, str) else b""
    return all(
        (
            receipt.get("schema") == "compand.command_result.v1",
            isinstance(receipt.get("call_id"), str) and bool(receipt["call_id"]),
            receipt.get("source_kind") == "command_result",
            receipt.get("trusted_adapter") is True,
            isinstance(exit_status, int) and not isinstance(exit_status, bool),
            exit_status == 0,
            receipt.get("content_type") == "text/plain",
            receipt.get("encoding") == "utf-8",
            receipt.get("truncated") is False,
            receipt.get("signed") is False,
            isinstance(output, str),
            receipt.get("byte_count") == len(output_bytes),
            0 <= len(output_bytes) <= 1_048_576,
            receipt.get("output_sha256") == hashlib.sha256(output_bytes).hexdigest(),
            receipt.get("new_suffix") is True,
        )
    )


class Proto9CodexResponsesContractTest(unittest.TestCase):
    def test_certification_tuple_is_exact_and_sanitized(self) -> None:
        certification = MANIFEST["certification_tuple"]
        self.assertEqual(certification["client_version"], "0.144.5")
        self.assertEqual(certification["model"], "gpt-5.4")
        self.assertEqual(certification["auth_lane"], "custom_api_provider")
        self.assertEqual(certification["wire_api"], "responses")
        self.assertEqual(certification["env_key"], "COMPAND_FIXTURE_API_KEY")
        self.assertTrue(MANIFEST["sanitized"])
        self.assertFalse(MANIFEST["capture"]["raw_private_prompts_retained"])
        self.assertFalse(MANIFEST["capture"]["credential_values_retained"])
        self.assertFalse(MANIFEST["capture"]["model_discovery_conformant"])

    def test_all_frozen_file_hashes_match(self) -> None:
        hashes = MANIFEST["files_sha256"]
        self.assertTrue(hashes, "fixture manifest must freeze file hashes")
        for relative_path, expected in hashes.items():
            self.assertRegex(expected, SHA256_RE)
            observed = hashlib.sha256(fixture_bytes(relative_path)).hexdigest()
            self.assertEqual(observed, expected, relative_path)

    def test_passthrough_crosses_executable_proxy_without_wire_drift(self) -> None:
        exercised_paths = set()
        for exchange in WIRE_EXCHANGES:
            request = exchange["request"]
            response = exchange["response"]
            request_body = fixture_bytes(request["body_file"])
            response_body = fixture_bytes(response["body_file"])
            result = replay_through_no_transform_proxy(exchange)
            exercised_paths.update((request["body_file"], response["body_file"]))

            for capture in (result["proxy"], result["upstream"]):
                self.assertEqual(capture["method"], request["method"], exchange["exchange_id"])
                self.assertEqual(capture["path_query"], request["path_query"], exchange["exchange_id"])
                self.assertEqual(capture["selected_headers"], request["selected_headers"], exchange["exchange_id"])
                self.assertEqual(capture["body"], request_body, exchange["exchange_id"])
            self.assertEqual(result["proxy"]["response_status"], response["status"])
            self.assertEqual(result["proxy"]["response_headers"], response["selected_headers"])
            self.assertEqual(result["proxy"]["response_body"], response_body)
            self.assertEqual(result["status"], response["status"])
            self.assertEqual(result["headers"], response["selected_headers"])
            self.assertEqual(result["body"], response_body)

        self.assertEqual(exercised_paths, set(MANIFEST["direct_passthrough"]))

    def test_exact_codex_coverage_excludes_unobserved_provider_shapes(self) -> None:
        coverage = MANIFEST["surface_coverage"]
        exact = {name for name, value in coverage.items() if value["exact_codex_observed"]}
        self.assertEqual(exact, {"normal_stream", "tool_call", "tool_output_followup"})
        for name, value in coverage.items():
            if value["exact_codex_observed"]:
                self.assertRegex(value["original_body_sha256"], SHA256_RE, name)
                self.assertGreater(value["original_body_bytes"], 0, name)
                self.assertTrue(value["observed_client_outcome"], name)
            else:
                self.assertIsNone(value["original_body_sha256"], name)
                self.assertIsNone(value["original_body_bytes"], name)
                self.assertIn(value["evidence_class"], {"provider_contract_only", "unsupported"}, name)
                self.assertTrue(value["unobserved_reason"], name)

    def test_fixture_tree_contains_no_secret_or_private_prompt_material(self) -> None:
        forbidden = (
            b"Authorization: Bearer",
            b"sk-proj-",
            b"/Users/",
            b"assignment-",
            b"execlease-",
        )
        for path in FIXTURES.rglob("*"):
            if path.is_file():
                payload = path.read_bytes()
                for marker in forbidden:
                    self.assertNotIn(marker, payload, str(path.relative_to(FIXTURES)))

    def test_sse_frames_and_event_order_are_frozen(self) -> None:
        for relative_path, expected_events in MANIFEST["streams"].items():
            frames = parse_sse(relative_path)
            observed_events = [event for event, _, _ in frames]
            self.assertEqual(observed_events, expected_events, relative_path)
            sequence_numbers = []
            for event, data, raw_frame in frames:
                self.assertEqual(data["type"], event)
                self.assertTrue(raw_frame.startswith(f"event: {event}\n".encode()))
                sequence_numbers.append(data["sequence_number"])
            self.assertEqual(sequence_numbers, list(range(len(frames))))

    def test_cancelled_stream_stays_partial_without_synthetic_truth(self) -> None:
        cancellation = MANIFEST["cancellation"]
        events = [event for event, _, _ in parse_sse(cancellation["stream"])]
        self.assertNotIn("response.completed", events)
        self.assertFalse(cancellation["terminal_event_present"])
        self.assertFalse(cancellation["synthetic_usage_allowed"])

    def test_error_status_headers_and_bodies_are_preserved(self) -> None:
        for expected_status, relative_path in MANIFEST["errors"].items():
            fixture = json.loads(fixture_bytes(relative_path))
            exchanges = [item for item in WIRE_EXCHANGES if item["response"]["body_file"] == relative_path]
            self.assertTrue(exchanges, relative_path)
            self.assertTrue(any(item["response"]["status"] == int(expected_status) for item in exchanges))
            self.assertIn("error", fixture)

    def test_retry_reuses_identical_frozen_request_bytes(self) -> None:
        retry = MANIFEST["retry"]
        request = fixture_bytes(retry["request"])
        attempt_bodies = [request for _ in retry["attempts"]]
        self.assertTrue(retry["request_body_must_remain_identical"])
        self.assertEqual(attempt_bodies[0], attempt_bodies[1])
        self.assertEqual(
            hashlib.sha256(attempt_bodies[0]).hexdigest(),
            hashlib.sha256(attempt_bodies[1]).hexdigest(),
        )

    def test_continuation_surfaces_remain_distinct(self) -> None:
        previous = json.loads(fixture_bytes("requests/previous-response.json"))
        conversation = json.loads(fixture_bytes("requests/conversation.json"))
        manual = json.loads(fixture_bytes("requests/manual-continuation.json"))
        self.assertIn("previous_response_id", previous)
        self.assertNotIn("conversation", previous)
        self.assertIn("conversation", conversation)
        self.assertNotIn("previous_response_id", conversation)
        self.assertTrue(any(item.get("encrypted_content") for item in manual["input"]))

    def test_dual_ledger_replays_frozen_provider_prefix_only(self) -> None:
        ledger = json.loads(fixture_bytes("ledgers/continuation.json"))
        client_prefix = ledger["client_view_prefix"]
        provider_prefix = ledger["provider_view_prefix"]
        incoming = ledger["incoming_client_items"]
        dispatched = ledger["expected_provider_items"]
        self.assertEqual(incoming[: len(client_prefix)], client_prefix)
        self.assertEqual(dispatched[: len(provider_prefix)], provider_prefix)
        self.assertEqual(dispatched[len(provider_prefix) :], incoming[len(client_prefix) :])
        self.assertEqual(ledger["retry_provider_items"], dispatched)
        self.assertNotEqual(ledger["recompressed_drift_candidate"], dispatched)
        self.assertFalse(ledger["recompressed_drift_allowed"])

    def test_tool_output_requires_trusted_typed_exit_status(self) -> None:
        receipts = [
            json.loads(line)
            for line in (FIXTURES / "eligibility/receipts.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        for receipt in receipts:
            self.assertEqual(
                typed_command_output_is_eligible(receipt),
                receipt["expected_eligible"],
                receipt["fixture_id"],
            )
        raw = next(item for item in receipts if item["fixture_id"] == "raw_codex_text_only")
        self.assertIn("Process exited with code 0", raw["output"])
        self.assertFalse(typed_command_output_is_eligible(raw))

    def test_unsupported_shapes_are_explicit_unchanged_passthrough(self) -> None:
        fixtures = json.loads(fixture_bytes("requests/unsupported.json"))
        self.assertEqual(
            {fixture["fixture_id"] for fixture in fixtures},
            {"binary", "unknown_encoding", "signed", "arbitrary_json", "oversized", "unknown_event"},
        )
        self.assertTrue(all(fixture["passthrough"] is True for fixture in fixtures))

    def test_provider_input_token_count_and_usage_fields_are_explicit(self) -> None:
        request = json.loads(fixture_bytes("requests/input-token-count.json"))
        count = json.loads(fixture_bytes("responses/input-token-count.json"))
        response = json.loads(fixture_bytes("responses/normal.json"))
        self.assertEqual(request["model"], "gpt-5.4")
        self.assertEqual(count["object"], "response.input_tokens")
        self.assertIsInstance(count["input_tokens"], int)
        usage = response["usage"]
        self.assertEqual(
            set(usage),
            {"input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"},
        )
        self.assertIn("cached_tokens", usage["input_tokens_details"])
        self.assertIn("cache_write_tokens", usage["input_tokens_details"])
        self.assertIn("reasoning_tokens", usage["output_tokens_details"])


if __name__ == "__main__":
    unittest.main()
