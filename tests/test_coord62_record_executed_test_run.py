#!/usr/bin/env python3
"""COORD-62 — typed evidence tool: record_executed_test_run.

BREAKDOWN 20 (docs/AUTOPILOT-BREAKDOWNS.md): the CO-21 runner executed five real
suites and recorded them under ``executed_tests`` — right surface, right facts,
wrong grammar. The gate reads one ``executed_test_run`` object with an output hash
under eight exact key names; a human assist rewrote the record by hand.

These tests pin the typed leg of the fix:
- a valid call greens the executed-test gate in ONE call and the response says so;
- malformed input is rejected at the tool boundary, field-named, before storage;
- both read surfaces (work_session.hygiene AND task_git_state.evidence) observe the
  SAME record, so complete_claim and merge authorization agree without a second write;
- the hygiene write MERGES — preflight keys survive (the replace-wipe trap);
- a free-form ``executed_tests`` hygiene write warns, non-blocking, naming the tool;
- the ``missing_executed_test_run`` refusal names the tool;
- the CO-21 window replays to green with zero assists.
"""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="coord62-typed-evidence-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

from path_setup import ROOT  # noqa: F401,E402

import store  # noqa: E402
from switchboard.application.commands import executed_test_runs as ext_cmd  # noqa: E402
from switchboard.storage.repositories import claims as claims_repo  # noqa: E402

P = "switchboard"
AGENT = "claude/COORD-62-typed-evidence"
HEAD = "a" * 40
GOOD_HASH = "9803d0a1" + "0" * 56

store.init_db(P)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_task(title, order=1):
    return store.create_task(
        {"workstream_id": "COORD", "title": title, "sort_order": order},
        actor="test",
        project=P,
    )


def session_payload(task_id, head_sha=HEAD):
    return {
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "claude",
        "repo_role": "canonical",
        "branch": f"claude/{task_id}-typed-evidence",
        "upstream": "origin/master",
        "base_sha": "b" * 40,
        "head_sha": head_sha,
        "worktree_path": f"/tmp/{task_id.lower()}-typed-evidence",
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
    }


def claim_strict(title, order=1, **overrides):
    created = make_task(title, order=order)
    payload = session_payload(created["task_id"], **overrides)
    claimed = store.claim_task(
        created["task_id"],
        AGENT,
        work_session=payload,
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(claimed.get("claimed") is True, f"{title}: strict claim starts")
    return created, claimed, payload


def record_input(task_id, ws_id, **overrides):
    base = {
        "task_id": task_id,
        "work_session_id": ws_id,
        "commands": [".venv/bin/python tests/test_coord62_record_executed_test_run.py"],
        "passed": True,
        "exit_code": 0,
        "output_sha256": GOOD_HASH,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def session_state(ws_id):
    return store.get_work_session(ws_id, project=P)


def git_evidence(task_id):
    return ((store.get_task(task_id, project=P) or {}).get("git_state") or {}).get("evidence") or {}


# ---------------------------------------------------------------------------
# 1. Valid call greens the executed-test gate in one call; the response says so.
# ---------------------------------------------------------------------------
task1, claim1, payload1 = claim_strict("Typed evidence happy path", order=1)
ws1 = claim1["work_session_id"]

before = time.time()
res = ext_cmd.execute_mapping(
    record_input(task1["task_id"], ws1), actor=AGENT, project=P)
after = time.time()

ok(res.get("recorded") is True, "happy path: recorded")
gate = res.get("executed_test_gate") or {}
ok(gate.get("ok") is True, "happy path: response carries a GREEN executed-test gate verdict")
ok(gate.get("source") == "hygiene.executed_test_run",
   f"happy path: gate reads the canonical hygiene key (source={gate.get('source')})")
ok("record_executed_test_run" not in str(res.get("error") or ""), "happy path: no error")
ok("accepted" in str(res.get("message") or "").lower(),
   "happy path: message says the evidence was accepted")

run = res.get("run") or {}
ok(run.get("schema") == "switchboard.executed_test_run.v1", "happy path: record carries the gate schema")
ok(before - 60 <= float(run.get("completed_at") or 0) <= after + 60,
   "happy path: completed_at is stamped server-side (now)")
ok(run.get("head_sha") == HEAD, "happy path: record pinned to the session head")
ok(run.get("branch") == payload1["branch"], "happy path: record pinned to the session branch")

# The merge-authorization read: empty evidence + bound session (what merge_gate does).
sess1 = session_state(ws1)
merge_read = claims_repo._executed_test_run_gate({}, sess1)
ok(merge_read.get("ok") is True, "happy path: merge-authorization read (hygiene) is green")

# ---------------------------------------------------------------------------
# 3. Both read surfaces observe the SAME record; complete_claim agrees without a
#    second write (no executed_test_run in the completion evidence).
# ---------------------------------------------------------------------------
hyg_record = (sess1.get("hygiene") or {}).get("executed_test_run") or {}
git_record = git_evidence(task1["task_id"]).get("executed_test_run") or {}
ok(bool(hyg_record), "surfaces: hygiene.executed_test_run present")
ok(bool(git_record), "surfaces: task_git_state.evidence.executed_test_run present")
ok(hyg_record == git_record, "surfaces: hygiene and claim evidence observe the SAME record")

completed = store.complete_claim(
    claim1["claim_id"],
    evidence={
        "branch": payload1["branch"],
        "head_sha": HEAD,
        "pr_url": "https://github.example/pr/62",
        "git_diff_check": "clean",
    },
    actor=AGENT,
    project=P,
)
ok(completed.get("completed") is True,
   f"surfaces: complete_claim passes with NO executed_test_run in caller evidence "
   f"(reason={completed.get('reason')})")

# ---------------------------------------------------------------------------
# 2. Malformed calls are rejected at the tool boundary, field-named, unstored.
# ---------------------------------------------------------------------------
task2, claim2, payload2 = claim_strict("Typed evidence boundary rejections", order=2)
ws2 = claim2["work_session_id"]


def rejected(data, needle, label):
    out = ext_cmd.execute_mapping(data, actor=AGENT, project=P)
    ok(out.get("error") == "invalid_executed_test_run",
       f"boundary: {label} rejected ({out.get('error')})")
    ok(needle in str(out.get("message") or ""),
       f"boundary: {label} error names the field ({out.get('message')!r:.120})")


rejected(record_input(task2["task_id"], ws2, output_sha256=""), "output_sha256", "missing hash")
rejected(record_input(task2["task_id"], ws2, output_sha256="xyz"), "output_sha256", "bad hash format")
rejected(record_input(task2["task_id"], ws2, commands=[]), "commands", "empty commands")
rejected(record_input(task2["task_id"], ws2, passed=None, exit_code=None),
         "passed", "no pass signal at all")
rejected(record_input(task2["task_id"], ws2, completed_at=123.0),
         "completed_at", "caller-supplied completed_at (server stamps it)")
rejected(record_input(task2["task_id"], ws2, output_hash=GOOD_HASH, output_sha256=None),
         "output_hash", "alias hash key is unrepresentable")

sess2 = session_state(ws2)
ok("executed_test_run" not in (sess2.get("hygiene") or {}),
   "boundary: rejected calls never reach hygiene")
ok("executed_test_run" not in git_evidence(task2["task_id"]),
   "boundary: rejected calls never reach claim evidence")

# head_sha validated against the session (server-side, after the schema boundary).
mismatch = ext_cmd.execute_mapping(
    record_input(task2["task_id"], ws2, head_sha="c" * 40), actor=AGENT, project=P)
ok(mismatch.get("error") == "head_sha_mismatch",
   f"boundary: head_sha contradicting the session is refused ({mismatch.get('error')})")
ok("executed_test_run" not in (session_state(ws2).get("hygiene") or {}),
   "boundary: head mismatch persisted nothing")

wrong_task = make_task("Typed evidence wrong-task target", order=3)
cross = ext_cmd.execute_mapping(
    record_input(wrong_task["task_id"], ws2), actor=AGENT, project=P)
ok(cross.get("error") == "work_session_task_mismatch",
   f"boundary: session bound to another task is refused ({cross.get('error')})")

missing = ext_cmd.execute_mapping(
    record_input(task2["task_id"], "worksession-does-not-exist"), actor=AGENT, project=P)
ok(missing.get("error") == "work_session_not_found",
   f"boundary: unknown work session refused ({missing.get('error')})")

# ---------------------------------------------------------------------------
# 4. Hygiene merge preserves preflight keys (the replace-wipe regression).
# ---------------------------------------------------------------------------
task4, claim4, payload4 = claim_strict("Typed evidence hygiene merge", order=4)
ws4 = claim4["work_session_id"]
seeded = store.update_work_session(
    ws4,
    {"hygiene": {"repo_preflight": {"verdict": "warn", "ok": False,
                                    "source": "coordinator_unverifiable"},
                 "preflight_run_id": "preflight-run-coord62"}},
    actor=AGENT, project=P)
ok(seeded.get("updated") is True, "merge: preflight seeded into hygiene")

merged = ext_cmd.execute_mapping(
    record_input(task4["task_id"], ws4), actor=AGENT, project=P)
ok(merged.get("recorded") is True, "merge: record accepted on a session with preflight")
hyg4 = session_state(ws4).get("hygiene") or {}
ok((hyg4.get("repo_preflight") or {}).get("source") == "coordinator_unverifiable",
   "merge: repo_preflight SURVIVES the evidence write")
ok(hyg4.get("preflight_run_id") == "preflight-run-coord62",
   "merge: preflight_run_id SURVIVES the evidence write")
ok((hyg4.get("executed_test_run") or {}).get("output_sha256") == GOOD_HASH,
   "merge: executed_test_run landed beside preflight")

# ---------------------------------------------------------------------------
# 5. Free-form near-miss writes warn, non-blocking, naming this tool.
# ---------------------------------------------------------------------------
task5, claim5, payload5 = claim_strict("Typed evidence near-miss warning", order=5)
ws5 = claim5["work_session_id"]
freeform = store.update_work_session(
    ws5,
    {"hygiene": {"executed_tests": [
        {"command": "python test_co_fleet.py", "passed": True, "summary": "46 passed"}]}},
    actor=AGENT, project=P)
ok(freeform.get("updated") is True, "near-miss: free-form hygiene write still lands (non-blocking)")
warnings = freeform.get("warnings") or []
ok(bool(warnings), "near-miss: free-form executed_tests write returns a warning")
warning_blob = str(warnings)
ok("record_executed_test_run" in warning_blob, "near-miss: warning NAMES the typed tool")
ok(all(w.get("blocking") is False for w in warnings), "near-miss: warning is non-blocking")
ok((session_state(ws5).get("hygiene") or {}).get("executed_tests"),
   "near-miss: legacy write was persisted unchanged")

# A top-level executed_test_run field is silently dropped today; it must warn too.
dropped = store.update_work_session(
    ws5, {"executed_test_run": {"passed": True}}, actor=AGENT, project=P)
ok("record_executed_test_run" in str(dropped.get("warnings") or []),
   "near-miss: top-level executed_test_run write warns and names the tool")

# An accepted-key hygiene write must NOT warn (that is the legacy-compatible path).
legit = store.update_work_session(
    ws5,
    {"hygiene": {"executed_tests": [
        {"command": "python test_co_fleet.py", "passed": True, "summary": "46 passed"}],
        "executed_test_run": {
            "schema": "switchboard.executed_test_run.v1",
            "commands": ["x"], "passed": True, "output_sha256": GOOD_HASH,
            "completed_at": time.time()}}},
    actor=AGENT, project=P)
ok(not (legit.get("warnings") or []),
   "near-miss: a write that includes the accepted key does not warn")

# ---------------------------------------------------------------------------
# 6. The refusal names the tool.
# ---------------------------------------------------------------------------
report = claims_repo._missing_artifact_report({}, None)
ok(report.get("repair_tool") == "record_executed_test_run",
   "refusal: missing_artifact report names record_executed_test_run")
refusal = claims_repo._executed_test_run_gate({}, {"work_session_id": "worksession-x",
                                                   "hygiene": {}})
ok(refusal.get("ok") is False and refusal.get("reason") == "missing_executed_test_run",
   "refusal: empty session still refuses missing_executed_test_run")
ok("record_executed_test_run" in str(refusal.get("message") or ""),
   "refusal: gate message tells the runner to call the tool")

# ---------------------------------------------------------------------------
# 7. CO-21 replay: the 2026-07-25 window with only this tool available.
# ---------------------------------------------------------------------------
task7, claim7, payload7 = claim_strict("CO-21 replay window", order=7)
ws7 = claim7["work_session_id"]
co21 = store.update_work_session(
    ws7,
    {"hygiene": {"executed_tests": [
        {"command": "python test_co_fleet.py", "passed": True, "summary": "46 passed"},
        {"command": "./test_co_repo_cache.py", "passed": True, "summary": "8 passed"},
        {"command": "python tests/test_co9_hybrid_scheduler.py", "passed": True, "summary": "27 passed"},
        {"command": "python tests/test_bug91_runtime_config_contract.py", "passed": True, "summary": "15 passed"},
        {"command": "python tests/test_co4_graceful_drain.py", "passed": True, "summary": "10 passed"}]}},
    actor="codex/CO-21", project=P)
ok("record_executed_test_run" in str(co21.get("warnings") or []),
   "replay: the runner's original free-form write would have been TOLD the tool name")

sess7 = session_state(ws7)
pre_gate = claims_repo._executed_test_run_gate({}, sess7)
ok(pre_gate.get("ok") is False and pre_gate.get("reason") == "missing_executed_test_run",
   "replay: gate refuses the free-form shape exactly as on 07-25")

one_call = ext_cmd.execute_mapping(
    {
        "task_id": task7["task_id"],
        "work_session_id": ws7,
        "commands": [
            "python test_co_fleet.py",
            "./test_co_repo_cache.py",
            "python tests/test_co9_hybrid_scheduler.py",
            "python tests/test_bug91_runtime_config_contract.py",
            "python tests/test_co4_graceful_drain.py",
        ],
        "passed": True,
        "exit_code": 0,
        "output_sha256": GOOD_HASH,
    },
    actor="codex/CO-21", project=P)
ok(one_call.get("recorded") is True, "replay: the first runner's ONE call records")
ok((one_call.get("executed_test_gate") or {}).get("ok") is True,
   "replay: gate green in the same call — zero assists")
post_gate = claims_repo._executed_test_run_gate({}, session_state(ws7))
ok(post_gate.get("ok") is True, "replay: merge-authorization read agrees")
ok((session_state(ws7).get("hygiene") or {}).get("executed_tests"),
   "replay: the runner's original evidence is preserved beside the typed record")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
