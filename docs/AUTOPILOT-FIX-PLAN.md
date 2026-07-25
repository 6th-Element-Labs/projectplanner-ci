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

**Corrected 2026-07-25 after operator code review — see Amendment history.**

`Switchboard / merge authorization` is a required status context, so any process-state
problem turns it red; `_required_ci_decision` is consulted *before* the review, findings
and draft branches, misattributes that red context as `product` CI failure, and returns —
making the already-correct typed routing downstream structurally unreachable.

The defect is **ordering**, not missing types. Everything in Phase 1 follows from it.

### The chain, verified in code

| Step | Location | What happens |
|---|---|---|
| 1 | `state_machine.py:578` | `_required_ci_decision(snap)` runs first and returns early |
| 2 | `normalize.py:76-85` | red merge-auth context has no authority/infrastructure prose → else-branch stamps `failure_attribution = "product"` |
| 3 | `state_machine.py:404-408` | `product` → `_decision("blocked","remediation","required_exact_head_ci_failed")` |
| 4 | `state_machine.py:583/596/619` | review check, `_finding_decision`, and the draft branch are never reached |

### What already exists and is already correct

- `hydrate_completion_snapshot` (`completion_driver.py:47`) already calls `merge_gate`
  (`:77`), so **coded findings are already on the snapshot**.
- `_finding_decision` (`state_machine.py:329`) — *"Map merge_gate codes; never infer a
  route from aggregate PR findings"* — already routes review findings to `review_merge`
  and coord findings to `coordination_retry`, and deliberately **skips**
  `draft_pr`/`pr_not_mergeable` at `:340` so the draft branch owns them.
- The draft branch (`:619`) returns `draft_ready_to_mark_ready` / effect
  `mark_ready_then_reread`, and `completion_driver.mark_ready` (`:158`) already shells
  `gh pr ready`.

Nothing needs inventing. The typed machinery is complete; it is simply unreachable.

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

**1.4 The unifying change (CORRECTED): stop routing on the merge-authorization context.**
When that specific required context is red, **defer to the already-typed findings and the
draft branch** rather than classifying it.

*Justification:* merge authorization is not an independent CI signal — it is a
**projection of merge_gate findings already present on the snapshot**. Routing off it
double-counts the same evidence and destroys its type in transit through GitHub. It should
never have been a routing input, for any reason code. This single change unblocks the whole
Class A family, which is why 1.1–1.3 become optional hardening rather than the fix.

*Must fail closed:* if hydrate fails or findings come back empty, deferring would drop the
signal entirely and let a genuinely blocked PR look clean. Defer **only when findings are
present**; otherwise retain today's behaviour. Test this explicitly — a red merge-auth
context with zero findings must not become `ready_to_queue`.

### Rejected alternatives (verified in code — do not retry)

| Idea | Why it fails |
|---|---|
| Publish `failure_attribution` on the commit status | `post_status` (`switchboard_pr_gate.py:94`) carries only state/context/description/target_url. The GitHub commit-status API cannot hold structured attribution. |
| Add an `attribution="process"` value | `_required_ci_decision` understands only `product` \| `authority`/`policy` \| `infrastructure`. Anything else hits the else-branch → `required_ci_failure_unknown`, `route=human`. Stops the remediation burn, replaces it with a human page, never reaches `mark_ready`. |
| Another prose heuristic in `normalize.py` | The typed original is already in hand. Inferring from prose is what lost the type in the first place. |

*Worth keeping as diagnostics, not routing:* `switchboard_pr_gate.py:292` computes
`reason = blocked[0]["code"]` and discards it from the published status. Persist that code
in Switchboard's own ledger for operator visibility — never as the routing transport.

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

**Phase 1.4 + 2.1 together** (revised — 1.4 is now the root fix, not the capstone):

1. **Merge-auth red → defer to findings/draft** (classifier ordering). Unblocks Class A as
   a family, including the review-verdict and evidence cases that a draft-only fix misses.
2. **B3 dependency gate on `start_task`** (CO-25).
3. **`gh pr ready` in the worker completion contract** (ADAPTER-30) as belt-and-suspenders.
4. **Re-run DOGFOOD-19** as the Phase 1 acceptance test.

## Amendment history

**2026-07-25 — root cause corrected after operator code review.** The original framing was
*"the worker receives an undifferentiated failure and can only answer with a commit."* That
described the symptom the worker sees, not the mechanism, and led to two proposals that were
disproved in code (publishing attribution on the GitHub status; adding an `attribution=process`
value). The verified mechanism is classifier **ordering**: a red merge-authorization context
short-circuits `_required_ci_decision` before the typed review/findings/draft branches can run.
The observation that the publisher computes `blocked[0]["code"]` and discards it remains valid
as evidence, and is retained as an operator-diagnostics item rather than a routing fix.
Board task COORD-49 was retitled and rewritten to match.
