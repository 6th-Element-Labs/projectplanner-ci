# Tally / economics — thin day-one surface (Mode A)

**Status:** Locked by [ADR-0015](../decisions/0015-tally-economics-process-strangler.md) (ARCH-MS-98).  
**Port:** **`:8125`** (after Deliverables `:8124`; avoids web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`, Coord `:8123`).  
**Deliverable:** `arch-ms-tally-service`

## Day-one routes (Tally process)

| Method | Path | Purpose |
|---|---|---|
| GET | `/tally/v1/kpis` | KPI list |
| GET | `/tally/v1/outcomes` | Outcome list |
| GET | `/tally/v1/project` | Project economics rollup |
| GET | `/tally/v1/task/{task_id}` | Per-task tally |
| GET | `/tally/v1/kpi/{kpi_id}` | Per-KPI tally |
| GET | `/tally/v1/deliverable/{deliverable_id}` | Per-deliverable tally |
| GET | `/health` | Cheap process health (when package lands) |

## Explicitly **not** day-one (stay on monolith)

- `POST /tally/v1/spend/ingest`
- `POST /tally/v1/outcomes`, `…/verify`, `…/reject`
- `POST /tally/v1/kpis`, `PATCH /tally/v1/kpis/{kpi_id}`
- `POST /tally/v1/outcome_kpi_links`
- MCP (`:8111`) — including MCP `report_usage` until a later Mode B
- Auth / Tasks / Coord / Deliverables process cuts — must not regress

## Dual-strip (future Go cut)

When Go: production monolith sets `PM_TALLY_HTTP_PRIMARY=service` and mounts only sibling
Tally write routes that are **not** on the day-one list.

## Drill artifacts (later tasks)

- Example unit: `deploy/tally/switchboard-tally.service.example` (not live until Go)
- Example Caddy fragment under `deploy/` (not live until Go)
- Rollback runbook patterned after Tasks/Auth/Coord/Deliverables
