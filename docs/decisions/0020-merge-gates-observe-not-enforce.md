# ADR-0020 — Merge gates observe; GitHub enforces; dispatch is where Switchboard says no

- **Status:** Accepted (operator decision, 2026-07-25). Ruleset change already applied.
- **Date:** 2026-07-25
- **Author:** ADAPTER-28 birth-to-merge session (Claude), operator-approved
- **Relates to:** [AUTOPILOT-COMPLETION-STATE-MACHINE.md](../AUTOPILOT-COMPLETION-STATE-MACHINE.md)
  (the approved design this ADR restores) · PR #902 (the merge that surfaced the failure) ·
  the fail-fix visible-fallback rule in the working agreement

> **One sentence:** the completion state machine is a **router** that reads GitHub state and
> decides the coordinator's next action; it is never itself a required GitHub merge gate —
> `Switchboard / merge authorization` is therefore advisory, GitHub's own required contexts
> plus the merge queue are the merge bar, and Switchboard's enforcement lives at **dispatch**
> (claims, generation fences, workspace verification), not between a green PR and master.

---

## Context

The approved completion state machine says its inputs — "GitHub PR state, CI, review
verdicts, mergeability, merge queue state, board status, claims, Work Sessions, and runner
state — are inputs or projections. **None of them independently decides** what work to
start next." Its merge bar is precedence rule 9: a clean, green, exact-head snapshot
advances to `READY_TO_QUEUE`; the merge queue re-runs the suite on the merge commit and is
the real landing gate.

Later, `Switchboard / merge authorization` — a projection of `merge_gate` — was promoted
into a **required GitHub status context** on the canonical repo. That inverted the
architecture: instead of GitHub state flowing into our router, board state became a hard
gate inside GitHub. On PR #902 (all substantive checks green) this produced:

- **A self-deadlock.** The gate consumed GitHub's aggregate `mergeable_state: blocked` as a
  blocking finding, while the *cause* of that aggregate was the gate's own red status —
  a direct violation of the design's rule 7 ("mergeability is decomposed into its
  underlying cause").
- **Hours of evidence-dialect archaeology.** Four consecutive refusals for correct facts in
  the wrong key spelling (verdict findings schema, missing `output_hash`/`completed_at`,
  a diff-check object without an `ok:` verdict field, a Work Session with an empty
  `head_sha` the coordinator could not populate because it cannot stat host worktrees).
- **No re-evaluation path.** The posted status only recomputes on certain GitHub events;
  board-side evidence repairs never re-trigger it, so a fully-satisfied PR sat red
  indefinitely.
- **Zero caught defects.** The only real bug in the change (a test needing a git identity
  on a clean runner) was caught by plain CI.

## Decision

1. **`Switchboard / merge authorization` is advisory.** It is removed from the required
   status contexts on the canonical default branch and keeps being posted for
   observability. The required contexts are `Switchboard CI / VM gate` and
   `Switchboard UI / Playwright`; the merge queue (ALLGREEN, squash) re-runs the suite on
   the merge commit and remains the landing authority. Re-promoting the context is a
   one-line ruleset change and requires a new ADR.
2. **Rule-7 compliance in `merge_gate`.** Aggregate mergeability states
   (`blocked`/`unstable`/`unknown`) are decomposed and never blocking by themselves; only
   real conflicts (`mergeable: false`, `dirty`/`conflicting`) block. The gate must never
   consume its own posted status as an input.
3. **Evidence-shape checks warn, they do not deny.** In `code_strict` completion, test-run
   and diff-check *shape* validation produces named `evidence_warnings` recorded into
   completion evidence (the visible-fallback rule), not denials. CI on the exact SHA is
   the executor of record for "the tests ran." Identity and provenance checks — branch,
   `head_sha`, push-verification — remain hard denials.
4. **`review_merge` stays enabled.** An earlier draft of this decision disabled the review
   route as a dead executor. That premise went stale: BUG-187's fix (#894) made Connect
   runners fenceable and real exact-head verdicts are landing again (CO-21, COORD-52).
   Self-review by the authenticated implementer is the documented contract, and review
   episodes feed the decision corpus (COORD-50/51/52). With the required status dropped,
   the route cannot deadlock a merge; if it proves noisy under real traffic, disable it
   *then*, with evidence, via a follow-up decision.

## The principle

**Enforce at dispatch, observe at merge, stamp at Done.**

- *Dispatch* is where Switchboard says no: atomic claims, execution-lease generations and
  fences, immutable execution assignments, workspace verification (ADAPTER-27/28). Refusing
  bad work before it runs prevents waste.
- *Merge* is GitHub's jurisdiction: required contexts on the exact head plus the merge
  queue's re-run. A second, board-side bureaucracy there can only add latency or deadlock —
  the work is already done and proven.
- *Done* stays webhook/reconcile-stamped from the canonical `merged_sha`. Board truth about
  what landed is untouched by this ADR.

The completion state machine keeps its whole job: classify one route
(`wait/review_merge/remediation/coordination_retry/reconcile/human/none`) from observed
state and act. Nothing in this ADR removes a state, a route, or a transition.

## Consequences

- A green PR merges in minutes. The state machine still auto-remediates red CI, still
  reconciles merged PRs, still escalates authority problems to humans.
- The advisory status becomes trustworthy (it no longer reports its own reflection) and is
  the auditable yellow signal the fail-fix policy requires for downgraded checks.
- Follow-up work tracked on the board: one canonical completion-evidence call that fans out
  to every read surface (kills the key-spelling class), and the `merge_gate` PR-identity
  fallback to `task.git_state` (the data-present-but-not-consulted pattern).
