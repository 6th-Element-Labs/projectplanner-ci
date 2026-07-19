#!/usr/bin/env python3
"""BUG-87: a no-claim direct Mac runner may acknowledge its exact wake."""
from __future__ import annotations

import hashlib
import os
import tempfile

from path_setup import ROOT  # noqa: F401

tmp = tempfile.mkdtemp(prefix="bug87-direct-completion-")
os.environ.update({
    "PM_DB_PATH": os.path.join(tmp, "maxwell.db"),
    "PM_HELM_DB_PATH": os.path.join(tmp, "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": os.path.join(tmp, "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": os.path.join(tmp, "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": tmp,
})

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


project = "switchboard"
host_id = "host/direct-mac"
principal_id = "principal/direct-mac"
store.init_project_registry()
store.init_db(project)
task = store.create_task(
    {"workstream_id": "BUG", "title": "Direct completion proof"},
    actor="test", project=project)
task_id = task["task_id"]
agent_id = f"codex/{task_id}"
wake = store.request_wake(
    selector={
        "runtime": "codex", "lane": "BUG", "agent_id": agent_id,
        "task_id": task_id, "host_id": host_id,
    },
    policy={
        "mode": "direct_task", "execution_mode": "direct_personal_cli",
        "require_runner_bind": False,
        "assignment": {"schema": "switchboard.direct_cli_assignment.v1"},
    },
    task_id=task_id, reason="test", source="test", actor="test",
    project=project,
)
runner_id = "run_" + hashlib.sha256(
    f"{wake['wake_id']}:{host_id}".encode()).hexdigest()[:16]
store.upsert_runner_session({
    "runner_session_id": runner_id,
    "host_id": host_id,
    "agent_id": agent_id,
    "runtime": "codex",
    "task_id": task_id,
    "status": "running",
    "cwd": str(ROOT),
    "metadata": {
        "wake_id": wake["wake_id"],
        "direct_assignment": True,
        "assignment_schema": "switchboard.direct_cli_assignment.v1",
    },
}, principal_id=principal_id, actor=host_id, project=project)
binding = {
    "wake_id": wake["wake_id"], "host_id": host_id,
    "runner_session_id": runner_id, "task_id": task_id,
    "agent_id": agent_id,
}
allowed = store.check_direct_task_completion_authority(
    binding, principal_id=principal_id, project=project)
ok(allowed.get("allowed") is True,
   "selected host with the exact live direct runner may complete the wake")
unchanged_task = store.get_task(task_id, project=project)
ok(unchanged_task.get("assignee") in (None, "")
   and unchanged_task.get("status") == "Not Started",
   "authorization does not create or require a scheduler task claim")
wrong_host = store.check_direct_task_completion_authority(
    {**binding, "host_id": "host/other"},
    principal_id=principal_id, project=project)
ok(wrong_host.get("allowed") is False
   and "wake_host_id_mismatch" in wrong_host.get("reason_codes", []),
   "another host cannot acknowledge the selected Mac wake")
wrong_principal = store.check_direct_task_completion_authority(
    binding, principal_id="principal/other", project=project)
ok(wrong_principal.get("allowed") is False
   and "runner_principal_id_mismatch" in wrong_principal.get("reason_codes", []),
   "another bearer cannot acknowledge the registered direct runner")

print(f"\nBUG-87 direct wake completion: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
