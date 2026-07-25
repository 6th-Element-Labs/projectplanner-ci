#!/usr/bin/env python3
"""ADR-0020: merge gates observe; GitHub enforces; shape problems warn.

Three behaviors are pinned:

1. Rule-7 compliance — GitHub's aggregate merge states (``blocked``,
   ``unstable``, ``unknown``) never produce a blocking merge-gate finding by
   themselves; only a real conflict does. The old behavior let the gate block
   on an aggregate its own posted status caused (the PR #902 deadlock).
2. Evidence-shape checks warn — a code_strict completion with real identity
   and provenance evidence but the wrong test-run/diff-check key spelling
   completes, records named ``evidence_warnings``, and stays auditable.
   Identity problems (missing head_sha) still deny.
3. The bare board/MCP merge_gate call falls back to ``task.git_state`` for PR
   identity instead of reporting the PR as missing while its number sits in
   the task record.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="adr0020-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_VERIFY_COMPLETION_PUSH"] = "0"

import store  # noqa: E402
from switchboard.application.commands import merge_gate as merge_gate_mod  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


store.init_db(P)
HEAD = "a" * 40


def gate_findings(merge_state, mergeable=True, **payload_extra):
    """Run merge_gate against a supplied PR snapshot; no network."""
    result = merge_gate_mod.merge_gate({
        "task_id": "ADR-20",
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
        "pr_number": 999,
        "target_branch": "master",
        "github_pr": {
            "number": 999,
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
            "state": "open",
            "mergeable": mergeable,
            "mergeable_state": merge_state,
            "base": {"ref": "master"},
            "head": {"ref": "adr20-branch", "sha": HEAD},
        },
        **payload_extra,
    }, project=P)
    return result, {
        str(f.get("code")): f for f in result.get("findings") or []
    }


# --- 1. Aggregate merge states are decomposed, never blocking by themselves.
# BUG-193 strips the self-referential aggregates (blocked/unstable) at
# hydration — compliant decomposition by removal. "unknown" survives the strip
# and must decompose to a named non-blocking finding rather than silence.
task = store.create_task(
    {"workstream_id": "BUG", "title": "ADR-0020 pin"}, actor="adr0020", project=P)
for aggregate in ("blocked", "unstable", "unknown"):
    result, by_code = gate_findings(aggregate)
    ok("pr_not_mergeable" not in by_code,
       f"aggregate merge_state={aggregate!r} never blocks as pr_not_mergeable")
    decomposed = by_code.get("pr_merge_state_decomposed")
    if aggregate == "unknown":
        ok(decomposed is not None and decomposed.get("blocking") is False,
           "merge_state='unknown' yields a named non-blocking decomposed finding")
    else:
        ok(decomposed is None,
           f"merge_state={aggregate!r} is stripped at hydration (BUG-193), "
           "leaving no mergeability finding at all")

result, by_code = gate_findings("dirty", mergeable=False)
conflict = by_code.get("pr_not_mergeable")
ok(conflict is not None and conflict.get("blocking") is True,
   "a real conflict still blocks under the classifier-known code")

# --- 3. Bare call resolves PR identity from task.git_state.
with store._conn(P) as c:
    store._upsert_git_state(c, task["task_id"], {
        "branch": "adr20-branch", "head_sha": HEAD,
        "pr_number": 999,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
    })
bare = merge_gate_mod.merge_gate({"task_id": task["task_id"]}, project=P)
ok(str(bare.get("pr_url") or "").endswith("/pull/999")
   or int(bare.get("pr_number") or 0) == 999,
   "bare merge_gate call recovers PR identity from task.git_state")
ok(not any(f.get("code") == "github_pr_state_unavailable"
           for f in bare.get("findings") or []
           if f.get("blocking") is not False)
   or bare.get("pr_number") == 999,
   "the gate no longer reports the PR missing while git_state names it")

# --- 2. Shape problems warn; identity problems still deny.
code_task = store.create_task(
    {"workstream_id": "BUG", "title": "ADR-0020 completion pin "
     "policy_profile:code_strict"}, actor="adr0020", project=P)
code_branch = f"claude/{code_task['task_id']}-pin"
claim = store.claim_task(
    code_task["task_id"], "claude/adr0020", actor="adr0020", project=P,
    require_work_session=True, session_policy_profile="code_strict",
    work_session={
        "project": P, "task_id": code_task["task_id"], "repo_role": "canonical",
        "storage_mode": "worktree", "worktree_path": str(TMP / "wt"),
        "branch": code_branch, "base_sha": "b" * 40, "head_sha": HEAD,
        "upstream": "origin/master", "dirty_status": "clean",
        "status": "active", "policy_profile": "code_strict",
    })
ok(claim.get("claimed") is True, "code_strict claim binds a work session")

# Real identity + provenance, but test-run/diff-check in the "wrong" spelling.
completion = store.complete_claim(
    claim["claim_id"], evidence=json.dumps({
        "branch": code_branch, "head_sha": HEAD,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
        "pr_number": 999,
        "tests_ran": "python3 tests/test_adr0020_observe_not_enforce.py",
        "diff_status": "clean",
    }), actor="adr0020", project=P)
ok(completion.get("completed") is True,
   "wrong-key-spelling evidence completes instead of denying "
   f"(got {completion.get('reason')})")
warnings = ((completion.get("git_state") or {}).get("evidence") or {}).get(
    "evidence_warnings") or []
warning_reasons = {w.get("reason") for w in warnings}
ok(bool(warnings) and warning_reasons & {
    "missing_executed_test_run", "invalid_executed_test_run",
    "missing_test_evidence", "missing_diff_check"},
   f"the downgrade is recorded as named warnings (got {sorted(warning_reasons)})")

# Identity is still a hard denial: no head_sha at all.
identity_task = store.create_task(
    {"workstream_id": "BUG", "title": "ADR-0020 identity pin "
     "policy_profile:code_strict"}, actor="adr0020", project=P)
identity_branch = f"claude/{identity_task['task_id']}-pin"
identity_claim = store.claim_task(
    identity_task["task_id"], "claude/adr0020b", actor="adr0020", project=P,
    require_work_session=True, session_policy_profile="code_strict",
    work_session={
        "project": P, "task_id": identity_task["task_id"],
        "repo_role": "canonical", "storage_mode": "worktree",
        "worktree_path": str(TMP / "wt2"), "branch": identity_branch,
        "base_sha": "b" * 40, "head_sha": "c" * 40,
        "upstream": "origin/master", "dirty_status": "clean",
        "status": "active", "policy_profile": "code_strict",
    })
denied = store.complete_claim(
    identity_claim["claim_id"], evidence=json.dumps({
        "branch": identity_branch,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/1000",
    }), actor="adr0020", project=P)
ok(denied.get("completed") is False
   and "head_sha" in str(denied.get("reason") or ""),
   f"missing head_sha still denies (got {denied.get('reason')})")

print(f"\nADR-0020 observe-not-enforce: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
