#!/usr/bin/env python3
"""BUG-154: complete_claim surrenders only its bound runner generation."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug154-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402


P = "switchboard"


def make_claim(title: str):
    task = store.create_task({
        "workstream_id": "BUG", "title": title, "status": "Not Started",
        "ui_impact": "no",
    }, actor="bug154-test", project=P)
    claim = store.claim_task(
        task["task_id"], f"agent/{task['task_id']}",
        session_policy_profile="docs_review", actor="bug154-test", project=P)
    assert claim["claimed"] is True
    return task, claim


store.init_db(P)
task, claim = make_claim("surrender exact runner generation")
other_task, other_claim = make_claim("preserve another generation")


def register(runner_id: str, task_id: str, claim_id: str):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/bug154",
        "agent_id": f"agent/{task_id}", "runtime": "codex",
        "task_id": task_id, "claim_id": claim_id, "status": "running",
        "heartbeat_ttl_s": 60,
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": f"ws-{claim_id}"},
    }, actor="host/bug154", project=P)


bound = register("run-bug154-bound", task["task_id"], claim["claim_id"])
other = register("run-bug154-other", other_task["task_id"], other_claim["claim_id"])
assert not bound.get("stale") and not other.get("stale")

completed = store.complete_claim(
    claim["claim_id"], evidence={"artifact_or_review_note": "BUG-154 regression"},
    actor="bug154-test", project=P)
assert completed["completed"] is True

surrendered = store.get_runner_session("run-bug154-bound", project=P)
unrelated = store.get_runner_session("run-bug154-other", project=P)
assert surrendered["stale"] is True
assert surrendered["metadata"]["lease_surrender"]["claim_id"] == claim["claim_id"]
assert unrelated["stale"] is False
assert "lease_surrender" not in unrelated["metadata"]

# A late host heartbeat must neither renew nor resurrect the fenced generation.
prior_heartbeat = surrendered["heartbeat_at"]
late = register("run-bug154-bound", task["task_id"], claim["claim_id"])
assert late["stale"] is True
assert late["heartbeat_at"] == prior_heartbeat

# The existing expiry clock remains the only automatic terminalization path.
applied = store.apply_cleanup(
    project=P, include_kinds=["runner_session"], dry_run=False, now=time.time())
assert any(row["id"].endswith("run-bug154-bound") and row["applied"]
           for row in applied["results"])
assert store.get_runner_session("run-bug154-bound", project=P)["status"] == "expired"
assert store.get_runner_session("run-bug154-other", project=P)["status"] == "running"

# A lease-expiry terminal heartbeat is allowed through the fence and is idempotent.
terminal = store.upsert_runner_session({
    "runner_session_id": "run-bug154-bound", "host_id": "host/bug154",
    "agent_id": f"agent/{task['task_id']}", "runtime": "codex",
    "task_id": task["task_id"], "claim_id": claim["claim_id"],
    "status": "expired", "metadata": {
        "wake_id": "wake-run-bug154-bound",
        "work_session_id": f"ws-{claim['claim_id']}",
        "terminalized_by": "runner_lease_expiry",
    },
}, actor="host/bug154", project=P)
assert terminal["status"] == "expired"

print("BUG-154 runner lease surrender tests passed")
