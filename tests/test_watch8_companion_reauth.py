#!/usr/bin/env python3
"""WATCH-8: relay reconnects refresh an expired host ticket."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest import mock

from path_setup import ROOT  # noqa: F401

from adapters.codex import pty_host_ws_client, pty_stream


class _Socket:
    def __init__(self, *, fail=False, on_enter=None):
        self.fail = fail
        self.on_enter = on_enter

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.fail:
            self.fail = False
            raise ConnectionError("socket dropped")
        raise StopAsyncIteration

    async def close(self):
        return None


urls = []
logs = []
conn = pty_host_ws_client.HostTunnelConnection(
    "wss://relay.example/expired",
    require_initial=False,
    refresh_url=lambda attempt, reason: "wss://relay.example/fresh",
    reconnect_log=lambda attempt, outcome, detail: logs.append(
        (attempt, outcome, detail)),
)
conn.on_connect = lambda: conn._stopped.set() if len(urls) > 1 else None
sockets = [
    _Socket(fail=True),
    _Socket(),
]


def _connect(url, **_kwargs):
    urls.append(url)
    return sockets.pop(0)


async def _no_sleep(_seconds):
    return None


with mock.patch.object(pty_host_ws_client.websockets, "connect", _connect), \
        mock.patch.object(pty_host_ws_client.asyncio, "sleep", _no_sleep):
    asyncio.run(conn._main())

assert urls == ["wss://relay.example/expired", "wss://relay.example/fresh"], urls
assert (1, "connected", "") in logs, logs


with tempfile.TemporaryDirectory() as tmp:
    url_path = Path(tmp) / "host_relay.url"
    url_path.write_text("wss://relay.example/expired", encoding="utf-8")

    def _publish(_seconds):
        url_path.write_text("wss://relay.example/new-ticket", encoding="utf-8")

    with mock.patch.object(pty_stream.time, "sleep", _publish):
        fresh = pty_stream._request_fresh_relay_url(
            "run-watch8", "host/watch8", url_path,
            "wss://relay.example/expired")
    request = json.loads(
        (Path(tmp) / "host_relay.refresh").read_text(encoding="utf-8"))

assert fresh.endswith("new-ticket"), fresh
assert request["runner_session_id"] == "run-watch8"
assert request["host_id"] == "host/watch8"

print("WATCH-8 companion reconnect reauth: passed")
