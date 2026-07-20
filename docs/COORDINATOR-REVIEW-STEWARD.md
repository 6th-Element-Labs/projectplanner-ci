# Coordinator Review Phase (COORD-5 / SIMPLIFY-2)

- **Status:** Internal phase of the lifecycle coordinator
- **Owner:** Coordinator Runtime
- **Relates to:** [COORDINATOR-CONTRACT](COORDINATOR-CONTRACT.md) §3 T2 · [COORDINATOR-AUDIT-LOOP](COORDINATOR-AUDIT-LOOP.md) · `review_steward.py` · `ci_scratchpad_dispatch.py` · COORD-6 escalation · COORD-7 merge steward

## Mandate

Keep **In Review** work moving toward a trustworthy green **without merging**:

1. Inspect board-recorded PR state, scratchpad `external_ci_run` evidence, deps, and unsafe sessions.
2. Auto-request scratchpad CI when evidence is missing, up to a bounded retry budget.
3. Call `Task Execution.start_task(role="remediation")` when CI is red.
4. Call `Task Execution.start_task(role="review_merge")` when CI is green and deps/session look clear.
5. Escalate only when bounded repair is exhausted or required evidence/authorization is unavailable.
6. **Never merge.** The following merge phase retains the mechanical gate.

## Lifecycle ownership

`coordinator_daemon.py` invokes this module inside its single leader tick. There is no
`jobs.py coordinator_review` entry point, queue, service, or timer. `dry_run=True`
remains available only for tests and diagnostics. Session placement and idempotency
belong to Task Execution; this phase never assembles a wake, injects a runner, or
chooses a host.

## Policy rules (COORD-3)

| Action | `policy_rule` |
|---|---|
| Rerun scratchpad CI | `coord.review.rerun_scratchpad` |
| Hold while CI pending | `coord.review.hold_pending_ci` |
| Dispatch review_merge | `coord.review.dispatch_review_merge` |
| Escalate human | `coord.review.escalate_human_judgment` |
| Hold for deps | `coord.review.hold_for_dependencies` |
| Repair unsafe session | `coord.review.repair_session` |
| Inspect missing PR/evidence | `coord.review.inspect_evidence` |

Every tick writes `switchboard.coordinator_decision.v1` rows (stable_key idempotent) and one `coordinator.review_steward.tick` activity payload.

## Acceptance mapping

| COORD-5 acceptance | Where |
|---|---|
| Inspect PR / CI / mergeability inputs | `plan_review_actions` over T0 snapshot |
| Auto-request mirror rerun on red/missing | `ACTION_RERUN_CI` → `try_dispatch_scratchpad` |
| Ensure remediation | `start_task(role="remediation")` |
| Ensure reviewer | `start_task(role="review_merge")` |
| Escalate only when automation cannot proceed | retry exhausted / missing PR or authorization |
| Never merge in this phase | `merges=False`; merge remains the next internal phase |

## Tests

```bash
python test_review_steward.py
```
