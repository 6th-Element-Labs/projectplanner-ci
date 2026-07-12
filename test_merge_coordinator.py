"""HARDEN-72 / Lever 6 — Switchboard merge-coordinator.

Script-style test (run directly: ``python test_merge_coordinator.py``; exits nonzero
on failure). Covers the pure dependency-ordered, back-pressured merge planner and the
safe-by-default coordinate() driver."""
import os

import merge_coordinator as mc
from merge_coordinator import PRCandidate


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


def green(number, task_ids, **kw):
    """A green, provenance-backed, conflict-free candidate (the merge-ready default)."""
    return PRCandidate(number=number, head_sha=f"sha{number}", task_ids=task_ids,
                       gate_state="success", claim_backed=True, mergeable=True, **kw)


# ----- eligibility filters ---------------------------------------------------------
cands = [
    green(10, ["A-1"]),
    PRCandidate(number=11, task_ids=["A-2"], gate_state="failure", claim_backed=True),
    PRCandidate(number=12, task_ids=["A-3"], gate_state="success", claim_backed=False),
    PRCandidate(number=13, task_ids=["A-4"], gate_state="success", claim_backed=True,
                mergeable=True, draft=True),
    PRCandidate(number=14, task_ids=["A-5"], gate_state="success", claim_backed=True,
                mergeable=False),
    PRCandidate(number=15, task_ids=["A-6"], gate_state="pending", claim_backed=True),
]
plan = mc.plan_merges(cands, max_in_flight=10)
released = {r["pr"] for r in plan["release"]}
defer_by_pr = {d["pr"]: d for d in plan["defer"]}
ok(released == {10}, "only the green/backed/mergeable/non-draft PR is released")
ok(defer_by_pr[11]["reason"] == mc.REASON_NOT_GREEN, "red PR deferred as not_green")
ok(defer_by_pr[11]["gate_state"] == "failure", "not_green defer records the gate state")
ok(defer_by_pr[12]["reason"] == mc.REASON_NO_PROVENANCE, "unbacked PR deferred as no_provenance")
ok(defer_by_pr[13]["reason"] == mc.REASON_DRAFT, "draft PR deferred as draft")
ok(defer_by_pr[14]["reason"] == mc.REASON_CONFLICTS, "unmergeable PR deferred as conflicts")
ok(defer_by_pr[15]["reason"] == mc.REASON_NOT_GREEN, "pending PR deferred as not_green")


# ----- dependency ordering ---------------------------------------------------------
# PR#20 (task B-2) depends on task B-1, whose PR#21 is still open -> #20 waits for #21.
dep_cands = [green(20, ["B-2"]), green(21, ["B-1"])]
deps = {"B-2": ["B-1"]}
plan = mc.plan_merges(dep_cands, task_deps=deps, max_in_flight=10)
released = {r["pr"] for r in plan["release"]}
ok(released == {21}, "a PR whose dependency PR is still open is held back")
d20 = [d for d in plan["defer"] if d["pr"] == 20][0]
ok(d20["reason"] == mc.REASON_BLOCKED and d20["blocked_by"] == ["B-1"],
   "the dependent PR is deferred with the blocking task id")

# Once B-1 has merged (not in open_task_ids), #20 becomes eligible.
plan = mc.plan_merges([green(20, ["B-2"])], task_deps=deps,
                      open_task_ids=["B-2"], max_in_flight=10)
ok({r["pr"] for r in plan["release"]} == {20},
   "a PR whose dependency already merged is released")

# A dependency that never had a PR (absent from open ids) does not block.
plan = mc.plan_merges([green(22, ["C-2"])], task_deps={"C-2": ["C-0"]},
                      open_task_ids=["C-2"], max_in_flight=10)
ok({r["pr"] for r in plan["release"]} == {22}, "an already-landed/absent dependency never blocks")


# ----- backpressure: capacity ------------------------------------------------------
many = [green(n, [f"D-{n}"]) for n in (30, 31, 32, 33)]
plan = mc.plan_merges(many, max_in_flight=2)
ok([r["pr"] for r in plan["release"]] == [30, 31],
   "release is capped at max_in_flight and ordered by PR number")
ok([h["pr"] for h in plan["hold"]] == [32, 33], "the rest are held, not deferred")
ok(all(h["reason"] == mc.HOLD_CAPACITY for h in plan["hold"]),
   "capacity holds are marked backpressure_capacity")

# in_flight already consumes capacity.
plan = mc.plan_merges(many, max_in_flight=2, in_flight=1)
ok([r["pr"] for r in plan["release"]] == [30], "in_flight reduces the release capacity")


# ----- backpressure: saturation ----------------------------------------------------
plan = mc.plan_merges(many, max_in_flight=10, saturated=True)
ok(plan["release"] == [] and plan["capacity"] == 0,
   "a saturated box releases nothing this pass")
ok(all(h["reason"] == mc.HOLD_SATURATED for h in plan["hold"]),
   "saturation holds are marked backpressure_saturated")
ok(len(plan["hold"]) == 4, "every eligible PR is held while saturated")


# ----- is_box_saturated interpretation ---------------------------------------------
ok(mc.is_box_saturated({"saturated": True}) is True, "explicit saturated flag -> hold")
ok(mc.is_box_saturated({"status": "red"}) is True, "red status -> hold")
ok(mc.is_box_saturated({"status": "critical"}) is True, "critical status -> hold")
ok(mc.is_box_saturated({"alerts": [{"severity": "red"}]}) is True, "a red alert -> hold")
ok(mc.is_box_saturated({"alerts": [{"severity": "yellow"}]}) is False,
   "a yellow alert alone does not hold")
ok(mc.is_box_saturated({"status": "green"}) is False, "green status -> release")
ok(mc.is_box_saturated(None) is False, "unknown/None signal fails open (not saturated)")


# ----- max_in_flight_from_env ------------------------------------------------------
os.environ.pop("SWITCHBOARD_MERGE_MAX_IN_FLIGHT", None)
ok(mc.max_in_flight_from_env() == mc.DEFAULT_MAX_IN_FLIGHT, "default cap when env unset")
os.environ["SWITCHBOARD_MERGE_MAX_IN_FLIGHT"] = "5"
ok(mc.max_in_flight_from_env() == 5, "env overrides the cap")
os.environ["SWITCHBOARD_MERGE_MAX_IN_FLIGHT"] = "-3"
ok(mc.max_in_flight_from_env() == 0, "negative env clamps to 0")
os.environ["SWITCHBOARD_MERGE_MAX_IN_FLIGHT"] = "nope"
ok(mc.max_in_flight_from_env(7) == 7, "non-integer env falls back to the default")
os.environ.pop("SWITCHBOARD_MERGE_MAX_IN_FLIGHT", None)


# ----- coordinate(): safe by default, arms in order when opted in ------------------
plan = mc.coordinate(many, max_in_flight=2)
ok(plan["dry_run"] is True and plan["armed"] == [],
   "coordinate() is dry-run by default and arms nothing")

armed_order = []
plan = mc.coordinate(many, max_in_flight=2, dry_run=False,
                     arm_fn=lambda ref: armed_order.append(ref["pr"]) or "auto_merge_on")
ok(armed_order == [30, 31], "coordinate() arms released PRs in dependency/number order")
ok([a["pr"] for a in plan["armed"]] == [30, 31] and all(a["ok"] for a in plan["armed"]),
   "coordinate() records each arm outcome")


def _flaky_arm(ref):
    if ref["pr"] == 30:
        raise RuntimeError("github 502")
    return "ok"


plan = mc.coordinate(many, max_in_flight=2, dry_run=False, arm_fn=_flaky_arm)
by_pr = {a["pr"]: a for a in plan["armed"]}
ok(by_pr[30]["ok"] is False and "github 502" in by_pr[30]["error"],
   "one PR's arm failure is captured, not raised")
ok(by_pr[31]["ok"] is True, "a later PR still arms after an earlier arm fails")


# ----- format_plan is a readable summary -------------------------------------------
text = mc.format_plan(mc.plan_merges(dep_cands, task_deps=deps, max_in_flight=1))
ok("RELEASE #21" in text and "DEFER   #20" in text and "B-1" in text,
   "format_plan renders release/defer lines with blocking ids")


# ----- collection layer (injected fetchers) ----------------------------------------
raw_prs = [
    {"number": 40, "head": {"sha": "s40"}, "base": {"ref": "master"}, "mergeable": True,
     "draft": False, "title": "feat E-1"},
    {"number": 41, "head": {"sha": "s41"}, "base": {"ref": "master"}, "mergeable": True,
     "draft": True, "title": "wip E-2"},
]
collected = mc.collect_candidates(
    raw_prs,
    gate_state_fn=lambda pr, sha: "success" if pr["number"] == 40 else "pending",
    backed_fn=lambda pr, tids: True,
    task_ids_fn=lambda pr: [f"E-{pr['number'] - 39}"])
ok(len(collected) == 2, "collect_candidates builds one candidate per PR")
c40 = [c for c in collected if c.number == 40][0]
ok(c40.head_sha == "s40" and c40.task_ids == ["E-1"] and c40.gate_state == "success",
   "collect_candidates maps sha/tasks/gate state via the injected fetchers")
ok(c40.mergeable is True and c40.draft is False, "collect_candidates carries mergeable/draft")
ok([c for c in collected if c.number == 41][0].draft is True, "draft flag is read from the PR")

ok(mc.open_task_ids(collected) == {"E-1", "E-2"}, "open_task_ids unions every candidate's tasks")

resolved_deps = mc.build_task_deps(
    collected, get_deps_fn=lambda tid: ["E-1"] if tid == "E-2" else [])
ok(resolved_deps == {"E-1": [], "E-2": ["E-1"]}, "build_task_deps resolves each task's deps once")


def _raising_deps(tid):
    raise RuntimeError("board unreachable")


safe = mc.build_task_deps(collected, get_deps_fn=_raising_deps)
ok(safe == {"E-1": [], "E-2": []}, "build_task_deps swallows a board error into no-deps")


print("\nAll merge_coordinator tests passed.")
