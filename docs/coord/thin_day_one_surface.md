# Coordination / board — thin day-one surface (Mode A)

**Status:** Locked by [ADR-0013](../decisions/0013-coord-board-process-strangler.md) (ARCH-MS-96).  
**Port:** **`:8123`** (suggested; avoids web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`).  
**Deliverable:** `arch-ms-coord-service`

## Day-one routes (Coord process)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/board` | Board summary (lite/cards views OK) |
| GET | `/api/signals` | Plan signals / health |
| GET | `/ixp/v1/delta` | Lane delta |
| GET | `/api/coordination` | Read-only coordination rollup |
| GET | `/api/coordinator_decisions` | Coordinator decision trail (read) |
| GET | `/health` | Cheap process health (when package lands) |

## Explicitly **not** day-one (stay on monolith)

- `GET /api/people`, `GET /api/dispatch/status`
- `GET /ixp/v1/saturation_signals`
- `GET|POST /api/coordinator_dispatch*` (write / dry-plan acting paths)
- Agents, messaging, wakes, monitors (except `/ixp/v1/delta`), resource leases, work sessions
- Deliverables / mission HTTP
- MCP (`:8111`)
- Auth (`:8121`) and Tasks Mode A (`:8122`) — must not regress

## Dual-strip (future Go cut)

When Go: production monolith sets `PM_COORD_HTTP_PRIMARY=service` and mounts only sibling
Coord/board routes that are **not** on the day-one list (Auth/Tasks dual-strip analogue).

## Drill artifacts (later tasks)

- Example unit: `deploy/coord/switchboard-coord.service.example` (not live until Go)
- Example Caddy fragment under `deploy/` (not live until Go)
- Rollback runbook patterned after Tasks/Auth
