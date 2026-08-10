#!/usr/bin/env python3
"""BUG-344: runner-scoped writes retain the runner's assigned project."""
from __future__ import annotations

import os
import tempfile

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


RUN_ID = "run-bug344"
TASK_ID = "BUG-344"
RUNNER_PROJECT = "switchboard"
DEFAULT_PROJECT = "maxwell"


def exited_runner():
    return {
        "runner_session_id": RUN_ID,
        "agent_id": "agent/codex/bug344",
        "runtime": "codex",
        "task_id": TASK_ID,
        "host_id": "host/bug344",
        "status": "running",
        "alive": False,
        "_host_project": RUNNER_PROJECT,
        "metadata": {
            "wake_id": "wake-bug344",
            "connect_assignment": True,
            "assignment_schema": "switchboard.connect.assignment.v1",
        },
    }


saved = {
    "project": agent_host.PROJECT,
    "runners": agent_host._drain_runner_projects,
    "try": agent_host._try,
}
saved_runner_dir = os.environ.get("PM_RUNNER_DIR")
posted = []

try:
    with tempfile.TemporaryDirectory(prefix="bug344-runner-") as runner_dir:
        os.environ["PM_RUNNER_DIR"] = runner_dir
        agent_host.PROJECT = DEFAULT_PROJECT
        agent_host._drain_runner_projects = lambda _inventory: [exited_runner()]

        def fake_try(method, path, body=None):
            payload = dict(body or {})
            posted.append((method, path, payload))
            if payload.get("project") != RUNNER_PROJECT:
                return {"error": "runner_not_found_in_project"}
            if path == agent_host.P_HEARTBEAT_RUNNER:
                return {"runner_session_id": RUN_ID, "status": "exited"}
            if path == agent_host.P_COMPLETE_WAKE:
                return {"status": "failed"}
            return {"ok": True}

        agent_host._try = fake_try
        outcome = agent_host.renew_live_direct_runners({"host_id": "host/bug344"})
finally:
    agent_host.PROJECT = saved["project"]
    agent_host._drain_runner_projects = saved["runners"]
    agent_host._try = saved["try"]
    if saved_runner_dir is None:
        os.environ.pop("PM_RUNNER_DIR", None)
    else:
        os.environ["PM_RUNNER_DIR"] = saved_runner_dir

terminal = next(
    body for method, path, body in posted
    if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER
)
wake = next(
    body for method, path, body in posted
    if method == "POST" and path == agent_host.P_COMPLETE_WAKE
)

assert terminal["project"] == RUNNER_PROJECT, terminal
assert wake["project"] == RUNNER_PROJECT, wake
assert outcome == [{
    "runner_session_id": RUN_ID,
    "task_id": TASK_ID,
    "wake_id": "wake-bug344",
    "terminalized": True,
    "wake_repaired": True,
}], outcome

# Late-bound Work Session validation is runner-scoped too.  The heartbeat had
# already used the central row's project; its immediate preflight refresh must
# not fall back to the daemon default after that successful binding.
saved = {
    "project": agent_host.PROJECT,
    "runners": agent_host._drain_runner_projects,
    "sessions": agent_host._drain_project_work_sessions,
    "preflight": agent_host._host_repo_preflight,
    "try": agent_host._try,
}
posted = []
try:
    agent_host.PROJECT = DEFAULT_PROJECT
    live = {
        **exited_runner(),
        "alive": True,
        "pid": 344,
        "cwd": str(ROOT),
        "_host_project": RUNNER_PROJECT,
        "metadata": {
            **exited_runner()["metadata"],
            "execution_id": "exec-bug344",
            "execution_generation": 1,
        },
    }
    agent_host._drain_runner_projects = lambda _inventory: [live]
    agent_host._drain_project_work_sessions = lambda project, **_filters: [{
        "work_session_id": "worksession-bug344",
        "claim_id": "taskclaim-bug344",
        "principal_id": f"direct-session/{RUN_ID}",
        "task_id": TASK_ID,
        "agent_id": live["agent_id"],
        "status": "active",
        "env": {
            "execution_id": "exec-bug344",
            "execution_generation": 1,
        },
    }]
    agent_host._host_repo_preflight = lambda _session, _inventory, _metadata: {
        "work_session_id": "worksession-bug344",
        "branch": "codex/BUG-344-runner-receipt-project",
        "ok": True,
    }

    def live_try(method, path, body=None):
        posted.append((method, path, dict(body or {})))
        return {"ok": True}

    agent_host._try = live_try
    agent_host.renew_live_direct_runners({"host_id": "host/bug344"})
finally:
    agent_host.PROJECT = saved["project"]
    agent_host._drain_runner_projects = saved["runners"]
    agent_host._drain_project_work_sessions = saved["sessions"]
    agent_host._host_repo_preflight = saved["preflight"]
    agent_host._try = saved["try"]

preflight = next(
    body for _method, path, body in posted
    if path.endswith("/worksession-bug344/preflight")
)
assert preflight["project"] == RUNNER_PROJECT, preflight

print("BUG-344 cross-project runner receipts: PASS")

# A receipt written by the affected release already has the assigned project in
# its durable workspace receipt. Replay repairs the stale daemon-default field
# before it calls the central terminal boundary.
saved_require = agent_host._require
posted = []
try:
    with tempfile.TemporaryDirectory(prefix="bug344-pending-") as runner_dir:
        os.environ["PM_RUNNER_DIR"] = runner_dir
        agent_host._persist_pending_stop_receipt({
            "project": DEFAULT_PROJECT,
            "runner_session_id": RUN_ID,
            "host_id": "host/bug344",
            "task_id": TASK_ID,
            "status": "stopped",
            "metadata": {
                "workspace_receipt": {"project_id": RUNNER_PROJECT},
            },
        })

        def pending_require(method, path, body=None):
            posted.append((method, path, dict(body or {})))
            return {"runner_session_id": RUN_ID, "status": "stopped"}

        agent_host._require = pending_require
        pending = agent_host._drain_pending_stop_receipts("host/bug344")
        assert not agent_host._pending_stop_receipt_path(RUN_ID).exists()
finally:
    agent_host._require = saved_require
    if saved_runner_dir is None:
        os.environ.pop("PM_RUNNER_DIR", None)
    else:
        os.environ["PM_RUNNER_DIR"] = saved_runner_dir

assert posted[0][2]["project"] == RUNNER_PROJECT, posted
assert pending[0]["expired"] is True, pending

# C3 lease surrender is reaped from every served project, not only the daemon
# default. This is the path that stops a completed cross-project runner before
# its exact terminal acknowledgement advances the task to In Review.
saved = {
    "runners": agent_host._drain_runner_projects,
    "pending": agent_host._drain_pending_stop_receipts,
    "supervisor": agent_host.supervisor_action,
    "revoke": agent_host.revoke_runner_workspace,
    "try": agent_host._try,
}
posted = []
try:
    with tempfile.TemporaryDirectory(prefix="bug344-reaper-") as runner_dir:
        os.environ["PM_RUNNER_DIR"] = runner_dir
        surrendered = {
            **exited_runner(),
            "alive": True,
            "stale": False,
            "metadata": {
                **exited_runner()["metadata"],
                "native_host_execution": True,
                "lease_surrender": {"authority": "completion_owner"},
                "workspace_receipt": {"project_id": RUNNER_PROJECT},
            },
        }
        agent_host._drain_runner_projects = lambda _inventory: [surrendered]
        agent_host._drain_pending_stop_receipts = lambda _host_id: []
        agent_host.supervisor_action = lambda *_args, **_kwargs: {"alive": False}
        agent_host.revoke_runner_workspace = lambda *_args, **_kwargs: {}

        def reaper_try(method, path, body=None):
            posted.append((method, path, dict(body or {})))
            return {"runner_session_id": RUN_ID, "status": "stopped"}

        agent_host._try = reaper_try
        expired = agent_host.expire_runner_leases({
            "host_id": "host/bug344",
            "placement": {"projects": [DEFAULT_PROJECT, RUNNER_PROJECT]},
        }, now=344.0)
finally:
    agent_host._drain_runner_projects = saved["runners"]
    agent_host._drain_pending_stop_receipts = saved["pending"]
    agent_host.supervisor_action = saved["supervisor"]
    agent_host.revoke_runner_workspace = saved["revoke"]
    agent_host._try = saved["try"]
    if saved_runner_dir is None:
        os.environ.pop("PM_RUNNER_DIR", None)
    else:
        os.environ["PM_RUNNER_DIR"] = saved_runner_dir

assert posted[0][2]["project"] == RUNNER_PROJECT, posted
assert expired[0]["terminalized"] is True, expired

# Fleet controls are project-scoped too. A host serving multiple projects must
# poll, claim, and complete the request in the project where the runner lives.
saved = {
    "try": agent_host._try,
    "supervisor": agent_host.supervisor_action,
    "revoke": agent_host.revoke_runner_workspace,
    "drop": agent_host._drop_host_bridge,
}
posted = []
try:
    def control_try(method, path, body=None):
        payload = dict(body or {})
        posted.append((method, path, payload))
        if method == "GET":
            if f"project={RUNNER_PROJECT}" in path:
                return {"requests": [{
                    "request_id": "runnerreq-bug344",
                    "runner_session_id": RUN_ID,
                    "action": "kill",
                    "options": {"reason": "operator kill"},
                }]}
            return {"requests": []}
        if path == agent_host.P_CLAIM_RUNNER_CONTROL:
            return {"claimed": True, "request": {
                "request_id": "runnerreq-bug344",
                "runner_session_id": RUN_ID,
                "action": "kill",
                "options": {"reason": "operator kill"},
            }}
        return {"ok": True}

    agent_host._try = control_try
    agent_host.supervisor_action = lambda *_args, **_kwargs: {"alive": False}
    agent_host.revoke_runner_workspace = lambda *_args, **_kwargs: {}
    agent_host._drop_host_bridge = lambda *_args, **_kwargs: None
    controls = agent_host.handle_runner_controls({
        "host_id": "host/bug344",
        "placement": {"projects": [DEFAULT_PROJECT, RUNNER_PROJECT]},
    })
finally:
    agent_host._try = saved["try"]
    agent_host.supervisor_action = saved["supervisor"]
    agent_host.revoke_runner_workspace = saved["revoke"]
    agent_host._drop_host_bridge = saved["drop"]

control_posts = [body for method, path, body in posted
                 if method == "POST" and path in {
                     agent_host.P_CLAIM_RUNNER_CONTROL,
                     agent_host.P_COMPLETE_RUNNER_CONTROL,
                 }]
assert control_posts and all(
    body["project"] == RUNNER_PROJECT for body in control_posts
), control_posts
assert controls == [{
    "request_id": "runnerreq-bug344",
    "action": "kill",
    "status": "completed",
    "runner_session_id": RUN_ID,
}], controls

print("BUG-344 pending replay, C3 reaper, and Fleet controls: PASS")
