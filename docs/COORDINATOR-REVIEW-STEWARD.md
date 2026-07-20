# Coordinator Review Steward (COORD-5 / T2)

- **Status:** Implemented (dry-run default)
- **Owner:** Coordinator Runtime
- **Relates to:** [COORDINATOR-CONTRACT](COORDINATOR-CONTRACT.md) §3 T2 · [COORDINATOR-AUDIT-LOOP](COORDINATOR-AUDIT-LOOP.md) · `review_steward.py` · `ci_scratchpad_dispatch.py` · COORD-6 escalation · COORD-7 merge steward

## Mandate

Keep **In Review** work moving toward a trustworthy green **without merging**:

1. Inspect board-recorded PR state, scratchpad `external_ci_run` evidence, deps, and unsafe sessions.
2. Auto-request scratchpad CI (`dispatch_scratchpad` / `request_external_ci_mirror_run`) when CI is red or missing, up to a bounded retry budget.
3. Dispatch a `review_merge/{task_id}` agent (message + `request_wake(mode=message_only)`) when CI is green and deps/session look clear.
4. Escalate only when bounded repair is exhausted or required evidence/authorization is unavailable.
5. **Never merge.** Merges stay behind COORD-7 / T3 `merge_coordinator` + `merge_gate`.

## Run

```bash
# Dry-run (default): plan + COORD-3 decisions + activity artifact, no side effects
python jobs.py coordinator_review

# Acting (operator-approved)
PM_COORDINATOR_REVIEW_ACT=1 python jobs.py coordinator_review
```

### Env

| Variable | Default | Meaning |
|---|---|---|
| `PM_COORDINATOR_REVIEW_PROJECTS` | `switchboard` | CSV / `all` project allowlist |
| `PM_COORDINATOR_REVIEW_ACT` | `0` | `1` enables mutating effects |
| `PM_COORDINATOR_REVIEW_LOG` | `1` | Persist activity + decisions |
| `PM_COORDINATOR_REVIEW_MAX_CI_RERUNS` | `2` | Terminal CI attempts before escalate |
| `PM_COORDINATOR_REVIEW_ACTOR` | `switchboard/coordinator-t2` | Decision/activity actor |
| `PM_COORDINATOR_REVIEW_OPERATOR` | `switchboard/operator` | Escalation mailbox |
| `PM_COORDINATOR_REVIEW_RUNTIME` | `cursor` | Wake selector runtime for review_merge |

Systemd units: `deploy/projectplanner-coordinator-review.{service,timer}` (timer every 5 minutes; service ships with `PM_COORDINATOR_REVIEW_ACT=0`).

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
| Dispatch review_merge | message + wake `kind=review_merge` |
| Escalate only when automation cannot proceed | retry exhausted / missing PR or authorization |
| Never merge by default | `merges=False`; no `merge_coordinator` arm |

## Tests

```bash
python test_review_steward.py
```
