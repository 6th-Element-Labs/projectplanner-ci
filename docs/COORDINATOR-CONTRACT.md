# Coordinator operating contract

- **Status:** SIMPLIFY-15 single completion owner, finalized by SIMPLIFY-11.
- **Owner:** Product / Control Plane
- **Architecture:** [ADR-0008](decisions/0008-three-plane-separation.md)

The coordinator owns one explicit scope at a time. It plans work and invokes
Task Execution; it is not a runner, launcher, liveness authority, or process
reaper.

## Non-bypassable rules

1. Every implementation, review, and remediation generation starts through
   `Task Execution.start_task`.
2. Task Execution owns attach, dedupe, execution identity, open, message, retry,
   stop, and the durable completion run.
3. `runner_sessions` is the physical execution registry. The browser and
   coordinator read the Task Execution projection and never select a runner.
4. The renewable execution lease is the only automatic process-stop clock.
   Explicit operator stop and fail-closed spawn cleanup are the only exceptions.
5. The scoped completion coordinator owns review, remediation, exact-head
   validation, merge queueing, reconciliation, and canonical Done provenance.
6. The global coordinator daemon is janitor-only. It may report or repair
   bookkeeping, but it cannot start work, schedule review, or arm merges.
7. Messages are durable communication only. Timeout, retry, or offline delivery
   cannot create a wake or mutate task/execution lifecycle.
8. No agent or coordinator marks a PR-backed task Done. GitHub merge provenance
   plus reconciliation owns that transition.

## Authority layers

| Layer | Owns | Does not own |
|---|---|---|
| Coordination | plan, scope lease, dependency decisions, escalation | process liveness, execution identity |
| Task Execution | generation admission, runner projection, control, completion run | process signals |
| Agent Host | supervised process and renewable execution lease | task status, review, merge |

## Safe merge

The completion owner may enqueue a merge only after exact-head review,
required checks, work-session hygiene, canonical repository validation, and
`merge_gate` pass. GitHub's merge queue owns the merge strategy. Reconciliation
records the resulting default-branch SHA exactly once.

## Escalation

Blocked gates, unavailable capacity, permission failures, budget limits,
conflicts, and provenance drift remain visible first-class states. The
coordinator may create a fresh remediation generation through Task Execution;
it may not bypass or hide the failed condition.
