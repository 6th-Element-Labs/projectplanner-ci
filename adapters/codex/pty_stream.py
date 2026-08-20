"""Host-local PTY executor companion for dedicated Codex runner sessions.

A short-lived companion process inherits the PTY master fd and dual-writes
output to stdout.log. SIMPLIFY-9: when ``host_relay.url`` appears in the
session directory (or ``--relay-ws-url`` is set), the companion dials ONE
outbound binary WebSocket to Switchboard and pumps PTY I/O there — no
localhost HTTP hop on the browser Watch path.

The Switchboard relay is the only supported browser transport.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
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

try:
    from adapters import relay_auth
except ModuleNotFoundError:
    try:
        import relay_auth
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import relay_auth

# ``session_chat`` is the browser/control-plane name for ordinary text entered in
# the Watch composer.  Keep it distinct in the audit response while formatting it
# exactly like ``freeform`` for the PTY.  The two sides previously disagreed here,
# causing a valid browser message to fail with HTTP 400.
INJECT_KINDS = frozenset({
    "freeform", "session_chat", "redirect", "hold", "approve",
})
CONTROL_ACTIONS = frozenset({"input", "resize", "signal"})
STREAM_CLIENT_QUEUE_LIMIT = 64
_SIGNAL_BYTES = {
    "SIGINT": b"\x03",
    "CTRL-C": b"\x03",
    "CTRL_C": b"\x03",
    "SIGTSTP": b"\x1a",
    "CTRL-Z": b"\x1a",
    "CTRL_Z": b"\x1a",
    "SIGQUIT": b"\x1c",
    "CTRL-\\": b"\x1c",
    "EOF": b"\x04",
    "CTRL-D": b"\x04",
    "CTRL_D": b"\x04",
}
_SHORTCUT_PREFIX = {
    "redirect": "[Switchboard Redirect] ",
    "hold": "[Switchboard Hold] ",
    "approve": "[Switchboard Approve] ",
    "freeform": "",
}


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


def mint_inject_ticket(
    *,
    runner_session_id: str,
    task_id: str,
    host_id: str = "",
    ttl_seconds: int = 120,
    now: float | None = None,
) -> tuple[str, float]:
    issued = float(now if now is not None else time.time())
    expires = issued + max(30, int(ttl_seconds))
    token = jwt_encode(
        {
            "scope": "runner_inject",
            "runner_session_id": runner_session_id,
            "task_id": str(task_id or ""),
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


def verify_inject_ticket(
    ticket: str,
    *,
    runner_session_id: str,
    task_id: str,
    host_id: str = "",
    now: float | None = None,
) -> tuple[bool, str]:
    payload, reason = jwt_decode(ticket, stream_secret(), now=now)
    if payload is None:
        return False, reason or "invalid_ticket"
    if payload.get("scope") != "runner_inject":
        return False, "wrong_scope"
    if str(payload.get("runner_session_id") or "") != str(runner_session_id):
        return False, "session_mismatch"
    if str(payload.get("task_id") or "") != str(task_id or ""):
        return False, "task_mismatch"
    expected_host = str(host_id or "")
    ticket_host = str(payload.get("host_id") or "")
    if expected_host and ticket_host and ticket_host != expected_host:
        return False, "host_mismatch"
    return True, ""


def mint_control_ticket(
    *,
    runner_session_id: str,
    host_id: str = "",
    actions: list[str] | None = None,
    ttl_seconds: int = 900,
    now: float | None = None,
) -> tuple[str, float]:
    issued = float(now if now is not None else time.time())
    expires = issued + max(30, int(ttl_seconds))
    allowed = []
    for action in actions or ["input", "resize", "signal"]:
        key = str(action or "").strip().lower()
        if key in CONTROL_ACTIONS and key not in allowed:
            allowed.append(key)
    if not allowed:
        allowed = ["input", "resize", "signal"]
    token = jwt_encode(
        {
            "scope": "runner_pty_control",
            "runner_session_id": runner_session_id,
            "host_id": host_id or "",
            "actions": allowed,
            "iat": int(issued),
            "exp": int(expires),
        },
        stream_secret(),
    )
    return token, expires


def verify_control_ticket(
    ticket: str,
    *,
    runner_session_id: str,
    action: str,
    host_id: str = "",
    now: float | None = None,
) -> tuple[bool, str]:
    payload, reason = jwt_decode(ticket, stream_secret(), now=now)
    if payload is None:
        return False, reason or "invalid_ticket"
    if payload.get("scope") != "runner_pty_control":
        return False, "wrong_scope"
    if str(payload.get("runner_session_id") or "") != str(runner_session_id):
        return False, "session_mismatch"
    expected_host = str(host_id or "")
    ticket_host = str(payload.get("host_id") or "")
    if expected_host and ticket_host and ticket_host != expected_host:
        return False, "host_mismatch"
    action_key = str(action or "").strip().lower()
    allowed = {
        str(item or "").strip().lower()
        for item in (payload.get("actions") or [])
    }
    if action_key not in CONTROL_ACTIONS:
        return False, "unsupported_action"
    if action_key not in allowed:
        return False, "action_denied"
    return True, ""


def set_pty_winsize(master_fd: int, rows: int, cols: int) -> None:
    packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
    fcntl.ioctl(int(master_fd), termios.TIOCSWINSZ, packed)


def signal_to_bytes(name: str) -> bytes | None:
    key = str(name or "").strip().upper().replace(" ", "_")
    if not key:
        return None
    return _SIGNAL_BYTES.get(key)


def format_inject_payload(
    text: str,
    *,
    kind: str = "freeform",
    newline: bool = True,
) -> bytes:
    kind_key = str(kind or "freeform").strip().lower() or "freeform"
    if kind_key not in INJECT_KINDS:
        kind_key = "freeform"
    body = str(text or "")
    prefix = _SHORTCUT_PREFIX.get(kind_key, "")
    payload = f"{prefix}{body}"
    if newline and payload and not payload.endswith("\n"):
        payload += "\n"
    return payload.encode("utf-8", errors="replace")


def build_inject_url(
    *,
    bind_host: str,
    port: int,
    runner_session_id: str,
    public_base: str = "",
) -> str:
    base = (public_base or "").rstrip("/")
    if not base:
        host = bind_host if bind_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        base = f"http://{host}:{int(port)}"
    return f"{base}/runner/v1/sessions/{urllib.parse.quote(runner_session_id)}/inject"


def build_control_url(
    *,
    bind_host: str,
    port: int,
    runner_session_id: str,
    public_base: str = "",
) -> str:
    base = (public_base or "").rstrip("/")
    if not base:
        host = bind_host if bind_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        base = f"http://{host}:{int(port)}"
    return f"{base}/runner/v1/sessions/{urllib.parse.quote(runner_session_id)}/control"


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

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._replay)

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


def _make_handler(
    fanout: _Fanout,
    runner_session_id: str,
    host_id: str,
    master_fd: int,
    write_lock: threading.Lock,
    bound_task_id: str = "",
    stream_queue_limit: int = STREAM_CLIENT_QUEUE_LIMIT,
):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            overflow = {"flag": False}
            limit = max(1, int(stream_queue_limit))

            def write_chunk(data: bytes) -> None:
                if overflow["flag"]:
                    raise RuntimeError("backpressure")
                if len(queue) >= limit:
                    overflow["flag"] = True
                    event.set()
                    raise RuntimeError("backpressure")
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
                    if overflow["flag"]:
                        break
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

        def _handle_control(self, body: dict[str, Any], parsed) -> None:
            ticket = str(
                body.get("ticket")
                or self.headers.get("X-Switchboard-Control-Ticket")
                or urllib.parse.parse_qs(parsed.query).get("ticket", [""])[0]
                or ""
            )
            action = str(body.get("action") or "").strip().lower()
            ok, reason = verify_control_ticket(
                ticket,
                runner_session_id=runner_session_id,
                action=action,
                host_id=host_id,
            )
            if not ok:
                self._json(401, {"error": "unauthorized", "reason": reason})
                return
            if fanout.closed:
                self._json(503, {"error": "not_supported", "reason": "pty_closed"})
                return
            if action == "input":
                data = b""
                if body.get("data_b64") is not None:
                    try:
                        data = base64.b64decode(str(body.get("data_b64") or ""), validate=False)
                    except Exception:
                        self._json(400, {"error": "invalid_input", "reason": "bad_data_b64"})
                        return
                elif isinstance(body.get("text"), str):
                    # Raw text — no forced newline (unlike inject).
                    data = body["text"].encode("utf-8", errors="replace")
                elif isinstance(body.get("data"), str):
                    data = body["data"].encode("utf-8", errors="replace")
                else:
                    self._json(400, {"error": "invalid_input", "reason": "data_required"})
                    return
                if not data:
                    self._json(400, {"error": "invalid_input", "reason": "empty_input"})
                    return
                try:
                    with write_lock:
                        written = os.write(master_fd, data)
                except OSError as exc:
                    self._json(503, {
                        "error": "not_supported",
                        "reason": "pty_write_failed",
                        "message": str(exc),
                    })
                    return
                self._json(200, {
                    "ok": True,
                    "action": "input",
                    "runner_session_id": runner_session_id,
                    "bytes_written": written,
                })
                return
            if action == "resize":
                try:
                    rows = int(body.get("rows") or body.get("row") or 0)
                    cols = int(body.get("cols") or body.get("col") or body.get("columns") or 0)
                except (TypeError, ValueError):
                    self._json(400, {"error": "invalid_input", "reason": "rows_cols_required"})
                    return
                if rows <= 0 or cols <= 0:
                    self._json(400, {"error": "invalid_input", "reason": "rows_cols_required"})
                    return
                try:
                    with write_lock:
                        set_pty_winsize(master_fd, rows, cols)
                except OSError as exc:
                    self._json(503, {
                        "error": "not_supported",
                        "reason": "pty_resize_failed",
                        "message": str(exc),
                    })
                    return
                self._json(200, {
                    "ok": True,
                    "action": "resize",
                    "runner_session_id": runner_session_id,
                    "rows": rows,
                    "cols": cols,
                })
                return
            if action == "signal":
                name = str(body.get("name") or body.get("signal") or "SIGINT")
                payload = signal_to_bytes(name)
                if payload is None:
                    self._json(400, {
                        "error": "not_supported",
                        "reason": "unsupported_signal",
                        "name": name,
                    })
                    return
                try:
                    with write_lock:
                        written = os.write(master_fd, payload)
                except OSError as exc:
                    self._json(503, {
                        "error": "not_supported",
                        "reason": "pty_signal_failed",
                        "message": str(exc),
                    })
                    return
                self._json(200, {
                    "ok": True,
                    "action": "signal",
                    "runner_session_id": runner_session_id,
                    "name": name,
                    "bytes_written": written,
                })
                return
            self._json(400, {"error": "invalid_input", "reason": "unsupported_action"})

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            control_path = f"/runner/v1/sessions/{runner_session_id}/control"
            inject_path = f"/runner/v1/sessions/{runner_session_id}/inject"
            path = parsed.path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(max(0, length)) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._json(400, {"error": "malformed_payload", "reason": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "malformed_payload", "reason": "body_must_be_object"})
                return
            if path == control_path.rstrip("/"):
                self._handle_control(body, parsed)
                return
            if path != inject_path.rstrip("/"):
                self.send_error(404, "not_found")
                return
            ticket = str(
                body.get("ticket")
                or self.headers.get("X-Switchboard-Inject-Ticket")
                or urllib.parse.parse_qs(parsed.query).get("ticket", [""])[0]
                or ""
            )
            task_id = str(body.get("task_id") or "").strip()
            ok, reason = verify_inject_ticket(
                ticket,
                runner_session_id=runner_session_id,
                task_id=task_id,
                host_id=host_id,
            )
            if not ok:
                self._json(401, {"error": "unauthorized", "reason": reason})
                return
            if bound_task_id and task_id != bound_task_id:
                self._json(403, {
                    "error": "wrong_session",
                    "reason": "task_mismatch",
                    "expected_task_id": bound_task_id,
                })
                return
            if fanout.closed:
                self._json(503, {"error": "not_supported", "reason": "pty_closed"})
                return
            kind = str(body.get("kind") or "freeform").strip().lower() or "freeform"
            if kind not in INJECT_KINDS:
                self._json(400, {"error": "invalid_input", "reason": "unsupported_kind"})
                return
            text = body.get("text")
            if text is None:
                text = body.get("message")
            if not isinstance(text, str) or not text:
                self._json(400, {"error": "invalid_input", "reason": "text_required"})
                return
            submit = bool(body.get("nl", body.get("newline", True)))
            payload = format_inject_payload(text, kind=kind, newline=False)
            try:
                with write_lock:
                    written = os.write(master_fd, payload)
                    if submit:
                        # Codex can treat text plus Enter in one write as a
                        # paste, leaving the text in its composer.  Model the
                        # real terminal interaction: type, then press Enter.
                        time.sleep(0.075)
                        written += os.write(master_fd, b"\r")
            except OSError as exc:
                self._json(503, {
                    "error": "not_supported",
                    "reason": "pty_write_failed",
                    "message": str(exc),
                })
                return
            self._json(200, {
                "injected": True,
                "runner_session_id": runner_session_id,
                "task_id": task_id,
                "kind": kind,
                "bytes_written": written,
            })

    return Handler


def _relay_url_path(runner_session_id: str, ready_path: str = "") -> Path:
    if ready_path:
        return Path(ready_path).resolve().parent / "host_relay.url"
    try:
        from codex.supervisor import _session_dir
    except ModuleNotFoundError:
        from adapters.codex.supervisor import _session_dir  # type: ignore
    return _session_dir(runner_session_id) / "host_relay.url"


def _request_fresh_relay_url(
        runner_session_id: str, host_id: str, url_path: Path, current_url: str) -> str:
    """Ask the credential-owning Agent Host to pull a WATCH-7 ticket."""
    request_path = url_path.with_name("host_relay.refresh")
    request_path.write_text(json.dumps({
        "runner_session_id": str(runner_session_id or ""),
        "host_id": str(host_id or ""),
        "requested_at": time.time(),
    }), encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if url_path.exists():
            fresh_url = url_path.read_text(encoding="utf-8").strip()
            if fresh_url and fresh_url != current_url:
                return fresh_url
        time.sleep(0.25)
    raise TimeoutError("relay_refresh_url_timeout")


def _start_host_ws_executor(
    *,
    master_fd: int,
    log_path: str,
    runner_session_id: str,
    relay_ws_url: str,
    child_pid: int = 0,
    initial_snapshot: bytes = b"",
    target_label: str = "",
    refresh_url: Any = None,
    reconnect_log: Any = None,
    auth_policy: Any = None,
) -> Any:
    """Dial Switchboard and pump master_fd (file logging stays in the executor)."""
    try:
        from adapters.codex.pty_host_ws_client import open_host_bridge
    except ModuleNotFoundError:
        from codex.pty_host_ws_client import open_host_bridge  # type: ignore
    bridge = open_host_bridge(
        runner_session_id=runner_session_id,
        relay_ws_url=relay_ws_url,
        master_fd=master_fd,
        child_pid=child_pid,
        target_label=target_label,
        log_path=log_path,
        dial=True,
        refresh_url=refresh_url,
        reconnect_log=reconnect_log,
        auth_policy=auth_policy,
    )
    if initial_snapshot:
        from switchboard.domain import runner_pty as pty_domain
        bridge.conn.send(
            pty_domain.encode_frame("snapshot", data=initial_snapshot),
            timeout=None,
        )
    return bridge


def _attention_http(method: str, path: str, body: Any = None) -> dict[str, Any]:
    """POST/GET Switchboard host attention APIs using the session bearer."""
    try:
        from adapters.switchboard_core import _http
    except ModuleNotFoundError:
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from adapters.switchboard_core import _http
        except ModuleNotFoundError:
            adapters_dir = str(Path(__file__).resolve().parents[1])
            if adapters_dir not in sys.path:
                sys.path.insert(0, adapters_dir)
            from switchboard_core import _http  # type: ignore
    return _http(method, path, body)


def _import_pty_prompt_attention():
    """Resolve the ADAPTER-31 module whether launched as a package or script."""
    try:
        from adapters.codex.pty_prompt_attention import (
            CodexPtyAttentionBridge, PtyPromptWatcher,
        )
        return CodexPtyAttentionBridge, PtyPromptWatcher
    except ModuleNotFoundError:
        pass
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from adapters.codex.pty_prompt_attention import (
            CodexPtyAttentionBridge, PtyPromptWatcher,
        )
        return CodexPtyAttentionBridge, PtyPromptWatcher
    except ModuleNotFoundError:
        sibling = str(Path(__file__).resolve().parent)
        if sibling not in sys.path:
            sys.path.insert(0, sibling)
        from pty_prompt_attention import (  # type: ignore
            CodexPtyAttentionBridge, PtyPromptWatcher,
        )
        return CodexPtyAttentionBridge, PtyPromptWatcher


def _start_pty_attention_watcher(
    *,
    master_fd: int,
    write_lock: threading.Lock,
    log_path: str,
    runner_session_id: str,
    host_id: str,
    task_id: str,
    child_pid: int,
    child_process: Any,
) -> Any:
    """ADAPTER-31: surface known Codex TUI prompts into the Needs-you queue.

    Fail-soft: import/setup errors never take down the PTY companion. Watch and
    Chat must keep working even if attention wiring cannot load.
    """
    if str(os.environ.get("PM_CODEX_PTY_ATTENTION", "1")).strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    if not task_id:
        return None
    try:
        CodexPtyAttentionBridge, PtyPromptWatcher = _import_pty_prompt_attention()

        def write_pty(data: bytes) -> None:
            with write_lock:
                os.write(master_fd, data)

        fault_path = Path(log_path).with_name("pty_attention_fault.json")

        def on_fault(exc: BaseException) -> None:
            payload = {
                "schema": "switchboard.codex_pty_attention_fault.v1",
                "runner_session_id": runner_session_id,
                "task_id": task_id,
                "host_id": host_id,
                "error": str(exc),
                "failure_class": "failed_gate",
                "never_auto_switch_model": True,
            }
            try:
                fault_path.write_text(
                    json.dumps(payload, sort_keys=True), encoding="utf-8")
            except Exception:
                pass
            sys.stderr.write(f"pty_attention_fault:{exc}\n")
            sys.stderr.flush()
            # Visible runner fault — do not invent a model choice.
            target = int(child_pid or 0)
            if target > 0:
                try:
                    os.killpg(target, 15)
                except Exception:
                    try:
                        os.kill(target, 15)
                    except Exception:
                        pass
            if child_process is not None:
                try:
                    child_process.terminate()
                except Exception:
                    pass

        journal = str(Path(log_path).with_name("pty_attention_journal.json"))
        bridge = CodexPtyAttentionBridge(
            http=_attention_http,
            binding={
                "project": str(os.environ.get("PM_PROJECT") or "switchboard"),
                "task_id": str(task_id),
                "work_session_id": str(os.environ.get("PM_WORK_SESSION_ID") or ""),
                "runner_session_id": str(runner_session_id),
                "host_id": str(host_id or os.environ.get("PM_HOST_ID") or ""),
            },
            write_pty=write_pty,
            journal_path=journal,
            poll_interval=float(
                os.environ.get("PM_CODEX_PTY_ATTENTION_POLL_S") or 1.0),
            claim_deadline_s=float(
                os.environ.get("PM_CODEX_PTY_ATTENTION_CLAIM_DEADLINE_S") or 3600.0),
            expires_in_s=float(
                os.environ.get("PM_CODEX_PTY_ATTENTION_EXPIRES_S") or 3600.0),
        )
        watcher = PtyPromptWatcher(bridge=bridge, on_fault=on_fault)
        watcher.start_log_tail(
            log_path,
            poll_s=float(os.environ.get("PM_CODEX_PTY_ATTENTION_DETECT_S") or 0.5),
        )
        return watcher
    except Exception as exc:  # noqa: BLE001 — never brick Connect/Watch
        sys.stderr.write(
            f"pty_attention_watcher_disabled:{type(exc).__name__}:{exc}\n")
        sys.stderr.flush()
        return None


def _start_codex_telemetry_collector(
    *, child_command: list[str] | None, child_cwd: str, log_path: str,
    runner_session_id: str, task_id: str, host_id: str, started_at: float,
):
    """Attach safe rollout telemetry to an interactive Codex Connect run."""
    command = list(child_command or [])
    executable = Path(command[0]).name.lower() if command else ""
    if "codex" not in executable:
        return None
    codex_home = str(os.environ.get("CODEX_HOME") or "").strip()
    if not codex_home or not Path(codex_home).is_dir():
        return None
    try:
        from switchboard.application.codex_telemetry import (
            CodexRolloutTelemetryCollector,
            binding_from_environment,
        )
        event_path = Path(log_path).resolve().parent / "codex-telemetry.jsonl"
        collector = CodexRolloutTelemetryCollector(
            codex_home=codex_home,
            cwd=child_cwd or os.getcwd(),
            binding=binding_from_environment(
                runner_session_id=runner_session_id,
                task_id=task_id,
                host_id=host_id,
                command=command,
            ),
            event_path=event_path,
            started_at=started_at,
            poll_interval=float(
                os.environ.get("PM_CODEX_TELEMETRY_POLL_SECONDS") or 0.5),
        )
        return collector.start()
    except Exception as exc:  # noqa: BLE001 — telemetry cannot brick execution
        sys.stderr.write(
            f"codex_telemetry_collector_disabled:{type(exc).__name__}:{exc}\n")
        sys.stderr.flush()
        return None


def serve(
    *,
    master_fd: int,
    log_path: str,
    runner_session_id: str,
    host_id: str = "",
    bind_host: str = "127.0.0.1",
    port: int = 0,
    ready_path: str = "",
    task_id: str = "",
    relay_ws_url: str = "",
    child_pid: int = 0,
    child_process: Any = None,
    target_label: str = "",
    child_command: list[str] | None = None,
    child_cwd: str = "",
    initial_rows: int = 40,
    initial_cols: int = 120,
) -> int:
    child_started_at = time.time()
    slave_fd = -1
    if child_command:
        # This process is the execution authority: it allocates the PTY before
        # launch, then spawns the CLI only after legacy local tooling has bound
        # successfully. Browser Watch never uses that HTTP listener.
        master_fd, slave_fd = pty.openpty()
        set_pty_winsize(slave_fd, initial_rows, initial_cols)
    if int(master_fd) < 0:
        raise ValueError("master_fd_or_child_command_required")
    fanout = _Fanout(Path(log_path))
    write_lock = threading.Lock()
    try:
        server = ThreadingHTTPServer((bind_host, int(port)), _make_handler(
            fanout, runner_session_id, host_id, master_fd, write_lock,
            bound_task_id=str(task_id or "")))
    except Exception:
        fanout.close()
        if slave_fd >= 0:
            os.close(slave_fd)
        if child_command:
            os.close(master_fd)
        raise
    if child_command:
        try:
            child_process = subprocess.Popen(
                list(child_command),
                cwd=child_cwd or os.getcwd(),
                env=os.environ.copy(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
            child_pid = int(child_process.pid)
        except Exception:
            server.server_close()
            fanout.close()
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
    actual_port = int(server.server_address[1])
    if ready_path:
        Path(ready_path).write_text(json.dumps({
            "runner_session_id": runner_session_id,
            "bind_host": bind_host,
            "port": actual_port,
            "pid": os.getpid(),
            "executor_pid": os.getpid(),
            "child_pid": int(child_pid or 0),
            "task_id": task_id or "",
            "stream_path": f"/runner/v1/sessions/{runner_session_id}/stream",
            "inject_path": f"/runner/v1/sessions/{runner_session_id}/inject",
            "control_path": f"/runner/v1/sessions/{runner_session_id}/control",
        }), encoding="utf-8")

    host_session = {"bridge": None}
    relay_url = str(relay_ws_url or os.environ.get("PM_RUNNER_HOST_RELAY_URL") or "").strip()
    url_path = _relay_url_path(runner_session_id, ready_path)

    def _refresh_relay_url(attempt: int, reason: str) -> str:
        nonlocal relay_url
        relay_url = _request_fresh_relay_url(
            runner_session_id, host_id, url_path, relay_url)
        return relay_url

    def _log_reconnect(attempt: int, outcome: str, detail: str) -> None:
        suffix = f" detail={detail}" if detail else ""
        sys.stderr.write(
            f"host_ws_reconnect attempt={attempt} outcome={outcome}{suffix}\n")
        sys.stderr.flush()

    def _publish_relay_auth_fault(fault: dict) -> None:
        # HARDEN-79: the companion holds no coordination bearer, so the fault
        # travels the same session-directory hop the ticket refresh uses. The
        # Agent Host picks it up and puts it on the host row.
        published = relay_auth.publish_fault(url_path.parent, fault)
        sys.stderr.write(
            "host_ws_relay_auth_fault "
            f"reason={fault.get('reason')} "
            f"attempts={fault.get('attempt_count')} "
            f"first_failure_at={fault.get('first_failure_at')} "
            f"credential_source={fault.get('credential_source')} "
            f"restart_required={fault.get('restart_required')} "
            f"published={published}\n")
        sys.stderr.flush()

    auth_policy = relay_auth.RelayAuthFaultTracker(
        label=str(runner_session_id or ""),
        on_fault=_publish_relay_auth_fault)

    def _maybe_attach_host_ws() -> None:
        nonlocal relay_url
        published_url = ""
        if url_path.exists():
            try:
                published_url = url_path.read_text(encoding="utf-8").strip()
            except Exception:
                published_url = ""
        if published_url and published_url != relay_url:
            relay_url = published_url
            bridge = host_session.get("bridge")
            if bridge is not None:
                bridge.update_relay_url(relay_url)
        if host_session["bridge"] is not None:
            return
        if not relay_url:
            return
        try:
            host_session["bridge"] = _start_host_ws_executor(
                master_fd=master_fd,
                log_path=log_path,
                runner_session_id=runner_session_id,
                relay_ws_url=relay_url,
                child_pid=int(child_pid or 0),
                initial_snapshot=fanout.snapshot(),
                target_label=target_label,
                refresh_url=_refresh_relay_url,
                reconnect_log=_log_reconnect,
                auth_policy=auth_policy,
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"host_ws_attach_failed:{type(exc).__name__}:{exc}\n")

    # When the host WS attaches, stop the local select loop from consuming
    # bytes — PtyHostExecutor owns the read (+ stdout.log). Until then, pump
    # for local HTTP clients + file logging.
    def pump_until_host_ws() -> None:
        try:
            while host_session["bridge"] is None:
                _maybe_attach_host_ws()
                if host_session["bridge"] is not None:
                    break
                readable, _, _ = select.select([master_fd], [], [], 0.25)
                if not readable:
                    continue
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                fanout.publish(data)
            # Host WS executor is pumping; keep HTTP server alive for inject/CO-12
            # until the process exits (executor stop closes master_fd).
            bridge = host_session.get("bridge")
            if bridge is not None:
                while bridge.is_alive():
                    _maybe_attach_host_ws()
                    time.sleep(0.5)
        finally:
            fanout.close()
            try:
                server.shutdown()
            except Exception:
                pass

    attention_watcher = _start_pty_attention_watcher(
        master_fd=master_fd,
        write_lock=write_lock,
        log_path=log_path,
        runner_session_id=runner_session_id,
        host_id=host_id,
        task_id=str(task_id or ""),
        child_pid=int(child_pid or 0),
        child_process=child_process,
    )
    telemetry_collector = _start_codex_telemetry_collector(
        child_command=child_command,
        child_cwd=child_cwd,
        log_path=log_path,
        runner_session_id=runner_session_id,
        task_id=str(task_id or ""),
        host_id=host_id,
        started_at=child_started_at,
    )
    threading.Thread(target=pump_until_host_ws, name="pty-pump", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        if telemetry_collector is not None:
            try:
                telemetry_collector.stop()
            except Exception:
                pass
        if attention_watcher is not None:
            try:
                attention_watcher.stop()
            except Exception:
                pass
        fanout.close()
        bridge = host_session.get("bridge")
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        if child_process is not None:
            try:
                child_process.wait(timeout=1.0)
            except Exception:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PTY host executor companion (SIMPLIFY-9)")
    parser.add_argument("--runner-session-id", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--master-fd", type=int, default=-1)
    parser.add_argument("--host-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--bind-host", default=os.environ.get("PM_RUNNER_STREAM_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PM_RUNNER_STREAM_PORT", "0") or 0))
    parser.add_argument("--ready-path", default="")
    parser.add_argument("--relay-ws-url", default=os.environ.get("PM_RUNNER_HOST_RELAY_URL", ""))
    parser.add_argument("--child-command-json", default="")
    parser.add_argument("--child-cwd", default=os.getcwd())
    parser.add_argument("--initial-rows", type=int, default=40)
    parser.add_argument("--initial-cols", type=int, default=120)
    args = parser.parse_args(argv)
    master_fd = int(args.master_fd)
    command = None
    if args.child_command_json:
        command = json.loads(args.child_command_json)
        if not isinstance(command, list) or not all(isinstance(v, str) for v in command):
            raise ValueError("child_command_must_be_string_array")
    if master_fd < 0 and not command:
        raise ValueError("master_fd_or_child_command_required")
    return serve(
        master_fd=master_fd,
        log_path=args.log_path,
        runner_session_id=args.runner_session_id,
        host_id=args.host_id,
        bind_host=args.bind_host,
        port=args.port,
        ready_path=args.ready_path,
        task_id=args.task_id,
        relay_ws_url=args.relay_ws_url,
        child_command=command,
        child_cwd=args.child_cwd,
        initial_rows=max(1, min(1000, int(args.initial_rows))),
        initial_cols=max(1, min(1000, int(args.initial_cols))),
        target_label=str(
            os.environ.get("PM_AGENT_HOST_PLATFORM")
            or os.environ.get("PM_HOST_PLATFORM")
            or sys.platform),
    )


if __name__ == "__main__":
    raise SystemExit(main())
