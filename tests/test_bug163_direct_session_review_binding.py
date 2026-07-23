#!/usr/bin/env python3
"""BUG-163: Connect review identity uses its authenticated assignment binding."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug163-review-binding-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

import store  # noqa: E402


P = "switchboard"
AGENT = "agent/codex/reused"
CURRENT_TASK = "ACCESS-28"
STALE_TASK = "ADAPTER-24"
PRINCIPAL = "direct-session/run_bug163"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    for task_id in (CURRENT_TASK, STALE_TASK):
        store.create_task({
            "task_id": task_id,
            "workstream_id": task_id.split("-", 1)[0],
            "title": task_id,
        }, actor="bug163-test", project=P)
    store.register_agent(
        AGENT, "codex", task_id=STALE_TASK, ttl_s=300,
        actor="bug163-test", project=P,
    )

    binding = store.resolve_write_actor(
        AGENT, project=P, task_id=CURRENT_TASK, agent_id=AGENT,
        principal_id=PRINCIPAL, principal_kind="direct_session",
        bound_task_id=CURRENT_TASK, bound_agent_id=AGENT,
    )
    ok(binding.get("ok") is True
       and binding.get("binding") == "direct_session"
       and binding.get("actor") == AGENT,
       "authenticated Connect assignment outranks stale agent presence")

    wrong_task = store.resolve_write_actor(
        AGENT, project=P, task_id=STALE_TASK, agent_id=AGENT,
        principal_id=PRINCIPAL, principal_kind="direct_session",
        bound_task_id=CURRENT_TASK, bound_agent_id=AGENT,
    )
    ok(wrong_task.get("error") == "direct_session_bound_to_different_task",
       "direct-session bearer cannot cross its authenticated task boundary")

    wrong_agent = store.resolve_write_actor(
        AGENT, project=P, task_id=CURRENT_TASK, agent_id="agent/forged",
        principal_id=PRINCIPAL, principal_kind="direct_session",
        bound_task_id=CURRENT_TASK, bound_agent_id=AGENT,
    )
    ok(wrong_agent.get("error") == "direct_session_bound_to_different_agent",
       "direct-session bearer cannot spoof a reviewer identity")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-163 direct-session review binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
