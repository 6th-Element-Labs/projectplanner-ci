# Coordinator Merge Steward (COORD-7 / T3)

- **Status:** Implemented (dry-run default; policy-gated)
- **Owner:** Coordinator Runtime / Release Policy
- **Relates to:** [COORDINATOR-CONTRACT](COORDINATOR-CONTRACT.md) §3 T3 · [COORDINATOR-REVIEW-STEWARD](COORDINATOR-REVIEW-STEWARD.md) · [COORDINATOR-ESCALATION](COORDINATOR-ESCALATION.md) · `merge_steward.py` · `merge_coordinator.py` · `store.merge_gate`

## Mandate

Optional **T3 merge steward** for eligible **In Review** PRs:

1. Inspect board-recorded PR / CI / dependency / session state (T0 snapshot). Task risk labels remain informational.
2. Fail closed to COORD-6 escalation for red/unknown checks, conflicts, stale branches, missing provenance, human gates, and missing authority.
3. When policy is **enabled** and **authority_granted**, arm GitHub auto-merge only for eligible green PRs under the backpressure cap.
4. After arming, optionally request `reconcile` so Done provenance can land via webhook — **never set Done**.
5. Default posture is dry-run.

## Run

```bash
# Dry-run (default): plan + COORD-3 decisions + activity artifact
python jobs.py coordinator_merge

# Acting (operator-approved)
PM_COORDINATOR_MERGE_ACT=1 \
PM_COORDINATOR_MERGE_ENABLED=1 \
PM_COORDINATOR_MERGE_AUTHORITY=1 \
  python jobs.py coordinator_merge
```

### Env

| Variable | Default | Meaning |
|---|---|---|
| `PM_COORDINATOR_MERGE_PROJECTS` | `switchboard` | CSV / `all` project allowlist |
| `PM_COORDINATOR_MERGE_ACT` | `0` | `1` enables mutating effects |
| `PM_COORDINATOR_MERGE_LOG` | `1` | Persist activity + decisions |
| `PM_COORDINATOR_MERGE_ENABLED` | `0` | Policy switch: allow arming when authority also granted |
| `PM_COORDINATOR_MERGE_AUTHORITY` | `0` | Explicit T3 authority grant |
| `PM_COORDINATOR_MERGE_MAX_IN_FLIGHT` | `3` | Backpressure arm budget |
| `PM_COORDINATOR_MERGE_SATURATED` | `0` | Force hold-all backpressure |
| `PM_COORDINATOR_MERGE_IN_FLIGHT` | `0` | Current in-flight merges for budget math |
| `PM_COORDINATOR_MERGE_ACTOR` | `switchboard/coordinator-t3` | Decision/activity actor |
| `PM_COORDINATOR_MERGE_OPERATOR` | `switchboard/operator` | Escalation mailbox |

Systemd units: `deploy/projectplanner-coordinator-merge.{service,timer}` (timer every 5 minutes; service ships with `PM_COORDINATOR_MERGE_ACT=0`).

## Policy rules (COORD-3)

| Action | `policy_rule` |
|---|---|
| Arm auto-merge | `coord.merge.arm_auto_merge` |
| Hold pending CI | `coord.merge.hold_pending_ci` |
| Hold dependencies | `coord.merge.hold_for_dependencies` |
| Hold backpressure | `coord.merge.hold_backpressure` |
| Hold policy disabled | `coord.merge.hold_policy_disabled` |
| Escalate blocked gate | `coord.merge.escalate_blocked_gate` |
| Verify post-merge | `coord.merge.verify_post_merge_provenance` |

Every tick writes `switchboard.coordinator_decision.v1` rows and one
`coordinator.merge_steward.tick` activity payload.

## Acceptance mapping

| COORD-7 acceptance | Where |
|---|---|
| Policy must explicitly allow merge | `enabled` + `authority_granted` |
| Branch/checks/provenance gates | board CI + injectable/`store.merge_gate` |
| Red/unknown/conflicts/stale/missing provenance fail closed | escalate via COORD-6 |
| Human gate / missing authority fail closed | escalate |
| Successful merge → reconcile / Done verification | post-arm `reconcile`; never set Done |
| Dry-run default | `steward_project(dry_run=True)` / `PM_COORDINATOR_MERGE_ACT=0` |

## Tests

```bash
python test_merge_steward.py
```
