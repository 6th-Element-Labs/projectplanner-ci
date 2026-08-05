#!/usr/bin/env python3
"""Run the Compand pilot through real loopback HTTP and emit content-free evidence."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from switchboard.domain.compand import GatewayMode  # noqa: E402
from switchboard.services.compand.app import create_app  # noqa: E402
from switchboard.services.compand.settings import CompandGatewaySettings  # noqa: E402


CLIENT_A = "dogfood-client-a"
CLIENT_B = "dogfood-client-b"
UPSTREAM = "dogfood-upstream"


class CapturingProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    forwarded: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.path == "/v1/responses/input_tokens":
            output = json.loads(body)["input"][-1]["output"]
            count = 4 if "[repeated " in output else 20
            payload = json.dumps(
                {"object": "response.input_tokens", "input_tokens": count},
                separators=(",", ":"),
            ).encode()
        elif self.path == "/v1/responses":
            self.forwarded.append(
                {
                    "correlation_id": self.headers.get("x-compand-correlation-id", ""),
                    "body_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "transformed": b"[repeated " in body,
                    "body": body,
                }
            )
            payload = b"{}"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def log_message(self, _format: str, *args: Any) -> None:
        return None


@contextmanager
def provider_server():
    CapturingProvider.forwarded = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CapturingProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def gateway_server(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        raise RuntimeError("Compand gateway failed to start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def exchange(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = (response.status, dict(response.headers.items()), response.read())
    connection.close()
    return result


def main() -> int:
    output = "dogfood repeated line\n" * 4
    body = json.dumps(
        {
            "model": "gpt-5.4",
            "input": [
                {"type": "function_call_output", "call_id": "call-dogfood", "output": output}
            ],
            "store": False,
            "stream": True,
            "reasoning": {"effort": "high"},
        },
        separators=(",", ":"),
    ).encode()
    receipt = {
        "schema": "compand.command_result.v1",
        "call_id": "call-dogfood",
        "source_kind": "command_result",
        "trusted_adapter": True,
        "exit_status": 0,
        "content_type": "text/plain",
        "encoding": "utf-8",
        "truncated": False,
        "signed": False,
        "new_suffix": True,
        "byte_count": len(output.encode()),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="compand-dogfood-") as temp:
        with provider_server() as provider_port:
            app = create_app(
                CompandGatewaySettings(
                    upstream_origin=f"http://127.0.0.1:{provider_port}",
                    upstream_api_key=UPSTREAM,
                    client_credentials={"tenant-a": CLIENT_A, "tenant-b": CLIENT_B},
                    mode=GatewayMode.ENFORCE,
                    source_version="ENFORCE-25-live-dogfood",
                    allow_http_loopback=True,
                    frozen_tuple_config_attested=True,
                    state_db_path=str(Path(temp) / "compand.sqlite"),
                    capability_secret="dogfood-local-capability-key",
                    artifact_retention_seconds=300,
                )
            )
            repository = app.state.compand_gateway_runtime.repository
            with gateway_server(app) as gateway_port:
                headers = {
                    "authorization": f"Bearer {CLIENT_A}",
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                    "x-compand-session-id": "dogfood-session",
                    "x-compand-command-receipt": json.dumps(receipt, separators=(",", ":")),
                }
                status, response_headers, _ = exchange(
                    gateway_port, "POST", "/v1/responses", body=body, headers=headers
                )
                retry_status, retry_headers, _ = exchange(
                    gateway_port,
                    "POST",
                    "/v1/responses",
                    body=body,
                    headers={key: value for key, value in headers.items() if key != "x-compand-command-receipt"},
                )
                capability = response_headers.get("x-compand-recovery-capability", "")
                recover_status, _, recovered = exchange(
                    gateway_port,
                    "GET",
                    f"/compand/v1/artifacts/{capability}",
                    headers={
                        "authorization": f"Bearer {CLIENT_A}",
                        "x-compand-session-id": "dogfood-session",
                    },
                )
                denied_status, _, _ = exchange(
                    gateway_port,
                    "GET",
                    f"/compand/v1/artifacts/{capability}",
                    headers={
                        "authorization": f"Bearer {CLIENT_B}",
                        "x-compand-session-id": "dogfood-session",
                    },
                )
                for observed in CapturingProvider.forwarded:
                    repository.record_observation(
                        str(observed["correlation_id"]),
                        "loopback_provider_process",
                        "/v1/responses",
                        "captured",
                    )
                snapshot = repository.evidence_snapshot()
                checks = {
                    "response_ok": status == 200,
                    "provider_received_transformed_body": bool(CapturingProvider.forwarded[0]["transformed"]),
                    "retry_reused_frozen_body": (
                        retry_status == 200
                        and retry_headers.get("x-compand-outcome") == "frozen_retry"
                        and CapturingProvider.forwarded[0]["body_sha256"]
                        == CapturingProvider.forwarded[1]["body_sha256"]
                    ),
                    "exact_recovery": recover_status == 200 and recovered == output.encode(),
                    "cross_tenant_recovery_denied": denied_status == 404,
                    "independent_provider_observation": len(snapshot["observations"]) == 2,
                    "content_free_evidence": output not in json.dumps(snapshot, default=str),
                }
                evidence = {
                    "schema": "compand.live_dogfood_evidence.v1",
                    "mode": "enforce",
                    "technique": "line-rle-v1",
                    "checks": checks,
                    "receipts": snapshot["receipts"],
                    "observations": snapshot["observations"],
                }
                print(json.dumps(evidence, indent=2, sort_keys=True, default=str))
                return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
