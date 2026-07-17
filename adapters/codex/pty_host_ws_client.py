"""Host-side WebSocket transport for the ADAPTER-22 browser PTY relay (UI-24).

pty_relay_bridge.LocalPtyRelayBridge already pumps the local CO-12 PTY stream
into relay frames and maps relay control frames onto the local /control
endpoint; production never wrapped it in a real transport, so RelayHub's
attach_host() was only ever exercised by tests. This module is that
transport: a websockets client for /pty/host, run on its own thread and
event loop so the synchronous agent_host.py daemon never has to become
asyncio-aware.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Optional

import websockets

try:
    from adapters.codex.pty_relay_bridge import LocalPtyRelayBridge
except ModuleNotFoundError:
    from codex.pty_relay_bridge import LocalPtyRelayBridge  # type: ignore


class HostTunnelConnection:
    """A websockets client for /pty/host, isolated to its own thread/loop."""

    def __init__(self, url: str, *, on_frame: Optional[Callable[[str], None]] = None):
        self.url = url
        self.on_frame = on_frame
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._connect_error: Optional[BaseException] = None
        self._gave_up = threading.Event()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._stopped.is_set()

    def start(self, timeout: float = 10.0) -> None:
        self._thread = threading.Thread(
            target=self._run, name="pty-host-tunnel-ws", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            # Tell a still-connecting _main() to close and stop rather than
            # pump messages nobody is listening for - otherwise a connect
            # that succeeds moments after we give up here leaks its socket
            # and event-loop thread forever (nothing else references it).
            self._gave_up.set()
            raise TimeoutError("host_tunnel_connect_timeout")
        if self._connect_error is not None:
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
        try:
            async with websockets.connect(self.url, max_size=None) as ws:
                if self._gave_up.is_set():
                    return
                self._ws = ws
                self._ready.set()
                async for message in ws:
                    if self.on_frame is not None:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self.on_frame, message)
        except Exception as exc:  # noqa: BLE001
            self._connect_error = exc
            self._ready.set()
        finally:
            self._ws = None

    def send(self, frame: str, timeout: float = 5.0) -> None:
        """Thread-safe send from any thread (the bridge's pump thread calls this).

        Raises on failure instead of returning a bool: the caller
        (LocalPtyRelayBridge.on_bytes) only reacts to an exception to detect
        a dead tunnel and stop its pump thread - swallowing failures here
        would make that detection dead code and drop output forever.
        """
        loop, ws = self._loop, self._ws
        if loop is None or ws is None:
            raise ConnectionError("host_tunnel_not_connected")
        future = asyncio.run_coroutine_threadsafe(ws.send(frame), loop)
        future.result(timeout=timeout)

    def stop(self, timeout: float = 3.0) -> None:
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop).result(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        self._stopped.wait(timeout=timeout)


class HostBridgeSession:
    """Owns one LocalPtyRelayBridge + one HostTunnelConnection for a runner_session_id."""

    def __init__(self, runner_session_id: str, bridge: LocalPtyRelayBridge,
                 conn: HostTunnelConnection):
        self.runner_session_id = runner_session_id
        self.bridge = bridge
        self.conn = conn

    def is_alive(self) -> bool:
        return self.bridge.is_alive() and self.conn.connected

    def stop(self) -> None:
        self.bridge.stop()
        self.conn.stop()


def open_host_bridge(
    *,
    runner_session_id: str,
    relay_ws_url: str,
    local_stream_url: str,
    local_control_url: str,
    control_ticket: str,
    on_close: Optional[Callable[[str], None]] = None,
) -> HostBridgeSession:
    """Connect the host tunnel, then start pumping local PTY bytes into it.

    Connects first and raises on failure so the pump thread never starts
    against a dead relay connection and silently drops early output.
    """
    conn = HostTunnelConnection(relay_ws_url)
    bridge = LocalPtyRelayBridge(
        stream_url=local_stream_url,
        control_url=local_control_url,
        control_ticket=control_ticket,
        send_to_relay=conn.send,
        on_close=on_close,
    )
    conn.on_frame = bridge.handle_control_frame
    conn.start()
    bridge.start()
    return HostBridgeSession(runner_session_id, bridge, conn)
