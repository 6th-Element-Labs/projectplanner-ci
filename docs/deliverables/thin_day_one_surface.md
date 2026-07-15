# Deliverables / mission — thin day-one surface (Mode A)

**Status:** Locked by [ADR-0014](../decisions/0014-deliverables-mission-process-strangler.md) (ARCH-MS-97).  
**Port:** **`:8124`** (after Coord `:8123`; avoids web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`).  
**Deliverable:** `arch-ms-deliverables-service`

## Day-one routes (Deliverables process)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/deliverables` | List / picker summaries |
| GET | `/api/deliverables/{deliverable_id}` | Deliverable detail |
| GET | `/api/mission_status` | Mission cockpit status |
| GET | `/api/deliverables/{deliverable_id}/mission_status` | Per-deliverable mission status |
| GET | `/api/deliverables/{deliverable_id}/closure_report` | Closure report **read** |
| GET | `/api/deliverables/{deliverable_id}/dependency_graph` | Dependency graph read |
| GET | `/api/deliverables/breakdown_proposals` | Breakdown proposals list |
| GET | `/api/deliverables/breakdown_proposals/{proposal_id}` | Breakdown proposal get |
| GET | `/health` | Cheap process health (when package lands) |

## Explicitly **not** day-one (stay on monolith)

- `POST /api/deliverables` (create), archive, outcome
- Milestone / task_link mutate
- `POST …/closure_verify`, `POST …/closure_request`
- `POST …/coordinator_tick`, `POST …/mission_brief`, `PATCH …/narrative`
- Breakdown create / approve / reject / defer / patch
- MCP (`:8111`)
- Auth / Tasks / Coord process cuts — must not regress

## Dual-strip (future Go cut)

When Go: production monolith sets `PM_DELIVERABLES_HTTP_PRIMARY=service` and mounts only
sibling Deliverables routes that are **not** on the day-one list.

## Drill artifacts (later tasks)

- Example unit: `deploy/deliverables/switchboard-deliverables.service.example` (not live until Go)
- Example Caddy fragment under `deploy/` (not live until Go)
- Rollback runbook patterned after Tasks/Auth/Coord
