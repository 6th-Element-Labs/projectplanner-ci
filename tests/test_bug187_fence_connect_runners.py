#!/usr/bin/env python3
"""BUG-187: a Connect-path runner with no execution_connection_id can be fenced.

fence_task_generation required all seven identity fields to be non-empty,
including execution_connection_id. Connect-path runners structurally never carry
one -- the completion hydrator sources it only from
active_runner['metadata']['execution_connection_id'], which is absent -- so an
entire class of runner was permanently unfenceable through the completion path.

Consequence, measured on prod 2026-07-25: a stale generation could never be
replaced, so no review_merge runner could start, so no review verdict was ever
recorded (verdict_count 0 on every task), so the state machine never reached
exact_head_gates_passed -- the sole emitter of effect="enqueue". review_required
logged 1000 ticks against 5 for exact_head_gates_passed, and nothing entered the
merge queue all day.

The field stays in the identity comparison. make_runner_lease_due reads the
current value as metadata.get("execution_connection_id") or "", so absence
compares equal to absence while any real disagreement -- in either direction --
is still a mismatch that refuses the fence.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug187-fence-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


CONNECT_IDENTITY = {
    "runner_session_id": "run_e9d5a081ab73a7df",
    "execution_id": "execlease-62d036501b6147ec9873",
    "execution_connection_id": "",          # structurally absent on Connect
    "generation": 3,
    "fence_epoch": 7,
    "role": "review_merge",
    "head_sha": "a894afc0ef8bb84dec565b7a8fe9b15b7dedaaaf",
}

calls = []
saved_due = task_execution.runner_repo.make_runner_lease_due


def fake_due(runner_session_id, **kwargs):
    calls.append({"runner_session_id": runner_session_id, **kwargs})
    return {"updated": True, "runner_session_id": runner_session_id}


try:
    task_execution.runner_repo.make_runner_lease_due = fake_due

    # 1. The outage case: an empty execution_connection_id must not refuse.
    calls.clear()
    result = task_execution.fence_task_generation(
        "CO-20", dict(CONNECT_IDENTITY), project=P, actor="bug187-test")
    ok(bool(result) and len(calls) == 1,
       "a Connect runner with no execution_connection_id reaches the fence")
    ok(calls[0]["runner_session_id"] == "run_e9d5a081ab73a7df",
       "the fence targets the exact runner session")

    # The comparison must still receive the field, so a runner that HAS a
    # connection id is still pinned to it downstream.
    ok("execution_connection_id" in (calls[0].get("expected_identity") or {}),
       "execution_connection_id is still passed through for identity comparison")

    # 2. A runner that does carry one is unaffected.
    calls.clear()
    with_conn = {**CONNECT_IDENTITY, "execution_connection_id": "conn-abc123"}
    task_execution.fence_task_generation(
        "CO-20", with_conn, project=P, actor="bug187-test")
    ok(len(calls) == 1
       and calls[0]["expected_identity"]["execution_connection_id"] == "conn-abc123",
       "a runner that has a connection id still fences on its exact value")

    # 3. Every other identity field is still mandatory -- absence is refused.
    for field in ("runner_session_id", "execution_id", "generation",
                  "fence_epoch", "role"):
        calls.clear()
        broken = {**CONNECT_IDENTITY, field: ""}
        try:
            task_execution.fence_task_generation(
                "CO-20", broken, project=P, actor="bug187-test")
            ok(False, f"missing {field} must still refuse the fence")
        except task_execution.TaskExecutionError as exc:
            detail = exc.as_dict()
            ok(detail.get("error_code") == "runner_bind_incomplete"
               and field in (detail.get("missing") or [])
               and not calls,
               f"missing {field} still refuses with runner_bind_incomplete")

    calls.clear()
    missing_head = dict(CONNECT_IDENTITY)
    missing_head.pop("head_sha")
    try:
        task_execution.fence_task_generation(
            "CO-20", missing_head, project=P, actor="bug187-test")
        ok(False, "omitted head_sha must still refuse the fence")
    except task_execution.TaskExecutionError as exc:
        detail = exc.as_dict()
        ok(detail.get("error_code") == "runner_bind_incomplete"
           and "head_sha" in (detail.get("missing") or [])
           and not calls,
           "omitted head_sha still refuses with runner_bind_incomplete")

    # 4. A missing task_id is still refused.
    calls.clear()
    try:
        task_execution.fence_task_generation(
            "", dict(CONNECT_IDENTITY), project=P, actor="bug187-test")
        ok(False, "a missing task_id must still refuse")
    except task_execution.TaskExecutionError as exc:
        ok(exc.as_dict().get("error_code") == "runner_bind_incomplete" and not calls,
           "a missing task_id still refuses before any fence")

    # 5. A genuine identity mismatch still refuses -- absence must not become a
    #    silent "fence anything".
    task_execution.runner_repo.make_runner_lease_due = (
        lambda _rsid, **_kw: {"updated": False,
                              "error": "execution_identity_mismatch",
                              "mismatched_fields": ["execution_connection_id"]})
    try:
        task_execution.fence_task_generation(
            "CO-20", dict(CONNECT_IDENTITY), project=P, actor="bug187-test")
        ok(False, "an identity mismatch must still refuse the fence")
    except task_execution.TaskExecutionError as exc:
        detail = exc.as_dict()
        ok(detail.get("error_code") == "stale_execution_generation"
           and "execution_connection_id" in (detail.get("mismatched_fields") or []),
           "a connection-id mismatch reported by the repository still refuses")
finally:
    task_execution.runner_repo.make_runner_lease_due = saved_due

# 6. (retired with SIMPLIFY-30) The v1 completion hydrator that projected
#    execution_connection_id was deleted; the fence contract below is the
#    remaining authority on which fields pin a generation.
# 7. The required tuple itself is pinned, so the field cannot be quietly re-added.
#    Read the literal rather than the surrounding prose -- the explanatory comment
#    above it names the field on purpose, and must not trip this assertion.
src = (ROOT / "src/switchboard/application/commands/task_execution.py").read_text()
block = src[src.index("def fence_task_generation("):]
tuple_src = block[block.index("required = ("):block.index("missing = [")]
required_fields = set(re.findall(r'"([a-z_]+)"', tuple_src))
ok("execution_connection_id" not in required_fields,
   "execution_connection_id is not in fence_task_generation's required tuple")
ok(required_fields == {"runner_session_id", "execution_id", "generation",
                       "fence_epoch", "role", "head_sha"},
   f"the fence contract requires exactly the six pinning fields ({sorted(required_fields)})")

print(f"\nBUG-187 fence Connect runners: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
