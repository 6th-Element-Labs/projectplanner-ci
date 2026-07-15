# Auth Caddy cutover + rollback drill (ARCH-MS-84 artifacts → ARCH-MS-76 live)

**Status:** Live cut is **ARCH-MS-76** — `deploy/Caddyfile` routes `/api/auth*` →
`switchboard-auth` on `127.0.0.1:8121`. Monolith (`:8110`) still mounts the Auth
router for rollback (green façade). Independence gate:
[`AUTH-INDEPENDENCE-GATE.md`](../AUTH-INDEPENDENCE-GATE.md).

Reference fragment (commented):
[`deploy/skeleton/Caddyfile.auth-fragment.example`](../../deploy/skeleton/Caddyfile.auth-fragment.example).

## Goal

Prove that routing `/api/auth*` to the Auth uvicorn can be enabled and fully
reverted without leaving a half-cut network façade.

## Preconditions

1. Independence gate G1–G6 Go (ARCH-MS-75 recorded operator Go).
2. `switchboard-auth.service` installed and healthy on `127.0.0.1:8121`.
3. Prefer `bash deploy/redeploy.sh` (starts Auth, proves health, then reloads Caddy).

## Cutover (production / Plan VM)

1. `sudo systemctl enable --now switchboard-auth`
2. `curl -sS http://127.0.0.1:8121/health` → `{"status":"ok",...}`
3. Confirm `deploy/Caddyfile` contains `handle /api/auth*` → `127.0.0.1:8121`
   **above** the catch-all `:8110` handle.
4. `caddy validate --adapter caddyfile --config deploy/Caddyfile`
5. `sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`
   (or run `bash deploy/redeploy.sh`).
6. Smoke through the public edge (`https://plan.taikunai.com`):
   - `GET /api/auth/session` (unauthenticated → **401**; never 403)
   - `POST /api/auth/login` with bad password → **401**
   - Register / login / logout happy path
7. Record wall-clock from reload to first green smoke (target: < 60s).

See also the checklist in [`deploy/PROVISION.md`](../../deploy/PROVISION.md).

## Rollback

1. Remove the Auth `handle /api/auth*` block(s) from `/etc/caddy/Caddyfile`
   (or restore the pre-cut file from git / backup). Traffic returns to monolith `:8110`.
2. `sudo systemctl reload caddy` (prefer reload over restart).
3. Re-run the same smoke suite against the monolith path.
4. Stop the Auth unit only after smoke is green on `:8110`:
   `sudo systemctl stop switchboard-auth` (optional; leave running for re-cut soak).
5. Record rollback wall-clock (target: < 60s).

## Pass criteria

| Check | Pass |
|---|---|
| Cutover smoke | Auth routes behave; 401/403 contract holds via `:8121` |
| Rollback smoke | Monolith Auth restored; no residual `/api/auth*` → second port |
| Live unit | `deploy/switchboard-auth.service` present and enabled on the VM |
| Timing | Cutover + rollback each under ~60s on the VM |

## Fail / No-Go signals

- Reload leaves Auth unreachable on both ports
- 403 returned for missing session (contract break)
- Need to restart Caddy (not just reload) to recover
- Second process shares SQLite writers unsafely (see ops proof contention harness)
