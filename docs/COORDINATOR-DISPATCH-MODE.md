# COORD-4 T1 dispatcher — operator guide

- **Status:** Shipped with COORD-4 (dry-run default)
- **Tier:** T1 Dispatcher ([contract §3](COORDINATOR-CONTRACT.md))
- **Depends on:** [COORD-2 audit loop](COORDINATOR-AUDIT-LOOP.md), COORD-3 decision trail

## What it does

Each tick:

1. Opens a COORD-2 read-only snapshot / ranked plan for one project.
2. Selects ready, unblocked `consider_assignment` recommendations (lane allowlist aware).
3. Fails closed when no eligible host/agent exists; legacy human-gate metadata is non-blocking.
4. Optionally nudges live agent sessions whose heartbeat is older than the stale threshold.
5. Records a `switchboard.coordinator_decision.v1` row for **every** candidate (act or skip).
6. In act mode only: creates a wake via `dispatch.dispatch` and sends a directed claim-request message. It never claims or completes tasks.

## Safe defaults

| Knob | Default |
|------|---------|
| `dry_run` | `true` |
| `PM_COORDINATOR_DISPATCH_ACT` | unset / false |
| Claims | never |
| Self-claim | never |

Turn on acting only after dry-run decisions look right:

```bash
PM_COORDINATOR_DISPATCH_ACT=1 python coordinator_dispatch.py --project switchboard --act
```

## CLI

```bash
python coordinator_dispatch.py --project switchboard
python coordinator_dispatch.py --project switchboard --act --max-dispatches 2 --max-nudges 2
```

Env:

- `PM_COORDINATOR_DISPATCH_PROJECTS` — comma-separated project ids (default `switchboard`)
- `PM_COORDINATOR_DISPATCH_ACT` — `1` to act (otherwise dry-run)
- `PM_COORDINATOR_DISPATCH_ACTOR` — coordinator agent id (default `switchboard/coordinator-t1`)

## systemd

`deploy/projectplanner-coordinator-dispatch.timer` runs the oneshot service. Keep act mode off until operators review dry-run decision trails on `/api/coordinator_decisions`.

## REST

- `GET /api/coordinator_dispatch/plan?project=` — dry plan preview
- `POST /api/coordinator_dispatch` — body `{project, dry_run, policy}` runs one tick

## Decision trail

Policy rules used:

- `coord.dispatch.wake_ready_task`
- `coord.dispatch.nudge_stale_session`
- `coord.dispatch.no_host`
- `coord.dispatch.lane_allowlist`
