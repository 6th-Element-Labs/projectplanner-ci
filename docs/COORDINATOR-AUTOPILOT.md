# Durable deliverable autopilot (COORD-8)

`coordinator_daemon.py` runs the existing deliverable mission coordinator as a
durable service profile. It does not create a second claim/wake path: every
effect still flows through `run_mission_coordinator_tick`, `claim_next`, provider
capacity policy, host placement, review, and merge provenance gates.

The shipped systemd service is disarmed (`PM_COORDINATOR_AUTOPILOT_ACT=0`). In
that posture it refreshes briefs and records decisions but does not claim or wake
workers. Arm it only after setting an explicit project/lane policy and observing
the decision trail.

## Runtime contract

- One instance holds the `coordinator_leader` resource lease for a profile and
  project. A replacement waits until the old lease expires.
- Agent presence is registered as `runtime=coordinator-daemon` and heartbeated.
- State is stored under `coordinator_daemon.state:<profile>` in each project DB.
  The sequence and last deliverable are committed after each idempotent tick, so
  restart repeats at most the same idempotency key and never skips an effect.
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
| `PM_COORDINATOR_AUTOPILOT_ACT` | `0` | Enable worker claim/wake effects |
| `PM_COORDINATOR_AUTOPILOT_WORKER_AGENT` | empty | Existing worker identity to claim; empty uses a runtime wake |
| `PM_COORDINATOR_AUTOPILOT_WORKER_RUNTIME` | `codex` | Runtime selector for wake mode |
| `PM_COORDINATOR_AUTOPILOT_POLL_SECONDS` | `30` | Loop interval |
| `PM_COORDINATOR_AUTOPILOT_HEARTBEAT_SECONDS` | `30` | Presence/lease heartbeat interval |
| `PM_COORDINATOR_AUTOPILOT_LEASE_TTL_SECONDS` | `120` | Leader failover TTL; at least 3x heartbeat |
| `PM_COORDINATOR_AUTOPILOT_MAX_DELIVERABLES` | `8` | Bound per-project work per tick |

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
re-reads them between deliverable effects.
