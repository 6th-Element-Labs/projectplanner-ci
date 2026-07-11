#!/usr/bin/env python3
"""ENFORCE: with PM_VERIFY_COMPLETION_PUSH on, the managed loop pushes real refs
(no fabricated remote_ref) and abandons rather than completing when the push fails.
All subprocess/network is monkeypatched."""
import importlib.util
import os
import sys
from pathlib import Path

os.environ["PM_VERIFY_COMPLETION_PUSH"] = "1"
ROOT = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("switchboard_core_push_test", ROOT / "adapters" / "switchboard_core.py")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


FINDING = {"TASK-CS": {"reason": "work_session_required", "policy_profile": "code_strict"}}
completed_evidence = {}


def _complete(project, claim_id, evidence, base=None, token=None, final_status=""):
    completed_evidence["ev"] = evidence
    return {"completed": True, "status": "In Review"}


def _patch(base_stubs):
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


BASE = {
    "claim_next": {"claimed": False, "reason": "no_unblocked_work",
                   "dispatch_reason": {"work_session_findings": FINDING}},
    "create_managed_work_session": {
        "work_session_id": "ws-1", "workspace_path": "/ws/task-cs",
        "work_session": {"branch": "codex/TASK-CS", "head_sha": "deadbeef"}},
    "claim_task": {"claimed": True, "claim_id": "claim-1", "task": {"task_id": "TASK-CS"}},
    "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                           "status": "success", "executed": True, "exit_code": 0},
    "complete_claim": _complete,
    "abandon_claim": {"abandoned": True},
    "archive_work_session_workspace": {"archived": True},
}

# ---- Push succeeds -> remote_ref comes from the verified push, task completes ----
completed_evidence.clear()
calls = _patch(dict(BASE))
sb._push_and_verify = lambda ws, br, sha, **k: {
    "ok": True, "remote_ref": f"refs/heads/{br}", "pushed_at": 123.0, "remote_sha": sha}
res = sb.run_session("switchboard", "claude/PROOF", "claude-code",
                     work_fn=lambda task: {}, lanes="PROOF", max_tasks=1,
                     auto_work_session=True, source_path="/opt/projectplanner")
ev = completed_evidence.get("ev") or {}
ok(ev.get("remote_ref") == "refs/heads/codex/TASK-CS", "remote_ref set from verified push")
ok(ev.get("pushed_at") == 123.0, "pushed_at set from verified push")
ok(res["completed"] and res["completed"][0]["managed"] is True, "task completes when push verified")

# ---- Push fails -> abandon, archive, no completion (loud stop) ----
completed_evidence.clear()
calls = _patch(dict(BASE))
sb._push_and_verify = lambda ws, br, sha, **k: {"ok": False, "detail": "no origin remote"}
res = sb.run_session("switchboard", "claude/PROOF", "claude-code",
                     work_fn=lambda task: {}, lanes="PROOF", max_tasks=1,
                     auto_work_session=True, source_path="/opt/projectplanner")
names = [c[0] for c in calls]
ok(not completed_evidence.get("ev"), "complete_claim NOT called when push fails")
ok("abandon_claim" in names, "claim abandoned when push fails")
ok("archive_work_session_workspace" in names, "workspace archived on push failure")
ok(res.get("stopped", "").startswith("push_error:"), "loop stops with push_error")
ok(res["completed"] == [], "no completion recorded on push failure")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
