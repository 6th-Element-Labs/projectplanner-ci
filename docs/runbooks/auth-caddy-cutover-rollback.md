# Auth Caddy cutover + rollback drill (ARCH-MS-84 → ARCH-MS-76 live → ARCH-MS-77)

**Status:** Live cut is **ARCH-MS-76** — `deploy/Caddyfile` routes `/api/auth*` →
`switchboard-auth` on `127.0.0.1:8121`. **ARCH-MS-77** production dual-strip:
`deploy/projectplanner.service` sets `PM_AUTH_HTTP_PRIMARY=service` so the
monolith does not mount Auth HTTP (shared package + `/api/auth/me` only).
`/api/auth/me*` stays on monolith `:8110` via a preceding Caddy handle.

Independence gate: [`AUTH-INDEPENDENCE-GATE.md`](../AUTH-INDEPENDENCE-GATE.md).

Reference fragment (commented):
[`deploy/skeleton/Caddyfile.auth-fragment.example`](../../deploy/skeleton/Caddyfile.auth-fragment.example).

## Goal

Prove that Auth HTTP is served from the Auth uvicorn, with hermetic parity tests
and no second live copy of Auth route logic on the monolith.

## Preconditions

1. Independence gate G1–G6 Go (ARCH-MS-75 recorded operator Go).
2. `switchboard-auth.service` installed and healthy on `127.0.0.1:8121`.
3. Prefer `bash deploy/redeploy.sh` (starts Auth, proves health, then reloads Caddy).

## Cutover (production / Plan VM)

1. `sudo systemctl enable --now switchboard-auth`
2. `curl -sS http://127.0.0.1:8121/health` → `{"status":"ok",...}`
3. Confirm `deploy/Caddyfile` contains:
   - `handle /api/auth/me*` → `127.0.0.1:8110` (monolith thin me)
   - `handle /api/auth*` → `127.0.0.1:8121` (Auth service) **above** catch-all
4. `caddy validate --adapter caddyfile --config deploy/Caddyfile`
5. `sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`
   (or run `bash deploy/redeploy.sh`).
6. Smoke through the public edge (`https://plan.taikunai.com`):
   - `GET /api/auth/session` (unauthenticated → **401**; never 403)
   - `POST /api/auth/login` with bad password → **401**
   - Register / login / logout happy path
7. Record wall-clock from reload to first green smoke (target: < 60s).

See also the checklist in [`deploy/PROVISION.md`](../../deploy/PROVISION.md).

## Recovery / rollback (post ARCH-MS-77)

**Primary:** Auth on `:8121` is the Auth HTTP source of truth. If the edge is
wrong, fix Caddy / restart `switchboard-auth` — do not expect monolith `:8110` to
serve `/api/auth/login|session|…` anymore.

1. Confirm `systemctl is-active switchboard-auth` and `curl -sS http://127.0.0.1:8121/health`.
2. If Caddy mishandled: restore `deploy/Caddyfile` Auth handles and `caddy reload`.
3. Re-run the smoke suite against the edge.

**Emergency remount (rare):** temporarily re-`include_router` the shared Auth
router in `app_impl.py`, remove the `/api/auth*` → `:8121` handle (keep `/api/auth/me*`
or fold into catch-all), redeploy monolith, `caddy reload`. Prefer restoring the
Auth unit instead.

## Pass criteria

| Check | Pass |
|---|---|
| Cutover smoke | Auth routes behave; 401/403 contract holds via `:8121` |
| Dual strip | Monolith does not mount full Auth router; me stays on `:8110` |
| Live unit | `deploy/switchboard-auth.service` present and enabled on the VM |
| Parity | `tests/test_arch_ms77_auth_cutover_parity.py` green in CI |
| Timing | Cutover + Auth-unit recover each under ~60s on the VM |

## Fail / No-Go signals

- Reload leaves Auth unreachable on `:8121` and edge `/api/auth*`
- 403 returned for missing session (contract break)
- Need to restart Caddy (not just reload) to recover
- Second process shares SQLite writers unsafely (see ops proof contention harness)
