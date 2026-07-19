# Ingest / inbox — thin day-one surface (Mode A)

**Status:** Locked by [ADR-0016](../decisions/0016-ingest-inbox-process-strangler.md) (ARCH-MS-99).  
**Port:** **`:8126`** (after Tally `:8125`; avoids web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`, Coord `:8123`, Deliverables `:8124`).  
**Deliverable:** `arch-ms-ingest-service`

## Day-one routes (Ingest process)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox` | Inbox list + pending count |
| POST | `/api/intake` | Text artifact intake + triage queue |
| GET | `/health` | Cheap process health (when package lands) |

## Explicitly **not** day-one (stay on monolith)

- `POST /api/intake/upload` (transcribe / extract / media)
- `POST /api/inbox/{item_id}/confirm`
- `POST /api/inbox/confirm_all`
- `POST /api/inbox/{item_id}/dismiss`
- `POST /api/inbox/simulate`
- `POST /api/inbox/poll`
- MCP (`:8111`)
- Auth / Tasks / Coord / Deliverables / Tally process cuts — must not regress

## Dual-strip (ARCH-MS-121 Go cut)

Production sets `PM_INGEST_HTTP_PRIMARY=service` and mounts only sibling
Ingest routes that are **not** on the day-one list.

Deployment and rollback: [`docs/runbooks/ingest-caddy-cutover-rollback.md`](../runbooks/ingest-caddy-cutover-rollback.md).

## Side-by-side artifact (ARCH-MS-120)

- Example unit: `deploy/ingest/switchboard-ingest.service.example` (64 MiB cgroup cap)
- Example Caddy fragment under `deploy/` (not live until Go)
- Rollback runbook patterned after Tasks/Auth/Coord/Deliverables/Tally
