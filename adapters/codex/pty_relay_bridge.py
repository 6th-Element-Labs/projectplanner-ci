"""DEPRECATED (SIMPLIFY-9): localhost HTTP ↔ relay bridge.

The browser Watch path no longer uses LocalPtyRelayBridge or the companion
``/stream``+``/control`` hop. Prefer ``pty_host_ws_client.PtyHostExecutor``,
which owns ``master_fd``, logs to stdout.log, and speaks one binary WS to
Switchboard. This module remains for legacy unit tests only.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

try:
    from switchboard.domain import runner_pty as domain
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_ROOT / "src"))
    from switchboard.domain import runner_pty as domain

try:
    from adapters.codex import pty_stream
except ModuleNotFoundError:
    from codex import pty_stream  # type: ignore


SendFn = Callable[[bytes], None]
OnCloseFn = Callable[[str], None]


def post_control(
    control_url: str,
    *,
    ticket: str,
    action: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    body = {"ticket": ticket, "action": action}
    if payload:
        body.update(payload)
    req = urllib.request.Request(
        control_url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            detail = {"error": "http_error", "status": exc.code}
        detail.setdefault("status", exc.code)
        return detail


def apply_relay_control_frame(
    control_url: str,
    ticket: str,
    frame: str | bytes | dict[str, Any],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Map a browser→host relay frame onto the companion `/control` POST."""
    decoded = domain.decode_frame(frame)
    kind = decoded["type"]
    if kind in {"input", "in"}:
        payload: dict[str, Any] = {}
        if isinstance(decoded.get("data"), (bytes, bytearray)):
            import base64
            payload["data_b64"] = base64.b64encode(decoded["data"]).decode("ascii")
        elif decoded.get("data_b64"):
            payload["data_b64"] = decoded["data_b64"]
        elif isinstance(decoded.get("text"), str):
            payload["text"] = decoded["text"]
        else:
            return {"ok": False, "error": "invalid_input", "reason": "data_required"}
        return post_control(control_url, ticket=ticket, action="input",
                            payload=payload, timeout=timeout)
    if kind == "resize":
        return post_control(
            control_url,
            ticket=ticket,
            action="resize",
            payload={
                "rows": decoded.get("rows") or decoded.get("row"),
                "cols": decoded.get("cols") or decoded.get("col") or decoded.get("columns"),
            },
            timeout=timeout,
        )
    if kind == "signal":
        return post_control(
            control_url,
            ticket=ticket,
            action="signal",
            payload={"name": decoded.get("name") or decoded.get("signal") or "SIGINT"},
            timeout=timeout,
        )
    return {"ok": False, "error": "unsupported_frame", "reason": kind}


def pump_chunked_stream(
    stream_url: str,
    on_bytes: Callable[[bytes], None],
    *,
    stop_event: threading.Event | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Read an HTTP chunked local stream and invoke ``on_bytes`` for each chunk."""
    stop = stop_event or threading.Event()
    req = urllib.request.Request(stream_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # HTTPResponse.read(n) waits for exactly n bytes (or EOF).  A live
            # PTY rarely emits 4 KiB at once, so using read(4096) made short
            # prompts and command output appear frozen in the browser until
            # enough unrelated output accumulated.  read1() returns the bytes
            # currently available from the chunked response and preserves the
            # low-latency terminal-stream contract.
            read_available = getattr(resp, "read1", resp.read)
            while not stop.is_set():
                chunk = read_available(4096)
                if not chunk:
                    break
                on_bytes(chunk)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    return {"ok": True}


class LocalPtyRelayBridge:
    """Threaded bridge: local stream → relay output frames; control frames → /control."""

    def __init__(
        self,
        *,
        stream_url: str,
        control_url: str,
        control_ticket: str,
        send_to_relay: SendFn,
        on_close: OnCloseFn | None = None,
    ):
        self.stream_url = stream_url
        self.control_url = control_url
        self.control_ticket = control_ticket
        self.send_to_relay = send_to_relay
        self.on_close = on_close
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _run() -> None:
            def on_bytes(data: bytes) -> None:
                if not data or self._stop.is_set():
                    return
                frame = domain.encode_frame("out", data=data)
                try:
                    self.send_to_relay(frame)
                except Exception:
                    self._stop.set()

            result = pump_chunked_stream(
                self.stream_url, on_bytes, stop_event=self._stop, timeout=3600)
            reason = "eof" if result.get("ok") else str(result.get("error") or "stream_error")
            try:
                self.send_to_relay(domain.encode_frame(
                    "exit",
                    {"reason": reason, "message": result.get("message") or ""},
                ))
            except Exception:
                pass
            if self.on_close:
                try:
                    self.on_close(reason)
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, name="pty-relay-bridge", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        # on_close (called from _run, above) can itself trigger stop() via a
        # caller's cleanup callback - that's this same thread joining itself,
        # which raises RuntimeError. Setting _stop is enough in that case:
        # _run() is already unwinding and will exit on its own.
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def handle_control_frame(self, frame: str | bytes | dict[str, Any]) -> dict[str, Any]:
        decoded = domain.decode_frame(frame)
        result = apply_relay_control_frame(
            self.control_url, self.control_ticket, decoded)
        request_id = str(decoded.get("request_id") or "").strip()
        if request_id:
            acknowledged = bool(result.get("ok")) and not result.get("error")
            ack = domain.encode_frame("ready", {
                "request_id": request_id,
                "action": decoded.get("type") or "",
                "ok": acknowledged,
                "ack": True,
                "error": result.get("error") or result.get("reason") or "",
            })
            try:
                self.send_to_relay(ack)
            except Exception:
                self._stop.set()
        return result


def build_local_bridge_urls(
    *,
    bind_host: str,
    port: int,
    runner_session_id: str,
    stream_ticket: str,
) -> dict[str, str]:
    return {
        "stream_url": pty_stream.build_stream_url(
            bind_host=bind_host, port=port,
            runner_session_id=runner_session_id, ticket=stream_ticket),
        "control_url": pty_stream.build_control_url(
            bind_host=bind_host, port=port,
            runner_session_id=runner_session_id),
        "inject_url": pty_stream.build_inject_url(
            bind_host=bind_host, port=port,
            runner_session_id=runner_session_id),
    }


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0,
               interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
