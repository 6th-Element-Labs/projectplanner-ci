# Durable deliverable autopilot (COORD-8)

`coordinator_daemon.py` runs the existing deliverable mission coordinator as a
durable service profile. It owns one lifecycle decision stream: Ready tasks,
review, remediation, merge, and reconciliation all flow through the same leader.
Every task-session transition calls `Task Execution.start_task(role=...)`; the
Execution service alone owns idempotency, placement, assignment, and transport.

UI-27 makes the durable scope table the arming boundary. The shipped service is
effect-capable (`PM_COORDINATOR_AUTOPILOT_ACT=1`) but does no work while there
are no active scopes. An operator starts a deliverable or individual task from
the Deliverables UI; pause/resume/stop is durable per scope.

## Runtime contract

- One instance holds the `coordinator_leader` resource lease for a profile and
  project. A replacement waits until the old lease expires.
- Agent presence is registered as `runtime=coordinator-daemon` and heartbeated.
- State is stored under `coordinator_daemon.state:<profile>` in each project DB.
  The sequence and last scope are committed after each idempotent tick, so
  restart repeats at most the same idempotency key and never skips an effect.
- Active work comes only from `autopilot_scopes`. A deliverable scope fans out
  across its ready frontier; a task scope waits durably for its dependencies.
  Starting overlapping scopes is idempotent and a deliverable scope supersedes
  narrower task scopes for that deliverable.
- Deliverable replacement is a lifecycle operation, not a board-only edit.
  `update_deliverable(..., replacement_deliverable_id=...)` transfers the live
  scope in the same transaction, retaining its `scope_id`, decision history,
  runtime, and status while incrementing its generation. A conflicting live
  target fails closed. Archiving without a replacement explicitly stops the
  scope and atomically records an audit event plus an acknowledged operator
  message; silent scope loss is forbidden.
- Project and lane allowlists fail closed. Paused lanes are removed between
  deliverable effects; a project pause stops the whole project loop.
- Generic deliverable drain only selects blocking flow links, or nonblocking
  links explicitly marked `metadata.dispatch_eligible=true`. Foundation,
  historical, moved, skipped, parked, and `dispatch_eligible=false` links remain
  visible but are never generic automatic candidates.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PM_COORDINATOR_AUTOPILOT_PROFILE` | `autopilot-default` | Stable lease/control namespace |
| `PM_COORDINATOR_AUTOPILOT_PROJECTS` | `switchboard` | Project allowlist |
| `PM_COORDINATOR_AUTOPILOT_LANES` | empty (all) | Optional lane allowlist |
| `PM_COORDINATOR_AUTOPILOT_ACT` | `0` in code, `1` in service | Enable effects for active scopes; no scopes is idle |
| `PM_COORDINATOR_AUTOPILOT_POLL_SECONDS` | `30` | Loop interval |
| `PM_COORDINATOR_AUTOPILOT_HEARTBEAT_SECONDS` | `30` | Presence/lease heartbeat interval |
| `PM_COORDINATOR_AUTOPILOT_LEASE_TTL_SECONDS` | `120` | Leader failover TTL; at least 3x heartbeat |
| `PM_COORDINATOR_AUTOPILOT_MAX_DELIVERABLES` | `64` | Bound per-project work per tick |
| `PM_COORDINATOR_AUTOPILOT_MAX_TASKS_PER_SCOPE` | `64` | Ready-frontier fan-out bound per scope/tick |

## Operator controls

```bash
python coordinator_daemon.py status --project switchboard
python coordinator_daemon.py pause-project --project switchboard
python coordinator_daemon.py resume-project --project switchboard
python coordinator_daemon.py pause-lane --project switchboard --lane CO
python coordinator_daemon.py resume-lane --project switchboard --lane CO
python coordinator_daemon.py run --once
```

Controls are durable and audited as `coordinator.daemon.control`. The service
re-reads them between scope effects.

Normal operators use the Deliverables UI Start/Pause/Resume/Stop controls. The
project/lane CLI controls remain an administrative kill switch.

Board-surgery callers use the deliverable PATCH or archive endpoint with
`replacement_deliverable_id` and `scope_transition_reason`. Omitting the
replacement while archiving means "stop Autopilot" and surfaces that stop to
the operator; it never leaves an active scope attached to an archived outcome.
