#!/usr/bin/env python3
"""WATCH-13: claim binding and relay reconnect cannot make Watch go dark."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest import mock

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="watch13-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "watch13-secret"

import bridge_attachment_monitor as monitor  # noqa: E402
import store  # noqa: E402
from adapters.codex import pty_host_ws_client  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


P = "switchboard"
store.init_db(P)
task = store.create_task({
    "workstream_id": "WATCH", "title": "WATCH-13 lifecycle fixture",
    "status": "Not Started", "ui_impact": "yes",
}, actor="watch13", project=P)
task_id = task["task_id"]
agent_id = f"agent/codex/{task_id.lower()}"
principal = "principal/watch13"
work_session = store.create_work_session({
    "task_id": task_id, "agent_id": agent_id, "runtime": "codex",
    "repo_role": "canonical", "branch": f"codex/{task_id}-fixture",
    "upstream": "origin/master", "base_sha": "a" * 40, "head_sha": "a" * 40,
    "worktree_path": str(ROOT), "storage_mode": "worktree", "status": "active",
    "dirty_status": "clean", "policy_profile": "code_strict",
    "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
}, actor="watch13", principal_id=principal, project=P)["work_session"]
claim = store.claim_task(
    task_id, agent_id, actor="watch13", principal_id=principal, project=P,
    work_session_id=work_session["work_session_id"],
    session_policy_profile="code_strict", require_work_session=True)
assert claim["claimed"] is True

runner_id = "run_watch13"
host_id = "host/watch13"
row = store.upsert_runner_session({
    "runner_session_id": runner_id, "task_id": task_id,
    "claim_id": claim["claim_id"], "host_id": host_id, "agent_id": agent_id,
    "runtime": "codex", "status": "running",
    "control": {"managed_process": True, "runner_open": True},
    "metadata": {"wake_id": "wake-watch13", "pty": True,
                 "native_host_execution": True, "connect_assignment": True,
                 "work_session_id": work_session["work_session_id"]},
}, actor=host_id, principal_id="principal/watch13-host", project=P)

# Full lifecycle pin: after claim + Work Session binding, the next heartbeat relay
# options retain the real IDs and still mint a host capability.
minted = runner_repo._server_relay_options(row, user_id="user/watch13", project=P)
assert minted.get("host_url"), minted
assert minted["binding"]["claim_id"] == claim["claim_id"]
assert minted["binding"]["work_session_id"] == work_session["work_session_id"]

hub = relay.reset_default_hub_for_tests()
browser_frames: list[bytes] = []
host_ticket, host_payload = relay.mint_host_tunnel_ticket(minted["binding"], ttl_seconds=60)
browser_ticket, browser_payload = relay.mint_capability_ticket(
    minted["binding"], ["watch"], ttl_seconds=60)
assert host_ticket and browser_ticket
assert hub.attach_host(runner_id, lambda _frame: None, binding=host_payload)["ok"]
assert hub.attach_browser(
    runner_id, browser_payload, browser_frames.append, client_id="browser-watch13")["ok"]
hub.publish_output(runner_id, b"before-drop\n")
assert any(domain.decode_frame(frame).get("data") == b"before-drop\n"
           for frame in browser_frames)

# Socket loss must raise the WATCH-6 signal while detached and clear immediately
# after reattachment. A zero window makes this deterministic while exercising the
# production monitor transition logic.
events: list[dict] = []
sessions = lambda _project: [row]
attached = lambda _sid: relay.host_attached_for(runner_id, hub=hub)
emit = lambda project, **payload: events.append({"project": project, **payload})
monitor.reset_for_tests()
hub.detach_host(runner_id)
raised = monitor.snapshot(P, sessions_provider=sessions,
                          attachment_provider=attached, event_sink=emit,
                          now=1000, window_s=0)
assert raised["active"] is True and events[-1]["active"] is True

# Reconnect with a freshly minted ticket and prove output resumes on the existing
# browser attachment; expiry of the old ticket is therefore a non-event.
fresh_host_ticket, fresh_host_payload = relay.mint_host_tunnel_ticket(
    minted["binding"], ttl_seconds=60, now=float(host_payload["exp"]) + 1)
assert fresh_host_ticket and fresh_host_payload["jti"] != host_payload["jti"]
assert hub.attach_host(runner_id, lambda _frame: None, binding=fresh_host_payload)["ok"]
cleared = monitor.snapshot(P, sessions_provider=sessions,
                           attachment_provider=attached, event_sink=emit,
                           now=1001, window_s=0)
assert cleared["active"] is False and events[-1]["active"] is False
hub.publish_output(runner_id, b"after-reconnect\n")
assert any(domain.decode_frame(frame).get("data") == b"after-reconnect\n"
           for frame in browser_frames)


class _Socket:
    def __init__(self, fail=False):
        self.fail = fail
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_args):
        return False
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self.fail:
            self.fail = False
            raise ConnectionError("forced relay drop")
        raise StopAsyncIteration
    async def close(self):
        return None


# Pin the companion's bounded automatic reconnect path separately from the hub
# state transition above: the second dial uses the refreshed URL.
urls: list[str] = []
sockets = [_Socket(fail=True), _Socket()]
connection = pty_host_ws_client.HostTunnelConnection(
    "wss://plan.example/expired", require_initial=False,
    refresh_url=lambda _attempt, _reason: "wss://plan.example/fresh")
connection.on_connect = lambda: connection._stopped.set() if len(urls) > 1 else None
def connect(url, **_kwargs):
    urls.append(url)
    return sockets.pop(0)
async def no_sleep(_seconds):
    return None
with mock.patch.object(pty_host_ws_client.websockets, "connect", connect), \
        mock.patch.object(pty_host_ws_client.asyncio, "sleep", no_sleep):
    asyncio.run(connection._main())
assert urls == ["wss://plan.example/expired", "wss://plan.example/fresh"]

print("WATCH-13 lifecycle + chaos reconnect: passed")
