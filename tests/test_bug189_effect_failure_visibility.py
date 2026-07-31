#!/usr/bin/env python3
"""BUG-189 (live remainder): the completion projection names the blocker.

The v1 effect executor half of this proof was deleted with SIMPLIFY-30. What
remains live is the operator projection: a failed completion effect in the
external-effects ledger must surface its last_error / effect / retry count
beside the retry deadline, and an unavailable ledger must degrade to the plain
projection instead of failing the read.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- the projection names the blocker, not just the deadline ---------------
projection_src = (
    ROOT / "src/switchboard/application/queries/completion_projection.py").read_text()
ok("blocked_reason" in projection_src and "_attach_blocked_reason" in projection_src,
   "the completion projection surfaces a blocked_reason beside retry_deadline")

from switchboard.application.queries import completion_projection  # noqa: E402

saved_runs = completion_projection.completion_runs.get_active_completion_run
RUN = {"task_id": "CO-20", "state": "assessing", "route": "review_merge",
       "reason_code": "review_required", "next_retry_at": 123.0, "attempt": 31}
try:
    completion_projection.completion_runs.get_active_completion_run = (
        lambda _task_id, project=None: dict(RUN))

    import switchboard.storage.repositories.external_effects as ee
    saved_list = ee.list_external_effects
    ee.list_external_effects = lambda **_kw: [
        {"effect_type": "completion_effect", "status": "failed", "updated_at": 10.0,
         "last_error": "older failure", "resource": "repair_dispatch", "retry_count": 5},
        {"effect_type": "completion_effect", "status": "failed", "updated_at": 99.0,
         "last_error": "Project execution readiness is blocked.",
         "resource": "ensure_review_generation", "retry_count": 12},
        {"effect_type": "runner_control", "status": "failed", "updated_at": 500.0,
         "last_error": "unrelated surface", "resource": "kill", "retry_count": 1},
    ]
    task = completion_projection.attach_completion_projection(
        {"task_id": "CO-20"}, project="switchboard")
    proj = (task or {}).get("completion_projection") or {}
    ok(proj.get("blocked_reason") == "Project execution readiness is blocked.",
       "projection names the newest failed completion effect's last_error")
    ok(proj.get("blocked_effect") == "ensure_review_generation"
       and proj.get("blocked_retry_count") == 12,
       "projection carries the failing effect and its retry count")
    ok(proj.get("retry_deadline") == 123.0,
       "the existing retry_deadline is preserved beside the new reason")

    ee.list_external_effects = lambda **_kw: []
    clean = completion_projection.attach_completion_projection(
        {"task_id": "CO-20"}, project="switchboard")
    ok("blocked_reason" not in ((clean or {}).get("completion_projection") or {}),
       "no failed effect means no blocked_reason key")

    def boom(**_kw):
        raise RuntimeError("ledger unavailable")

    ee.list_external_effects = boom
    degraded = completion_projection.attach_completion_projection(
        {"task_id": "CO-20"}, project="switchboard")
    ok((degraded or {}).get("completion_projection", {}).get("route") == "review_merge",
       "an unavailable ledger leaves the projection intact rather than failing the read")
finally:
    completion_projection.completion_runs.get_active_completion_run = saved_runs
    ee.list_external_effects = saved_list

print(f"\nBUG-189 effect failure visibility: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
