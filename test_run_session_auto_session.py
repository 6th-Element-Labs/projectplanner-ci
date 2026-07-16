#!/usr/bin/env python3
"""SESSION-11: run_session auto-provisions a Work Session + runs executed tests for
code_strict tasks. Pure unit test — all network/subprocess calls are monkeypatched."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("switchboard_core_auto_test", ROOT / "adapters" / "switchboard_core.py")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _patch(base_stubs):
    """Silence handshake/inbox/heartbeat; install provided stubs; capture calls."""
    calls = []

    def rec(name, ret):
        def f(*a, **k):
            calls.append((name, a, k))
            return ret(*a, **k) if callable(ret) else ret
        return f

    sb.handshake = rec("handshake", {"ok": True})
    sb.inbox = rec("inbox", [])
    sb.heartbeat = rec("heartbeat", None)
    for name, ret in base_stubs.items():
        setattr(sb, name, rec(name, ret))
    return calls


# ---- Test 1: auto path provisions session, runs tests, attaches evidence, archives ----------
FINDING = {"TASK-CS": {"reason": "work_session_required", "policy_profile": "code_strict"}}
completed_evidence = {}


def _complete(project, claim_id, evidence, base=None, token=None, final_status=""):
    completed_evidence["ev"] = evidence
    return {"completed": True, "status": "In Review"}


calls = _patch({
    # first call: nothing claimable, but a code_strict task is skipped for a missing session
    "claim_next": {"claimed": False, "reason": "no_unblocked_work",
                   "dispatch_reason": {"work_session_findings": FINDING}},
    "create_managed_work_session": {
        "work_session_id": "worksession-abc", "workspace_path": "/ws/task-cs",
        "work_session": {"branch": "codex/TASK-CS", "head_sha": "deadbeef"}},
    "claim_task": {"claimed": True, "claim_id": "taskclaim-1",
                   "task": {"task_id": "TASK-CS"}},
    "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                           "status": "success", "executed": True, "exit_code": 0},
    "complete_claim": _complete,
    "archive_work_session_workspace": {"archived": True},
})

res = sb.run_session("switchboard", "claude/PROOF", "claude-code",
                     work_fn=lambda task: {},  # no pr_url -> exercises remote_ref backfill
                     lanes="PROOF", max_tasks=1, auto_work_session=True,
                     source_path="/opt/projectplanner")
names = [c[0] for c in calls]
ok("create_managed_work_session" in names, "auto path provisions a managed Work Session")
ok("claim_task" in names, "auto path claims by exact id after provisioning")
ct = next(c for c in calls if c[0] == "claim_task")
ok(ct[2].get("work_session_id") == "worksession-abc",
   "claim_task binds the provisioned work_session_id")
ev = completed_evidence.get("ev") or {}
ok(ev.get("executed_test_run", {}).get("status") == "success",
   "executed_test_run evidence attached to completion")
ok(ev.get("branch") == "codex/TASK-CS" and ev.get("head_sha") == "deadbeef",
   "managed branch/head_sha filled into evidence")
ok(ev.get("remote_ref") == "refs/heads/codex/TASK-CS",
   "remote_ref backfilled when work_fn gave no pr_url/remote_ref")
ok("archive_work_session_workspace" in names, "workspace archived after completion")
ok(res["completed"] and res["completed"][0]["managed"] is True,
   "completed record marks the task as managed")

# ---- Test 2: auto disabled -> old behavior, no provisioning --------------------------------
calls2 = _patch({
    "claim_next": {"claimed": False, "reason": "no_unblocked_work",
                   "dispatch_reason": {"work_session_findings": FINDING}},
    "create_managed_work_session": {"work_session_id": "x", "workspace_path": "/y"},
    "complete_claim": _complete,
})
res2 = sb.run_session("switchboard", "claude/PROOF", "claude-code",
                      work_fn=lambda task: {}, lanes="PROOF", max_tasks=1,
                      auto_work_session=False)
ok("create_managed_work_session" not in [c[0] for c in calls2],
   "auto_work_session=False never provisions (unchanged default behavior)")
ok(res2["stopped"] == "no_unblocked_work", "loop stops cleanly when auto is off")

# ---- Test 3: lost race on claim_task -> orphan workspace archived, no completion -----------
calls3 = _patch({
    "claim_next": {"claimed": False, "reason": "no_unblocked_work",
                   "dispatch_reason": {"work_session_findings": FINDING}},
    "create_managed_work_session": {
        "work_session_id": "worksession-race", "workspace_path": "/ws/race",
        "work_session": {"branch": "b", "head_sha": "h"}},
    "claim_task": {"claimed": False, "reason": "active_claim"},  # someone else grabbed it
    "complete_claim": _complete,
    "archive_work_session_workspace": {"archived": True},
})
res3 = sb.run_session("switchboard", "claude/PROOF", "claude-code",
                      work_fn=lambda task: {}, lanes="PROOF", max_tasks=1,
                      auto_work_session=True, source_path="/opt/projectplanner")
n3 = [c[0] for c in calls3]
ok("archive_work_session_workspace" in n3 and "complete_claim" not in n3,
   "orphaned workspace archived and no completion when the claim race is lost")

# ---- Test 4: personal host checkpoints the bound session before claim finalization ----------
personal_managed = {
    "work_session_id": "worksession-personal", "workspace_path": "/ws/personal",
    "branch": "codex/TASK-PERSONAL", "head_sha": "a" * 40,
    "profile": "code_strict", "external": True, "bound_existing": True,
    "session_hygiene": {},
}
personal_claim = {
    "claimed": True, "claim_id": "taskclaim-personal", "task_id": "TASK-PERSONAL",
    "task": {"task_id": "TASK-PERSONAL"},
}
old_personal = os.environ.get("PM_PERSONAL_AGENT_HOST_EXECUTION")
try:
    os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = "1"
    calls4 = _patch({
        "_acquire_claim": (personal_claim, personal_managed),
        "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                               "status": "success", "executed": True, "exit_code": 0},
        "checkpoint_personal_work_session": {"updated": True},
        "complete_claim": {"completed": True, "status": "In Review"},
    })
    res4 = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: {"head_sha": "b" * 40,
                              "branch": "codex/TASK-PERSONAL"},
        max_tasks=1, auto_work_session=True)
    names4 = [call[0] for call in calls4]
    calls5 = _patch({
        "_acquire_claim": (personal_claim, personal_managed),
        "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                               "status": "success", "executed": True, "exit_code": 0},
        "checkpoint_personal_work_session": {"updated": False},
        "complete_claim": {"completed": True},
    })
    res5 = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: {"head_sha": "b" * 40,
                              "branch": "codex/TASK-PERSONAL"},
        max_tasks=1, auto_work_session=True)
finally:
    if old_personal is None:
        os.environ.pop("PM_PERSONAL_AGENT_HOST_EXECUTION", None)
    else:
        os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = old_personal
ok(names4.index("checkpoint_personal_work_session") < names4.index("complete_claim")
   and res4["completed"] and "archive_work_session_workspace" not in names4,
   "personal host checkpoints its bound Work Session before completing the exact claim")
ok(res5.get("stopped") == "checkpoint_rejected:TASK-PERSONAL"
   and "complete_claim" not in [call[0] for call in calls5],
   "a rejected personal checkpoint stops loudly without leaking the active claim")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
