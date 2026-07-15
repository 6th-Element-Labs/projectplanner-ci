"""Host-local PTY byte stream for dedicated Codex runner sessions (CO-12).

A short-lived companion process inherits the PTY master fd, dual-writes output to
stdout.log, and serves authenticated HTTP chunked streams for curl/browser clients.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from switchboard.api.routers.auth.jwt_util import decode as jwt_decode
    from switchboard.api.routers.auth.jwt_util import encode as jwt_encode
except ModuleNotFoundError:  # adapters/ on sys.path without src/
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_ROOT / "src"))
    from switchboard.api.routers.auth.jwt_util import decode as jwt_decode
    from switchboard.api.routers.auth.jwt_util import encode as jwt_encode


def stream_secret() -> str:
    return str(
        os.environ.get("PM_RUNNER_STREAM_SECRET")
        or os.environ.get("PM_MCP_TOKEN")
        or "switchboard-runner-stream-dev"
    )


def mint_ticket(
    *,
    runner_session_id: str,
    host_id: str = "",
    ttl_seconds: int = 900,
    now: float | None = None,
) -> tuple[str, float]:
    issued = float(now if now is not None else time.time())
    expires = issued + max(30, int(ttl_seconds))
    token = jwt_encode(
        {
            "scope": "runner_stream",
            "runner_session_id": runner_session_id,
            "host_id": host_id or "",
            "iat": int(issued),
            "exp": int(expires),
        },
        stream_secret(),
    )
    return token, expires


def verify_ticket(
    ticket: str,
    *,
    runner_session_id: str,
    host_id: str = "",
    now: float | None = None,
) -> tuple[bool, str]:
    payload, reason = jwt_decode(ticket, stream_secret(), now=now)
    if payload is None:
        return False, reason or "invalid_ticket"
    if payload.get("scope") != "runner_stream":
        return False, "wrong_scope"
    if str(payload.get("runner_session_id") or "") != str(runner_session_id):
        return False, "session_mismatch"
    expected_host = str(host_id or "")
    ticket_host = str(payload.get("host_id") or "")
    if expected_host and ticket_host and ticket_host != expected_host:
        return False, "host_mismatch"
    return True, ""


def build_stream_url(
    *,
    bind_host: str,
    port: int,
    runner_session_id: str,
    ticket: str,
    public_base: str = "",
) -> str:
    base = (public_base or "").rstrip("/")
    if not base:
        host = bind_host if bind_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        base = f"http://{host}:{int(port)}"
    query = urllib.parse.urlencode({"ticket": ticket})
    return f"{base}/runner/v1/sessions/{urllib.parse.quote(runner_session_id)}/stream?{query}"


class _Fanout:
    def __init__(self, log_path: Path, replay_bytes: int = 65536):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clients: list[Any] = []
        self._closed = False
        self._log = self.log_path.open("ab")
        self._replay = bytearray()
        self._replay_limit = max(4096, int(replay_bytes))

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def add_client(self, write_chunk) -> bytes:
        with self._lock:
            if self._closed:
                raise RuntimeError("stream closed")
            self._clients.append(write_chunk)
            return bytes(self._replay)

    def remove_client(self, write_chunk) -> None:
        with self._lock:
            try:
                self._clients.remove(write_chunk)
            except ValueError:
                pass

    def publish(self, data: bytes) -> None:
        if not data:
            return
        self._log.write(data)
        self._log.flush()
        with self._lock:
            self._replay.extend(data)
            if len(self._replay) > self._replay_limit:
                overflow = len(self._replay) - self._replay_limit
                del self._replay[:overflow]
            clients = list(self._clients)
        dead = []
        for client in clients:
            try:
                client(data)
            except Exception:
                dead.append(client)
        for client in dead:
            self.remove_client(client)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._clients = []
        try:
            self._log.close()
        except Exception:
            pass


def _make_handler(fanout: _Fanout, runner_session_id: str, host_id: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            return

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            expected = f"/runner/v1/sessions/{runner_session_id}/stream"
            if parsed.path.rstrip("/") != expected.rstrip("/"):
                self.send_error(404, "not_found")
                return
            ticket = urllib.parse.parse_qs(parsed.query).get("ticket", [""])[0]
            ok, reason = verify_ticket(
                ticket, runner_session_id=runner_session_id, host_id=host_id)
            if not ok:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "unauthorized", "reason": reason}).encode())
                return
            queue: list[bytes] = []
            event = threading.Event()

            def write_chunk(data: bytes) -> None:
                queue.append(data)
                event.set()

            try:
                replay = fanout.add_client(write_chunk)
            except Exception:
                self.send_error(503, "stream_closed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Switchboard-Runner-Session", runner_session_id)
            self.end_headers()
            try:
                if replay:
                    size = f"{len(replay):x}\r\n".encode()
                    self.wfile.write(size + replay + b"\r\n")
                    self.wfile.flush()
                # Stay open for the life of the client/session. Exit only when the
                # fanout closes (PTY EOF) or the client disconnects — never after
                # idle timeout while the child is still alive.
                while not fanout.closed:
                    event.wait(timeout=1.0)
                    event.clear()
                    while queue:
                        chunk = queue.pop(0)
                        size = f"{len(chunk):x}\r\n".encode()
                        self.wfile.write(size + chunk + b"\r\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                fanout.remove_client(write_chunk)

    return Handler


def serve(
    *,
    master_fd: int,
    log_path: str,
    runner_session_id: str,
    host_id: str = "",
    bind_host: str = "127.0.0.1",
    port: int = 0,
    ready_path: str = "",
) -> int:
    fanout = _Fanout(Path(log_path))
    server = ThreadingHTTPServer((bind_host, int(port)), _make_handler(
        fanout, runner_session_id, host_id))
    actual_port = int(server.server_address[1])
    if ready_path:
        Path(ready_path).write_text(json.dumps({
            "runner_session_id": runner_session_id,
            "bind_host": bind_host,
            "port": actual_port,
            "pid": os.getpid(),
            "stream_path": f"/runner/v1/sessions/{runner_session_id}/stream",
        }), encoding="utf-8")

    def pump() -> None:
        try:
            while True:
                readable, _, _ = select.select([master_fd], [], [], 0.5)
                if not readable:
                    continue
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                fanout.publish(data)
        finally:
            fanout.close()
            try:
                server.shutdown()
            except Exception:
                pass

    threading.Thread(target=pump, name="pty-pump", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        fanout.close()
        try:
            os.close(master_fd)
        except OSError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PTY stream companion for CO-12")
    parser.add_argument("--runner-session-id", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--master-fd", type=int, required=True)
    parser.add_argument("--host-id", default="")
    parser.add_argument("--bind-host", default=os.environ.get("PM_RUNNER_STREAM_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PM_RUNNER_STREAM_PORT", "0") or 0))
    parser.add_argument("--ready-path", default="")
    args = parser.parse_args(argv)
    return serve(
        master_fd=args.master_fd,
        log_path=args.log_path,
        runner_session_id=args.runner_session_id,
        host_id=args.host_id,
        bind_host=args.bind_host,
        port=args.port,
        ready_path=args.ready_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
