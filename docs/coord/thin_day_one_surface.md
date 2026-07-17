# Coordination / board — thin day-one surface (Mode A)

**Status:** Live process cut (ARCH-MS-106), locked by [ADR-0013](../decisions/0013-coord-board-process-strangler.md).
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

## Dual-strip

Production sets `PM_COORD_HTTP_PRIMARY=service` and the monolith mounts only sibling
Coord/board routes that are **not** on the day-one list (Auth/Tasks dual-strip analogue).

## Deploy and rollback

- Production unit: `deploy/switchboard-coord.service`
- Exact live handles: `deploy/Caddyfile`
- Rollback: `docs/runbooks/coord-caddy-cutover-rollback.md`
