# DBOS background-job evaluation (RECON-10)

This document records the RECON-10 evaluation of [DBOS](https://www.dbos.dev/) as
**invisible infrastructure** for slow, resumable Switchboard background work. It
implements the boundary in [`docs/SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md`](SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md) §4.4.

## Verdict

**Ship the local SQLite checkpoint runner now.** DBOS remains an optional runtime adapter
for hosted, long-running jobs once provider reconciliation (`TALLY-4`) and dispatch
scorecards (`DISPATCH-7`) land.

| Runtime | When to use |
|---------|-------------|
| `local_checkpoint` (default) | systemd timers, VM jobs, dev — persists step manifests in `background_job_runs` |
| `dbos` (`SWITCHBOARD_JOB_RUNTIME=dbos`) | Optional when the `dbos` package is installed and a job exceeds timer tolerance or needs cross-process orchestration |

The hot coordination kernel **does not** move onto DBOS:

- `claim_next`, exact claims, message delivery/ack
- task/resource leases, heartbeats
- activity append and core provenance writes

## Implemented surface

| Component | Purpose |
|-----------|---------|
| `background_jobs.py` | Job catalog, boundary guard, step checkpoint runner |
| `background_job_runs` table | Per-project persisted run manifests |
| REST `/ixp/v1/background_jobs/*` | Catalog, evaluate, run, list runs |
| MCP `list_background_jobs`, `run_background_job`, … | Agent/operator access |
| `python jobs.py background_job <name>` | systemd-friendly entry |

### Eligible jobs (DBOS-ready, checkpointed today)

| Job | Steps | Mutates hot path? |
|-----|-------|-------------------|
| `replay_verify_batch` | `event_replay.verify_board` per project | No |
| `audit_export_batch` | `store.audit_export` per project | No |
| `receipt_projection_batch` | `coordination_receipts.list_*` per project | No |
| `reconcile_alerts_resumable` | `store.run_reconcile_alerts` per project | No (alerts only) |

## Acceptance mapping (borrowing map §7)

1. **Crash/resume without protocol change** — `test_background_jobs.py` simulates crash after step 0 and proves resume skips completed steps; public MCP/REST contracts unchanged.
2. **Hot path independence** — `FORBIDDEN_HOT_PATH_OPERATIONS` blocks scheduling kernel ops; `evaluate_dbos_runtime().hot_path_independent` is always true.
3. **Receipt/replay integration** — replay and receipt jobs wrap RECON-8/9 modules read-only.

## DBOS adoption criteria (future)

Adopt DBOS for a job only when **all** apply:

1. Job routinely exceeds single systemd timer budget (>5–15 minutes).
2. Job needs durable workflow semantics across multiple hosts.
3. Job side effects are already idempotent or guarded by `external_side_effects` (HARDEN-21).
4. Load tests show DBOS checkpoint latency does not block operator-facing APIs.

Until then, the local checkpoint runner satisfies RECON-10 without adding a production
dependency.

## Configuration

```bash
# Default — SQLite checkpoints in the project DB
export SWITCHBOARD_JOB_RUNTIME=local_checkpoint

# Optional — requires `pip install dbos` and hosted DBOS config
export SWITCHBOARD_JOB_RUNTIME=dbos

# jobs.py helper
python jobs.py background_job replay_verify_batch
```

## Related tasks

- `RECON-8` — replay harness (feeds `replay_verify_batch`)
- `RECON-9` — coordination receipts (feeds `receipt_projection_batch`)
- `HARDEN-13` — audit export (feeds `audit_export_batch`)
- `TALLY-4` — provider cost reconciliation (future DBOS candidate)
- `DISPATCH-7` — dispatch policy scorecards (future DBOS candidate)
