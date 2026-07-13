#!/usr/bin/env python3
"""BUG-63: the deploy gate tolerates startup delay but still fails closed."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from path_setup import ROOT

GATE = ROOT / "deploy" / "wait-for-health.sh"
REDEPLOY = ROOT / "deploy" / "redeploy.sh"

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


class SequencedHealthHandler(BaseHTTPRequestHandler):
    response_codes = [200]
    calls = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).calls += 1
        index = min(type(self).calls - 1, len(type(self).response_codes) - 1)
        status = type(self).response_codes[index]
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}' if status == 200 else b'{"status":"starting"}')

    def log_message(self, _format: str, *_args: object) -> None:
        return


def run_gate(responses: list[int], timeout: int) -> subprocess.CompletedProcess[str]:
    SequencedHealthHandler.response_codes = responses
    SequencedHealthHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), SequencedHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update({
        "HEALTH_URL": f"http://127.0.0.1:{server.server_port}/health",
        "HEALTH_TIMEOUT_SECONDS": str(timeout),
        "HEALTH_INTERVAL_SECONDS": "1",
        "HEALTH_CURL_TIMEOUT_SECONDS": "1",
    })
    try:
        return subprocess.run(
            ["bash", str(GATE)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


delayed = run_gate([503, 503, 200], timeout=5)
ok(delayed.returncode == 0, "delayed startup succeeds within the bounded health window")
ok("local /health: 503 (attempt 1)" in delayed.stdout
   and "local /health: 200 (attempt 3)" in delayed.stdout,
   "the gate reports each real response and the successful retry attempt")
ok("health gate passed." in delayed.stdout,
   "successful recovery is explicit")

failed_gate = run_gate([503], timeout=2)
ok(failed_gate.returncode == 1, "permanently unhealthy service fails closed at the deadline")
ok("did not return 200 within 2s" in failed_gate.stderr,
   "deadline failure names the bounded timeout")

with socket.socket() as unused_socket:
    unused_socket.bind(("127.0.0.1", 0))
    unused_port = unused_socket.getsockname()[1]
unreachable_env = os.environ.copy()
unreachable_env.update({
    "HEALTH_URL": f"http://127.0.0.1:{unused_port}/health",
    "HEALTH_TIMEOUT_SECONDS": "1",
    "HEALTH_INTERVAL_SECONDS": "1",
    "HEALTH_CURL_TIMEOUT_SECONDS": "1",
})
unreachable = subprocess.run(
    ["bash", str(GATE)], cwd=ROOT, env=unreachable_env, text=True,
    capture_output=True, timeout=5, check=False,
)
ok(unreachable.returncode == 1
   and "local /health: 000 (attempt 1)" in unreachable.stdout
   and "000000" not in unreachable.stdout,
   "connection failures retain one canonical 000 response code")

invalid_env = os.environ.copy()
invalid_env["HEALTH_TIMEOUT_SECONDS"] = "not-a-number"
invalid = subprocess.run(
    ["bash", str(GATE)], cwd=ROOT, env=invalid_env, text=True,
    capture_output=True, timeout=5, check=False,
)
ok(invalid.returncode == 2 and "must be a positive integer" in invalid.stderr,
   "invalid retry configuration fails before probing")

redeploy_text = REDEPLOY.read_text(encoding="utf-8")
ok('bash "$ROOT/deploy/wait-for-health.sh"' in redeploy_text,
   "the production redeploy path invokes the tested health gate")
ok("sleep 2\ncode=" not in redeploy_text and "|| echo 000" not in redeploy_text,
   "the single fixed-delay probe and duplicate 000 fallback are removed")

print(f"\nBUG-63 redeploy health gate: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
