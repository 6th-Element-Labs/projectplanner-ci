"""Host-side WebSocket transport for the SIMPLIFY-9 single-session PTY relay.

Architecture target:
  Browser --WSS binary--> RelayHub <--WSS binary-- Host executor (owns master_fd)

The executor speaks ONE outbound WS to Switchboard. No localhost HTTP
``/stream``+``/control`` hop and no ``LocalPtyRelayBridge`` on the Watch path.
PTY I/O rides binary frames (out/in/resize/signal/ready/exit/snapshot) with
8–16ms write coalescing and reconnect that reattaches without closing the hub
session.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import random
import select
import struct
import termios
import threading
import time
from typing import Callable, Optional

import websockets

try:
    from switchboard.domain import runner_pty as domain
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_ROOT / "src"))
    from switchboard.domain import runner_pty as domain


OnCloseFn = Callable[[str], None]
OnFrameFn = Callable[[bytes], None]
RefreshUrlFn = Callable[[int, str], str]
ReconnectLogFn = Callable[[int, str, str], None]


class HostTunnelConnection:
    """A websockets client for /pty/host, isolated to its own thread/loop."""

    def __init__(
        self,
        url: str,
        *,
        on_frame: Optional[OnFrameFn] = None,
        coalesce_ms: float = domain.DEFAULT_WRITE_COALESCE_MS,
        require_initial: bool = True,
        refresh_url: Optional[RefreshUrlFn] = None,
        reconnect_log: Optional[ReconnectLogFn] = None,
    ):
        self.url = str(url or "")
        self.on_frame = on_frame
        self.on_connect: Optional[Callable[[], None]] = None
        self.coalesce_ms = max(8.0, min(16.0, float(coalesce_ms)))
        self.require_initial = bool(require_initial)
        self.refresh_url = refresh_url
        self.reconnect_log = reconnect_log
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._stopped = threading.Event()
        self._connect_error: Optional[BaseException] = None
        self._gave_up = threading.Event()
        self._pause = threading.Event()
        self._url_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._stopped.is_set()

    def start(self, timeout: float = 10.0) -> None:
        self._thread = threading.Thread(
            target=self._run, name="pty-host-tunnel-ws", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            self._gave_up.set()
            raise TimeoutError("host_tunnel_connect_timeout")
        if self.require_initial and self._connect_error is not None:
            raise self._connect_error

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            self._stopped.set()
            self._ready.set()

    async def _main(self) -> None:
        # Symmetric reconnect: keep dialing until stop() — hub session stays open.
        backoff = 0.5
        reconnect_attempt = 0
        reconnect_reason = ""
        while not self._stopped.is_set() and not self._gave_up.is_set():
            if reconnect_reason:
                reconnect_attempt += 1
                if self.refresh_url is not None:
                    try:
                        loop = asyncio.get_running_loop()
                        fresh_url = await loop.run_in_executor(
                            None, self.refresh_url, reconnect_attempt, reconnect_reason)
                        if fresh_url:
                            self.update_url(fresh_url)
                    except Exception as exc:  # noqa: BLE001
                        if self.reconnect_log is not None:
                            self.reconnect_log(
                                reconnect_attempt, "refresh_failed", type(exc).__name__)
            try:
                with self._url_lock:
                    target_url = self.url
                if not target_url:
                    raise ConnectionError("host_tunnel_url_required")
                async with websockets.connect(target_url, max_size=None) as ws:
                    if self._gave_up.is_set() or self._stopped.is_set():
                        return
                    self._ws = ws
                    self._connected.set()
                    self._connect_error = None
                    self._ready.set()
                    backoff = 0.5
                    if reconnect_attempt and self.reconnect_log is not None:
                        self.reconnect_log(reconnect_attempt, "connected", "")
                    if self.on_connect is not None:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self.on_connect)
                    async for message in ws:
                        if self._stopped.is_set():
                            return
                        raw = message if isinstance(message, (bytes, bytearray)) else bytes(message)
                        if self.on_frame is not None:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(None, self.on_frame, bytes(raw))
                    raise ConnectionError("host_tunnel_socket_closed")
            except Exception as exc:  # noqa: BLE001
                self._connect_error = exc
                self._ws = None
                self._connected.clear()
                if not self._ready.is_set():
                    # Surface initial state to start(). Executors keep retrying;
                    # fail-closed callers may opt into the old one-shot gate.
                    self._ready.set()
                    if self.require_initial:
                        return
                if self._stopped.is_set() or self._gave_up.is_set():
                    return
                if reconnect_attempt and self.reconnect_log is not None:
                    self.reconnect_log(
                        reconnect_attempt, "connect_failed", type(exc).__name__)
                reconnect_reason = type(exc).__name__
                await asyncio.sleep(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 8.0)
            finally:
                self._ws = None
                self._connected.clear()

    def send(self, frame: bytes, timeout: float | None = 5.0) -> None:
        """Thread-safe lossless send across symmetric reconnects.

        Waiting here is intentional: it stops the executor's PTY master reads,
        allowing WebSocket/TCP backpressure to propagate without dropping data.
        """
        deadline = (
            None if timeout is None
            else time.monotonic() + max(0.1, float(timeout))
        )
        last_error: BaseException | None = None
        while not self._stopped.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            wait_for = 0.25 if remaining is None else min(remaining, 0.25)
            if not self._connected.wait(timeout=wait_for):
                continue
            loop, ws = self._loop, self._ws
            if loop is None or ws is None:
                self._connected.clear()
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(ws.send(bytes(frame)), loop)
                future.result(timeout=remaining)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._connected.clear()
        if last_error is not None:
            raise ConnectionError("host_tunnel_send_failed") from last_error
        raise ConnectionError("host_tunnel_not_connected")

    def send_out(self, data: bytes) -> None:
        """Send one already-coalesced output chunk without an unbounded buffer."""
        if not data:
            return
        self.send(domain.encode_frame("out", data=bytes(data)), timeout=None)

    def update_url(self, url: str) -> None:
        """Rotate an expiring host ticket without replacing the executor."""
        target = str(url or "").strip()
        if not target:
            return
        with self._url_lock:
            if target == self.url:
                return
            self.url = target
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:  # noqa: BLE001
                pass

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._pause.set()
        else:
            self._pause.clear()

    def stop(self, timeout: float = 3.0) -> None:
        self._stopped.set()
        self._connected.clear()
        self._pause.clear()
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop).result(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        self._stopped.wait(timeout=timeout)


class PtyHostExecutor:
    """Owns master_fd I/O and maps browser frames onto the local PTY."""

    def __init__(
        self,
        *,
        master_fd: int,
        conn: HostTunnelConnection,
        log_path: str = "",
        on_close: Optional[OnCloseFn] = None,
        child_pid: int = 0,
        target_label: str = "",
    ):
        self.master_fd = int(master_fd)
        self.conn = conn
        self.log_path = str(log_path or "")
        self.on_close = on_close
        self.child_pid = int(child_pid or 0)
        self.target_label = str(target_label or os.environ.get(
            "PM_AGENT_HOST_PLATFORM") or os.environ.get("PM_HOST_PLATFORM") or "host")
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._log_fp = None

    def start(self) -> None:
        if self.log_path:
            self._log_fp = open(self.log_path, "ab", buffering=0)
        self.conn.on_frame = self.handle_control_frame
        self.conn.on_connect = self._send_ready
        self._thread = threading.Thread(
            target=self._pump, name="pty-host-executor", daemon=True)
        self._thread.start()
        self._send_ready()

    def _send_ready(self) -> None:
        try:
            self.conn.send(domain.encode_frame(
                "ready",
                {"pid": self.child_pid or os.getpid(), "target_label": self.target_label},
            ))
        except Exception:  # noqa: BLE001
            pass

    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stopped.is_set()
        )

    def handle_control_frame(self, frame: bytes) -> None:
        try:
            decoded = domain.decode_frame(frame)
        except ValueError:
            return
        kind = decoded.get("type")
        if kind == "in":
            data = decoded.get("data") or b""
            if data:
                os.write(self.master_fd, bytes(data))
            request_id = decoded.get("request_id")
            if request_id:
                try:
                    self.conn.send(domain.encode_frame(
                        "ready",
                        {"request_id": request_id, "ok": True, "ack": True},
                    ))
                except Exception:  # noqa: BLE001
                    pass
        elif kind == "resize":
            rows = int(decoded.get("rows") or decoded.get("row") or 24)
            cols = int(decoded.get("cols") or decoded.get("col")
                       or decoded.get("columns") or 80)
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:  # noqa: BLE001
                pass
        elif kind == "signal":
            name = str(decoded.get("name") or "SIGINT").upper()
            sig = getattr(__import__("signal"), name, None)
            if sig is not None and self.child_pid > 0:
                try:
                    os.killpg(self.child_pid, int(sig))
                except Exception:  # noqa: BLE001
                    try:
                        os.kill(self.child_pid, int(sig))
                    except Exception:  # noqa: BLE001
                        pass
        elif kind == "exit":
            self.stop(reason=str(decoded.get("reason") or "exit"))

    def _pump(self) -> None:
        fd = self.master_fd
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:  # noqa: BLE001
            pass
        reason = "eof"
        try:
            while not self._stopped.is_set():
                # Slow host reads when the hub signals backpressure.
                while self.conn._pause.is_set() and not self._stopped.is_set():
                    time.sleep(self.conn.coalesce_ms / 1000.0)
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except Exception:  # noqa: BLE001
                    break
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 8192)
                except OSError as exc:
                    if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                        continue
                    reason = "read_error"
                    break
                if not chunk:
                    reason = "eof"
                    break
                # Coalesce only within the bounded 8–16ms window. The pending
                # bytes remain local and bounded while the tunnel reconnects.
                pending = bytearray(chunk)
                deadline = time.monotonic() + self.conn.coalesce_ms / 1000.0
                while len(pending) < domain.MAX_DATA_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        more_ready, _, _ = select.select([fd], [], [], remaining)
                    except Exception:  # noqa: BLE001
                        break
                    if not more_ready:
                        break
                    try:
                        more = os.read(fd, min(8192, domain.MAX_DATA_BYTES - len(pending)))
                    except OSError as exc:
                        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            break
                        reason = "read_error"
                        more = b""
                    if not more:
                        break
                    pending.extend(more)
                chunk = bytes(pending)
                if self._log_fp is not None:
                    try:
                        self._log_fp.write(chunk)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self.conn.send_out(chunk)
                except Exception:  # noqa: BLE001
                    reason = "tunnel_dead"
                    break
        finally:
            self._stopped.set()
            if self.on_close is not None:
                try:
                    self.on_close(reason)
                except Exception:  # noqa: BLE001
                    pass

    def stop(self, reason: str = "stopped") -> None:
        self._stopped.set()
        self.conn.set_paused(False)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fp = None
        try:
            self.conn.send(domain.encode_frame("exit", {"reason": reason}))
        except Exception:  # noqa: BLE001
            pass
        self.conn.stop()


class HostBridgeSession:
    """One host tunnel (+ optional PTY executor) for a runner_session_id."""

    def __init__(
        self,
        runner_session_id: str,
        conn: HostTunnelConnection,
        executor: Optional[PtyHostExecutor] = None,
        *,
        handoff: bool = False,
    ):
        self.runner_session_id = runner_session_id
        self.conn = conn
        self.executor = executor
        self.handoff = bool(handoff)
        self._stopped = False

    def is_alive(self) -> bool:
        if self._stopped:
            return False
        if self.handoff:
            # Companion owns the real WS; registry entry stays until drop.
            return True
        if self.executor is not None:
            return self.executor.is_alive()
        return self.conn.connected

    def stop(self) -> None:
        self._stopped = True
        if self.executor is not None:
            self.executor.stop()
        elif not self.handoff:
            self.conn.stop()

    def update_relay_url(self, relay_ws_url: str) -> None:
        if not self.handoff:
            self.conn.update_url(relay_ws_url)


def open_host_bridge(
    *,
    runner_session_id: str,
    relay_ws_url: str,
    master_fd: int | None = None,
    child_pid: int = 0,
    log_path: str = "",
    on_close: Optional[OnCloseFn] = None,
    coalesce_ms: float = domain.DEFAULT_WRITE_COALESCE_MS,
    dial: bool | None = None,
    target_label: str = "",
    refresh_url: Optional[RefreshUrlFn] = None,
    reconnect_log: Optional[ReconnectLogFn] = None,
) -> HostBridgeSession:
    """Open the host side of the session transport.

    No localhost stream/control URLs — starting the executor *is* opening.

    When ``master_fd`` is provided, this process dials Switchboard and pumps
    PTY I/O. When only a relay URL is known (agent_host handoff), we record the
    URL for the executor companion and return a live registry handle without
    attaching a second host tunnel (the companion is the single WS speaker).
    """
    should_dial = bool(dial) if dial is not None else (master_fd is not None)
    if not should_dial:
        # Handoff handle: companion will dial after reading host_relay.url.
        conn = HostTunnelConnection(relay_ws_url, coalesce_ms=coalesce_ms)
        conn._ready.set()
        return HostBridgeSession(runner_session_id, conn, None, handoff=True)

    conn = HostTunnelConnection(
        relay_ws_url,
        coalesce_ms=coalesce_ms,
        require_initial=False,
        refresh_url=refresh_url,
        reconnect_log=reconnect_log,
    )
    conn.start()
    executor = None
    if master_fd is not None:
        executor = PtyHostExecutor(
            master_fd=master_fd,
            conn=conn,
            log_path=log_path,
            on_close=on_close,
            child_pid=child_pid,
            target_label=target_label,
        )
        executor.start()
    return HostBridgeSession(runner_session_id, conn, executor)
