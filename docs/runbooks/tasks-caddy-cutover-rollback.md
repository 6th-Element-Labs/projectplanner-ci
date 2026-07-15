# Tasks Caddy cutover + rollback (ARCH-MS-92)

**Status:** Live Mode A cut. Independence gate records Go (G6) via
[`docs/phase3/tasks_independence_verdict.json`](../phase3/tasks_independence_verdict.json).

## Goal

Tasks uvicorn on `127.0.0.1:8122` owns Mode A traffic (`/api/tasks*` + claim-only
TXP). Sibling BC paths and MCP stay on the monolith.

## Deploy order (required)

1. Install/enable `deploy/switchboard-tasks.service` → confirm
   `curl -sS http://127.0.0.1:8122/health`.
2. Reload Caddy with live handles from `deploy/Caddyfile` (sibling carve before
   broad `/api/tasks*`).
3. Ensure monolith `PM_TASKS_HTTP_PRIMARY=service` then restart
   `projectplanner.service`.

Never reload Caddy Tasks handles while `:8122` is down (edge would 502 Mode A).

## Rollback

1. Remove Tasks/claim handles from Caddy (or point them at `:8110`); `caddy reload`.
2. Clear `PM_TASKS_HTTP_PRIMARY` on the monolith unit and restart web.
3. Stop or idle `switchboard-tasks`; confirm edge traffic on monolith.
4. Prefer fixing the Tasks unit over emergency remount.

## Pass criteria

| Check | Pass |
|---|---|
| Live Caddy | `/api/tasks*` → `:8122`; claim-only TXP → `:8122` |
| Sibling carve | dispatch/chat/review_* → `:8110` |
| Dual strip | `PM_TASKS_HTTP_PRIMARY=service`; Mode A not dual-mounted |
| Contract | Unauthenticated task writes → **401** (never 403) |
| MCP | Remains on `:8111` |
