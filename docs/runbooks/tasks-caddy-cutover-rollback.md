# Tasks Caddy cutover + rollback drill (ARCH-MS-89 → ARCH-MS-92 when Go)

**Status:** Drill / dry-run only. **No live cut.** Independence gate
[`TASKS-INDEPENDENCE-GATE.md`](../TASKS-INDEPENDENCE-GATE.md) must record Go
(including operator G6) before ARCH-MS-90…92 apply traffic.

Reference fragment (commented):
[`deploy/skeleton/Caddyfile.tasks-fragment.example`](../../deploy/skeleton/Caddyfile.tasks-fragment.example).

Unit example:
[`deploy/tasks/switchboard-tasks.service.example`](../../deploy/tasks/switchboard-tasks.service.example).

## Goal

Prove that a future Tasks uvicorn on `127.0.0.1:8122` can take Mode A traffic
(`/api/tasks*` + claim-only TXP) with a documented rollback that restores the
monolith (`:8110`) without a half-cut façade.

## Preconditions (not satisfied until Go)

1. Independence gate G1–G5 measured; operator G6 recorded on the board.
2. ARCH-MS-90 Tasks `create_app` + systemd unit healthy on `:8122`.
3. ARCH-MS-91 side-by-side parity green.
4. Prefer `bash deploy/redeploy.sh` patterns proven for Auth.

## Cutover (future — ARCH-MS-92)

1. Enable `switchboard-tasks` on `127.0.0.1:8122`; `curl -sS http://127.0.0.1:8122/health`.
2. Paste Mode A handles from the fragment **above** the catch-all; carve
   `…/dispatch`, `…/chat`, `…/review_*` back to `:8110`.
3. Claim-only TXP paths only — never blanket `/txp/v1/*`.
4. `caddy validate` + reload; smoke list/create/claim; 401 contract for
   unauthenticated writes (never 403 for missing session).
5. Dual-strip with `PM_TASKS_HTTP_PRIMARY=service` after parity.

## Rollback

1. Remove Tasks handles from Caddy (or point them back to `:8110`); `caddy reload`.
2. Stop or leave `switchboard-tasks` idle; confirm edge traffic on monolith.
3. Prefer fixing the Tasks unit over emergency remount; if remounting, clear
   `PM_TASKS_HTTP_PRIMARY` and redeploy monolith.

## Pass criteria (drill)

| Check | Pass |
|---|---|
| Artifacts present | Fragment + this runbook + unit example |
| Live Caddy | **No** premature `/api/tasks*` → `:8122` until Go |
| Contract | Unauthenticated task writes → **401** (never 403) |
| Surface | Mode A thin; siblings stay on monolith |

## Fail / No-Go signals

- Live cut applied before independence Go
- Multi-process SQLite writers unsafe (see ARCH-MS-89 harness)
- 403 returned for missing authentication on task writes
- Need to restart Caddy (not just reload) to recover
