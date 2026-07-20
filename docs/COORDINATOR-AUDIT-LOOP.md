# T0 coordinator audit loop

COORD-2 implements the first coordinator runtime mode: a scheduled, project-scoped observer that
turns current Switchboard state into a ranked next-action plan. It is deliberately weaker than a
worker. It recommends; it never claims, wakes, comments, runs reconcile, triggers CI, merges, or
edits a task.

The policy envelope is [Coordinator Agent — Operating Contract](COORDINATOR-CONTRACT.md). This
document describes the shipped T0 implementation in `coordinator_audit.py` and `jobs.py`.

## Safety boundary

The snapshot reader opens each existing board database with SQLite `mode=ro` and then sets
`PRAGMA query_only=ON`. Missing, corrupt, or schema-incompatible databases fail closed into a
critical escalation. They never become an empty green plan.

The planner is a pure transformation of that snapshot. Its receipt always records:

- `tier: "T0"` and `read_only: true`;
- `effects.work_state_executed: []`;
- a digest of its inputs and a digest of the ranked decision;
- every recommendation with `mutates: false`;
- the caveat that PR and CI facts came from board records, not live provider readback.

When logging is enabled, the scheduled wrapper performs one allowed write per selected project:
it appends the bounded plan as activity kind `coordinator.audit.plan`. That artifact does not
change task, claim, lease, monitor, Work Session, PR, CI, reconcile, or merge state. Disable even
that write with `PM_COORDINATOR_AUDIT_LOG=0` for a pure preview.

## Inputs and recommendation queues

Each project is read independently. No row is joined across project databases, and every audit
artifact is written back only to the project it describes.

| Board input | Output queue | Recommendation meaning |
|---|---|---|
| ready Not Started tasks, satisfied dependencies, no active claim | `assignment` | consider an assignment; do not dispatch |
| In Review plus missing/pending/red recorded CI or missing PR evidence | `review` | inspect or repair the review path |
| In Review plus green recorded CI, satisfied dependencies, safe session | `merge` | evaluate the canonical safe-merge gate; do not merge |
| status/provenance drift, missing canonical SHA, stale reconcile evidence | `reconcile` | ask the operator/reconcile authority to inspect or repair |
| expired claims/file leases/resource leases or a stale claim owner | `stale_claim` | inspect and release through the normal authority path |
| fired monitors, missing hosts, unsafe sessions/read failures | `escalation` | surface the named failure class when automation cannot repair it |

The loop also observes active agent and host heartbeats, Work Session hygiene, task dependencies,
recorded PR state, the latest recorded external CI run per task, durable monitors, and recent
reconcile evidence. Recommendations are globally ranked by severity and task risk/blocking signal,
then sorted deterministically for stable audit output.

An `evaluate_safe_merge_gate` recommendation is intentionally not a claim that a PR can merge.
T0 has no network access and does not read GitHub live. A T3 coordinator or human must still run
the canonical merge gate and provider readback before any merge.

## Schedule and configuration

`projectplanner-coordinator-audit.timer` starts after two minutes and then runs every five minutes
with a small randomized delay. Its oneshot service runs in `projectplanner-batch.slice`, is capped
at 128 MiB, and restricts address families to `AF_UNIX`; the observer cannot reach network
providers.

Configuration in `/opt/projectplanner/.env`:

| Variable | Default | Purpose |
|---|---:|---|
| `PM_COORDINATOR_AUDIT_PROJECTS` | `switchboard` | comma-separated project IDs; `all` selects every registered project |
| `PM_COORDINATOR_AUDIT_LOG` | `1` | append one plan activity per project; set `0` for no persistence |
| `PM_COORDINATOR_AUDIT_MAX_RECOMMENDATIONS` | `100` | bound the persisted recommendation list |
| `PM_COORDINATOR_AUDIT_RECONCILE_STALE_SECONDS` | `900` | age at which missing recent reconcile evidence is recommended |
| `PM_COORDINATOR_AUDIT_ACTOR` | `switchboard/coordinator-t0` | attributable activity actor |

Manual preview without an activity write:

```bash
cd /opt/projectplanner
sudo -u projectplanner env PM_COORDINATOR_AUDIT_LOG=0 \
  .venv/bin/python jobs.py coordinator_audit
```

Run once with the normal auditable artifact:

```bash
cd /opt/projectplanner
sudo -u projectplanner .venv/bin/python jobs.py coordinator_audit
```

Inspect the schedule and latest receipt:

```bash
systemctl list-timers projectplanner-coordinator-audit.timer
journalctl -u projectplanner-coordinator-audit.service -n 50 --no-pager
```

The job exits non-zero when a selected project cannot be read or its plan artifact cannot be
persisted. The receipt is still printed, including the failing project and structured error class.
This keeps failure visible to systemd rather than silently treating partial observation as health.

## Verification

`test_coordinator_audit.py` creates a representative board database and proves that:

- snapshot collection leaves the database bytes, mtime, and activity count unchanged;
- all six required recommendation queues can be produced and ranked deterministically;
- missing read state fails closed into a critical escalation;
- the scheduled wrapper invokes only one injected activity writer and no work-state effect;
- the shipped systemd service has no network address family and the timer is persistent.
