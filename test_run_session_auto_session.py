#!/usr/bin/env python3
"""SESSION-11: run_session auto-provisions a Work Session + runs executed tests for
code_strict tasks. Pure unit test — all network/subprocess calls are monkeypatched."""
import importlib.util
import json
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


# The post-run harness executes code from the worker-modifiable checkout. Its
# subprocess environment must not carry either stable coordination bearer.
captured_test_environment = {}
original_subprocess_run = sb.subprocess.run
original_coordination_env = {
    key: os.environ.get(key) for key in ("PM_MCP_TOKEN", "SWITCHBOARD_TOKEN")
}


def capture_test_environment(command, **kwargs):
    captured_test_environment.update(kwargs.get("env") or {})
    return sb.subprocess.CompletedProcess(
        command, 0,
        sb.json.dumps({
            "schema": "switchboard.executed_test_run.v1",
            "status": "success",
            "executed": True,
            "exit_code": 0,
        }),
        "",
    )


try:
    os.environ["PM_MCP_TOKEN"] = "stable-host-bearer-must-not-cross-tests"
    os.environ["SWITCHBOARD_TOKEN"] = "alternate-host-bearer-must-not-cross-tests"
    sb.subprocess.run = capture_test_environment
    sanitized_test_run = sb.run_executed_tests(
        "/ws/sanitized", "worksession-sanitized", "TASK-SANITIZED",
        "taskclaim-sanitized", "codex/TASK-SANITIZED",
        commands=["true"],
    )
finally:
    sb.subprocess.run = original_subprocess_run
    for key, value in original_coordination_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
ok(sanitized_test_run.get("status") == "success"
   and "PM_MCP_TOKEN" not in captured_test_environment
   and "SWITCHBOARD_TOKEN" not in captured_test_environment,
   "executed-test harness strips stable Agent Host coordination credentials")


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
    personal_lifecycle_events = []
    calls4 = _patch({
        "_acquire_claim": (personal_claim, personal_managed),
        "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                               "status": "success", "executed": True, "exit_code": 0},
        "checkpoint_personal_work_session": {"updated": True},
        "complete_claim": {"completed": True, "status": "In Review"},
        "_cleanup_personal_bound_workspace": {"cleaned": True},
    })
    res4 = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: {"head_sha": "b" * 40,
                              "branch": "codex/TASK-PERSONAL",
                              "_switchboard_personal_execution_lifecycle": {
                                  "complete": lambda _evidence: (
                                      personal_lifecycle_events.append("complete")
                                      or {"status": "completed"}),
                                  "fail": lambda reason: (
                                      personal_lifecycle_events.append(f"fail:{reason}")
                                      or {"status": "failed"}),
                                  "checkpointed": lambda _evidence, _checkpoint: (
                                      personal_lifecycle_events.append("checkpointed")),
                                  "claim_completed": lambda _evidence, _completion: (
                                      personal_lifecycle_events.append("claim_completed")),
                                  "cleanup_completed": lambda: (
                                      personal_lifecycle_events.append("cleanup_completed")),
                              }},
        max_tasks=1, auto_work_session=True)
    rejected_lifecycle_events = []
    names4 = [call[0] for call in calls4]
    calls5 = _patch({
        "_acquire_claim": (personal_claim, personal_managed),
        "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                               "status": "success", "executed": True, "exit_code": 0},
        "checkpoint_personal_work_session": {"updated": False},
        "complete_claim": {"completed": True},
        "abandon_claim": {"abandoned": True},
        "_cleanup_personal_bound_workspace": {"cleaned": True},
    })
    res5 = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: {"head_sha": "b" * 40,
                              "branch": "codex/TASK-PERSONAL",
                              "_switchboard_personal_execution_lifecycle": {
                                  "complete": lambda _evidence: (
                                      rejected_lifecycle_events.append("complete")
                                      or {"status": "completed"}),
                                  "fail": lambda reason: (
                                      rejected_lifecycle_events.append(f"fail:{reason}")
                                      or {"status": "failed"}),
                              }},
        max_tasks=1, auto_work_session=True)
finally:
    if old_personal is None:
        os.environ.pop("PM_PERSONAL_AGENT_HOST_EXECUTION", None)
    else:
        os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = old_personal
ok(names4.index("checkpoint_personal_work_session") < names4.index("complete_claim")
   and res4["completed"] and "archive_work_session_workspace" not in names4
   and "_cleanup_personal_bound_workspace" in names4
   and personal_lifecycle_events == [
       "complete", "checkpointed", "claim_completed", "cleanup_completed"],
   "personal host checkpoints its bound Work Session before completing the exact claim")
ok(res5.get("stopped", "").startswith("checkpoint_rejected:TASK-PERSONAL")
   and "complete_claim" not in [call[0] for call in calls5]
   and rejected_lifecycle_events[0] == "complete"
   and rejected_lifecycle_events[1].startswith("fail:")
   and "abandon_claim" in [call[0] for call in calls5]
   and "_cleanup_personal_bound_workspace" in [call[0] for call in calls5],
   "a rejected personal checkpoint recovers failure, abandons, and cleans its checkout")

failed_test_lifecycle_events = []
calls5b = _patch({
    "_acquire_claim": (personal_claim, personal_managed),
    "run_executed_tests": {"schema": "switchboard.executed_test_run.v1",
                           "status": "failed", "executed": True, "exit_code": 1},
    "abandon_claim": {"abandoned": True},
    "_cleanup_personal_bound_workspace": {"cleaned": True},
    "complete_claim": {"completed": True},
})
try:
    os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = "1"
    res5b = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: {
            "head_sha": "b" * 40,
            "branch": "codex/TASK-PERSONAL",
            "_switchboard_personal_execution_lifecycle": {
                "complete": lambda _evidence: (
                    failed_test_lifecycle_events.append("complete")
                    or {"status": "completed"}),
                "fail": lambda reason: (
                    failed_test_lifecycle_events.append(f"fail:{reason}")
                    or {"status": "failed"}),
            },
        },
        max_tasks=1, auto_work_session=True)
finally:
    if old_personal is None:
        os.environ.pop("PM_PERSONAL_AGENT_HOST_EXECUTION", None)
    else:
        os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = old_personal
ok(res5b.get("stopped", "").startswith("executed_tests_failed:TASK-PERSONAL")
   and failed_test_lifecycle_events
   and failed_test_lifecycle_events[0].startswith("fail:")
   and "complete" not in failed_test_lifecycle_events
   and "complete_claim" not in [call[0] for call in calls5b],
   "failed executed tests terminalize failure before abandon and never publish success")

calls6 = _patch({
    "_acquire_claim": (personal_claim, personal_managed),
    "abandon_claim": {"abandoned": True},
    "_cleanup_personal_bound_workspace": {"cleaned": True},
    "archive_work_session_workspace": {"archived": True},
})
try:
    os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = "1"
    res6 = sb.run_session(
        "switchboard", "codex/TASK-PERSONAL", "codex",
        work_fn=lambda task: (_ for _ in ()).throw(RuntimeError("native failure")),
        max_tasks=1, auto_work_session=True)
finally:
    if old_personal is None:
        os.environ.pop("PM_PERSONAL_AGENT_HOST_EXECUTION", None)
    else:
        os.environ["PM_PERSONAL_AGENT_HOST_EXECUTION"] = old_personal
names6 = [call[0] for call in calls6]
ok(res6.get("stopped", "").startswith("work_error:TASK-PERSONAL")
   and "_cleanup_personal_bound_workspace" in names6
   and "archive_work_session_workspace" not in names6,
   "failed personal adoption cleans only its host-local checkout after exact abandon")

# ---- Test 7: ambiguous post-execution writes retry or recover by exact readback -------------
recovery_managed = {
    "work_session_id": "worksession-recovery",
    "workspace_path": "/ws/recovery",
}
recovery_evidence = {"head_sha": "c" * 40, "branch": "codex/TASK-RECOVERY"}
recovery_agent = "codex/TASK-RECOVERY"
recovery_claim = "taskclaim-recovery"
recovery_task = "TASK-RECOVERY"
recovery_binding = json.dumps({
    "task_id": recovery_task,
    "claim_id": recovery_claim,
    "work_session_id": recovery_managed["work_session_id"],
    "agent_id": recovery_agent,
})


def recovery_session(head, status="active"):
    return {
        "work_session_id": recovery_managed["work_session_id"],
        "task_id": recovery_task,
        "claim_id": recovery_claim,
        "agent_id": recovery_agent,
        "head_sha": head,
        "dirty_status": "clean",
        "status": status,
    }


saved_recovery_functions = {
    name: getattr(sb, name) for name in (
        "checkpoint_personal_work_session", "complete_claim",
        "get_personal_postprocessing_state",
    )
}
saved_recovery_sleep = sb.time.sleep
saved_recovery_env = {
    key: os.environ.get(key) for key in (
        "PM_CO_ACCOUNT_BINDING_JSON",
        "PM_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S",
    )
}
try:
    os.environ["PM_CO_ACCOUNT_BINDING_JSON"] = recovery_binding
    os.environ["PM_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S"] = "1"
    sb.time.sleep = lambda _seconds: None

    checkpoint_before_calls = [0]

    def checkpoint_before(*_args, **_kwargs):
        checkpoint_before_calls[0] += 1
        if checkpoint_before_calls[0] == 1:
            raise RuntimeError("checkpoint failed before commit")
        return {"updated": True}

    sb.checkpoint_personal_work_session = checkpoint_before
    sb.get_personal_postprocessing_state = lambda *_args, **_kwargs: {
        "allowed": True, "state": "terminalized"}
    checkpoint_before_result = sb.checkpoint_personal_work_session_with_recovery(
        "switchboard", recovery_managed, recovery_evidence, recovery_agent)

    sb.checkpoint_personal_work_session = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("checkpoint response lost after commit")))
    sb.get_personal_postprocessing_state = lambda *_args, **_kwargs: {
        "allowed": True, "state": "checkpointed"}
    checkpoint_after_result = sb.checkpoint_personal_work_session_with_recovery(
        "switchboard", recovery_managed, recovery_evidence, recovery_agent)

    complete_before_calls = [0]

    def complete_before(*_args, **_kwargs):
        complete_before_calls[0] += 1
        if complete_before_calls[0] == 1:
            raise RuntimeError("completion failed before commit")
        return {"completed": True, "status": "In Review"}

    sb.complete_claim = complete_before
    sb.get_personal_postprocessing_state = lambda *_args, **_kwargs: {
        "allowed": True, "state": "checkpointed"}
    complete_before_result = sb.complete_personal_claim_with_recovery(
        "switchboard", recovery_task, recovery_claim, recovery_managed,
        recovery_evidence, recovery_agent)

    sb.complete_claim = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("completion response lost after commit"))
    sb.get_personal_postprocessing_state = lambda *_args, **_kwargs: {
        "allowed": True, "state": "completed"}
    complete_after_result = sb.complete_personal_claim_with_recovery(
        "switchboard", recovery_task, recovery_claim, recovery_managed,
        recovery_evidence, recovery_agent)

    transient_readback_calls = [0]

    def transient_readback(*_args, **_kwargs):
        transient_readback_calls[0] += 1
        if transient_readback_calls[0] == 1:
            raise RuntimeError("temporary readback outage")
        return {"allowed": True, "state": "checkpointed"}

    transient_completion_calls = [0]

    def transient_completion(*_args, **_kwargs):
        transient_completion_calls[0] += 1
        if transient_completion_calls[0] < 2:
            raise RuntimeError("temporary completion outage")
        return {"completed": True, "status": "In Review"}

    sb.complete_claim = transient_completion
    sb.get_personal_postprocessing_state = transient_readback
    complete_transient_result = sb.complete_personal_claim_with_recovery(
        "switchboard", recovery_task, recovery_claim, recovery_managed,
        recovery_evidence, recovery_agent)
finally:
    for name, value in saved_recovery_functions.items():
        setattr(sb, name, value)
    sb.time.sleep = saved_recovery_sleep
    for key, value in saved_recovery_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

ok(checkpoint_before_result.get("updated") is True
   and checkpoint_before_calls[0] == 2
   and checkpoint_after_result.get("checkpoint_confirmed_by_readback") is True,
   "checkpoint retries before-commit loss and confirms after-commit loss by exact readback")
ok(complete_before_result.get("completed") is True
   and complete_before_calls[0] == 2
   and complete_after_result.get("completed") is True
   and complete_after_result.get("completion_confirmed_by_readback") is True,
   "claim completion retries before-commit loss and confirms after-commit loss by exact readback")
ok(complete_transient_result.get("completed") is True
   and transient_completion_calls[0] == 2
   and transient_readback_calls[0] == 1,
   "claim completion keeps retrying through a transient exact-readback outage")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
