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

Prefer `bash deploy/redeploy.sh` (ARCH-MS-101): it enables/starts `switchboard-tasks`,
proves `:8122` health, and only then syncs Caddy through
`deploy/sync_caddy_fail_closed.sh`. A dead Tasks unit leaves the prior live
`/etc/caddy/Caddyfile` untouched. Post-deploy exact-SHA / unit / listener / health /
edge evidence: `scripts/verify_runtime_deploy.py`.

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
| Contract | Unauthenticated task **reads** → **401** (never 200) — BUG-69 |
| MCP | Remains on `:8111` |

**BUG-69 (fixed by ARCH-MS-125):** the write-only contract row above is not
sufficient on its own. `:8122`'s reads (`list_tasks`/`get_task`) have no
route-level auth check at all — they rely entirely on the process registering
the same global auth gate the monolith does
(`switchboard.api.middleware.register_auth_gate`, called from
`switchboard.services.tasks.app::create_app`). A Tasks build that passes the
write check above can still leak every task anonymously on reads; this shipped
to prod twice (2026-07-15, 2026-07-17) before the read row above and the
automated `check_anon_read_rejected` probe in `scripts/verify_runtime_deploy.py`
existed. `deploy/redeploy.sh` treats `switchboard-tasks` as a required always-on
service and re-applies this cutover on every redeploy — do not assume a quiet
`git pull` leaves the cutover untouched; the runtime proof is what actually
guards it now, not operator memory.
