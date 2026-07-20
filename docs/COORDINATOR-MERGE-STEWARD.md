# Coordinator Merge and Reconcile Phase (COORD-7 / SIMPLIFY-2)

- **Status:** Internal mechanically gated phase of the lifecycle coordinator
- **Owner:** Coordinator Runtime
- **Relates to:** [COORDINATOR-CONTRACT](COORDINATOR-CONTRACT.md) §3 T3 · [COORDINATOR-REVIEW-STEWARD](COORDINATOR-REVIEW-STEWARD.md) · [COORDINATOR-ESCALATION](COORDINATOR-ESCALATION.md) · `merge_steward.py` · `merge_coordinator.py` · `store.merge_gate`

## Mandate

Merge phase for eligible **In Review** PRs:

1. Inspect board-recorded PR / CI / dependency / session state (T0 snapshot). Task risk labels remain informational.
2. Hold red/unknown checks, conflicts, stale branches, and missing provenance in the agent remediation loop; these routine failures never page a human.
3. Arm GitHub auto-merge only when exact-head CI/review, dependencies, mergeability, and the merge gate pass under the backpressure cap.
4. After arming, request `reconcile` so Done provenance can land via webhook — **never set Done directly**.
5. If a merge SHA is already recorded but canonical provenance is missing, reconcile immediately.

## Lifecycle ownership

`coordinator_daemon.py` invokes this module after the review phase in the same
leader tick and decision stream. There is no `jobs.py coordinator_merge` entry
point, queue, service, or timer. `dry_run=True` remains available only for tests
and diagnostics.

### Policy env

| Variable | Default | Meaning |
|---|---|---|
| `PM_COORDINATOR_MERGE_LOG` | `1` | Persist activity + decisions |
| `PM_COORDINATOR_MERGE_MAX_IN_FLIGHT` | `3` | Backpressure arm budget |
| `PM_COORDINATOR_MERGE_SATURATED` | `0` | Force hold-all backpressure |
| `PM_COORDINATOR_MERGE_IN_FLIGHT` | `0` | Current in-flight merges for budget math |
| `PM_COORDINATOR_MERGE_ACTOR` | `switchboard/coordinator-t3` | Decision/activity actor |
| `PM_COORDINATOR_MERGE_OPERATOR` | `switchboard/operator` | Escalation mailbox |

## Policy rules (COORD-3)

| Action | `policy_rule` |
|---|---|
| Arm auto-merge | `coord.merge.arm_auto_merge` |
| Hold pending CI | `coord.merge.hold_pending_ci` |
| Hold dependencies | `coord.merge.hold_for_dependencies` |
| Hold backpressure | `coord.merge.hold_backpressure` |
| Hold mechanical gate | `coord.merge.hold_mechanical_gate` |
| Verify post-merge | `coord.merge.verify_post_merge_provenance` |

Every tick writes `switchboard.coordinator_decision.v1` rows and one
`coordinator.merge_steward.tick` activity payload.

## Acceptance mapping

| COORD-7 acceptance | Where |
|---|---|
| No separate approval stage | lifecycle coordinator act mode; exact-head mechanical gates decide eligibility |
| Branch/checks/provenance gates | board CI + injectable/`store.merge_gate` |
| Red/unknown/conflicts/stale/missing provenance fail closed | recorded hold; remediation/reconcile stays in the agent loop |
| Successful merge → reconcile / Done verification | post-arm and already-merged `reconcile`; never set Done directly |
| Single owner | called only by `CoordinatorDaemon._drain_lifecycle` in production |

## Tests

```bash
python test_merge_steward.py
```
