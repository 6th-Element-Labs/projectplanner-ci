#!/usr/bin/env python3
"""BUG-189: a suppressed or failed completion effect must say why.

Regression for the 2026-07-25 autopilot outage. Three failed effects carried
last_error "Project execution readiness is blocked." for eight hours. Every tick
reported only reason="effect_retry_backoff" with result={}, because the four
early-return receipts in _execute_mutating_effect hold the ledger row and drop
its last_error/retry_count/resource. The string the system needed was present
the whole time and was never shown; finding it by hand cost a day.

Second half of the same defect: start_task reports started=true when it binds to
an already requested generation, and its capacity readback carries that wake's
real status. A dispatch bound to a dead wake therefore verified in the ledger
and left the task pointing at a wake that would never produce a runner.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import executor

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- the ledger's account survives into the receipt ------------------------
LEDGER_ROW = {
    "status": "failed",
    "last_error": "Project execution readiness is blocked.",
    "retry_count": 12,
    "resource": "ensure_review_generation",
}

diag = executor._effect_diagnostics(LEDGER_ROW)
ok(diag.get("last_error") == "Project execution readiness is blocked.",
   "diagnostics carry the ledger's last_error verbatim")
ok(diag.get("retry_count") == 12 and diag.get("resource") == "ensure_review_generation",
   "diagnostics carry retry_count and the effect resource")

empty = executor._effect_diagnostics({"status": "claimed"})
ok("last_error" not in empty and "resource" not in empty,
   "a clean ledger row contributes no empty diagnostic keys")
ok(executor._effect_diagnostics(None) == {"retry_count": 0},
   "a missing ledger row degrades instead of raising")
ok(executor._effect_diagnostics({"retry_count": "not-a-number"}) == {"retry_count": 0},
   "a non-numeric retry_count degrades to 0 instead of raising")

# Every replay/suppressed-effect receipt must splice the diagnostics in. Pinned
# as a source needle so a new early return cannot be added without carrying them.
src = (ROOT / "src/switchboard/domain/completion/executor.py").read_text()
reasons = ["effect_issued_awaiting_readback", "effect_claim_in_flight",
           "effect_retry_backoff", "effect_retry_claim_lost"]
ok(all(f'"reason": "{reason}"' in src for reason in reasons),
   "all four suppressed-effect reasons are still present")
ok(src.count("**_effect_diagnostics(existing_effect)") == len(reasons) + 1,
   "verified replay and every suppressed-effect receipt carry ledger diagnostics")

# The coordinator has two completion-tick catch sites (task scope and
# deliverable scope). Both existing receipts must preserve the exception text;
# the class name alone hid "unsupported completion state: assessing".
coordinator_src = (ROOT / "scoped_completion_coordinator.py").read_text()
ok(coordinator_src.count('"status": "completion_tick_failed"') == 2,
   "both coordinator completion failure receipts are still present")
ok(coordinator_src.count('"reason": str(exc)') >= 2,
   "both coordinator completion failure receipts preserve exception text")

# --- a dispatch bound to a dead wake is a failure, not a success -----------
DEAD = {
    "started": True,
    "role": "review_merge",
    "wake_id": "wake-d4082d03fafb4506",
    "capacity": {
        "schema": "switchboard.connect.capacity_readback.v1",
        "wake_id": "wake-d4082d03fafb4506",
        "wake_status": "failed",
        "failure_class": "runner_killed",
    },
}
reason = executor._effect_failed(DEAD)
ok(bool(reason), "started=true with a failed wake is reported as an effect failure")
ok("wake-d4082d03fafb4506" in reason and "failed" in reason,
   "the failure names the wake and its state")
ok("runner_killed" in reason,
   "the failure carries the wake's recorded failure_class rather than a generic message")

for state in ("cancelled", "expired"):
    ok(bool(executor._effect_failed(
        {"started": True, "capacity": {"wake_status": state}})),
       f"a {state} wake is also treated as a dead dispatch")

LIVE = {"started": True, "wake_id": "wake-live",
        "capacity": {"wake_id": "wake-live", "wake_status": "pending"}}
ok(executor._effect_failed(LIVE) == "",
   "a pending wake is not a failure")
ok(executor._effect_failed(
    {"started": True, "capacity": {"wake_status": "completed"}}) == "",
   "a completed wake is not a failure")
ok(executor._effect_failed({"started": True}) == "",
   "a readback with no capacity block is unaffected")

# Pre-existing failure detection must be untouched.
ok(executor._effect_failed({"error": "boom"}) == "boom",
   "an explicit error still fails")
ok(executor._effect_failed({"action": "refused", "reason": "nope"}) == "nope",
   "a refusal still fails")
ok(executor._effect_failed({"returncode": 2, "stderr": "bad"}) == "bad",
   "a non-zero returncode still fails")
ok(executor._effect_failed({"returncode": 0}) == "",
   "a clean result still passes")

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
