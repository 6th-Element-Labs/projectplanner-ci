# Autopilot Fix Plan

Derived from the 2026-07-25 DOGFOOD-19 Mac proof run. Evidence lives in
[AUTOPILOT-BREAKDOWNS.md](AUTOPILOT-BREAKDOWNS.md); this file is the assessment and the
sequenced plan. Nothing here has been implemented.

## The result that frames everything

| Metric | Result |
|---|---|
| Tasks launched | 5 (UI-63, CO-20, ADAPTER-27, CO-21, + self-filed BUG-178/179) |
| Reached a PR with **green CI**, unattended | **5 / 5** |
| Reached **Done** | **0 / 5** |

The pipeline is not broken in the middle — launch, claim, isolated worktree, implement,
push, PR, CI, lease surrender and even autonomous bug intake all work with no operator.
**It fails at the last inch, and it fails 100% of the time.**

## Root cause (one sentence)

The merge gate emits precise, typed reason codes, but the worker receives a single
undifferentiated `failure` — so process-state problems (draft PR, missing verdict,
missing evidence) are indistinguishable from "your code is wrong", and the worker's only
available response is another commit, which can never fix any of them.

Everything in Phase 1 below is a consequence of that one design gap.

---

## Assessment

### Class A — Completion dead-ends (block 100% of Done). Fix first.

| # | Defect | Blocks | Cost | Notes |
|---|---|---|---|---|
| B5 | Workers open **draft** PRs; gate refuses them | 3 of 5 | **S** | Proof: BUG-178 had flawless evidence and still failed on draft alone |
| B6 | Each remediation push invalidates the exact-head review verdict; never re-recorded | #864 | S–M | Verdict binding is correct; the loop just never re-records |
| B7 | `executed_test_run` missing on the **remediation** path | #864 | S | Scoped down — BUG-178 proves the machinery works on the implementation path |

These three share one fix: **route typed gate codes to typed worker actions.**

### Class B — Dispatch correctness (waste, not blockage)

| # | Defect | Impact | Cost |
|---|---|---|---|
| B3 | `start_task` dispatches a live runner for a task with unsatisfied dependencies | CO-21 burned a runner + quota for ~45 min on unclaimable work | **XS** |
| B2 | `start_task` requires caller registered against that exact task | N re-registrations to arm N tasks; operator provenance ends up bound to the last task | S |
| B1 | `start_task` refuses an unregistered operator identity | Discoverability only | XS |
| B4 | Branch prefix does not match launched runtime | `pr_provenance_gate` keys fleet detection off prefix | S |

### Class C — Learning / moat (strategic)

| # | Gap | Why it matters | Cost |
|---|---|---|---|
| B9 | Reason codes recorded but **never aggregated** | Identical `required_exact_head_ci_failed` on 3/3 PRs while the real cause was draft state — a single systemic stall read as three unrelated retries | S–M |
| B10 | Execution transcripts incomplete (SIMPLIFY-9) | Outcomes durable, reasoning lost — the missing input for any learning loop | L |

---

## Plan

### Phase 1 — Make the completion loop closable (unblocks 5/5)

**1.1 Mark PRs ready (B5).** Either the worker calls `gh pr ready` when it considers work
complete, or the gate routes the draft code to a "mark ready" action. Recommend doing it
in the *worker completion contract* so it holds for every runtime.

**1.2 Re-record the exact-head verdict on every push (B6).** Make verdict recording part
of the push cycle rather than a one-shot. The gate should also distinguish
`no_verdict_for_this_head` (worker-actionable) from `review_found_problems` (needs code).

**1.3 Attach `executed_test_run` on the remediation path (B7).** Copy what the
implementation path already does correctly — BUG-178's `completion_handoff` is the
reference implementation.

**1.4 The unifying change: typed reason → typed action.** Publish the gate's reason codes
to the worker with a required next action per code. Without this, 1.1–1.3 are three
patches to the same hole and the next process-state code will stall the fleet again.

*Exit criterion: re-run DOGFOOD-19 and get at least one task to Done with no operator
interaction.*

### Phase 2 — Stop the waste (cheap, do alongside Phase 1)

**2.1 Check dependency readiness at dispatch (B3).** `start_task` should evaluate
`dependency_state.satisfied` before requesting a wake and refuse with a typed
`dependencies_unsatisfied`, consistent with how it already fails closed on capacity and
runtime mismatch. Roughly a ten-line change that prevents whole wasted runners.

**2.2 Emit a per-run economic summary.** Runners spawned, quota consumed, commits pushed
against unwinnable gates. This run spent three remediation runners on a problem no commit
could fix and nothing surfaced that cost.

### Phase 3 — Close the learning loop (the moat)

**3.1 Aggregate reason codes (B9).** A rollup beside the existing
`get_review_remediation_metrics` / `get_saturation_signals` / `get_plan_signals`, with a
"same reason_code on N tasks in window W" signal routed to the attention queue. This run's
true diagnosis would have been one glance.

**3.2 Land SIMPLIFY-9 transcript capture (B10)** and store transcripts alongside the
assignment records, so an analyzer can join `reason_code` → transcript → outcome. That
join is what turns a forensic log into a system that improves as it runs.

### Phase 4 — Ergonomics and provenance hygiene

**4.1** Allow a launcher-mode principal to start any task in its project without per-task
re-registration (B2), and make the refusal messages name the required flow (B1).
**4.2** Make branch prefix reflect the dispatched runtime, or stop using prefix as the
fleet-detection signal (B4).

---

## Sequencing rationale

- Phase 1 is the only phase that changes the 0/5 completion rate. Everything else is
  efficiency or insight.
- Phase 2 is nearly free and prevents recurring waste; no reason to defer it.
- Phase 3 is where the durable advantage is. The expensive half — typed, fenced, justified
  decision records — already exists. Counting them does not.
- Phase 4 is real but nothing is blocked on it.

## Recommended first move

**Phase 1.1 + 2.1 together.** Between them they are a few hours of work, they unblock three
of five PRs immediately, and they stop the most expensive form of waste observed. Then
re-run DOGFOOD-19 as the acceptance test for Phase 1.
