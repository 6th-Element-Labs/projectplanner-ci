# Auth Caddy cutover + rollback drill (ARCH-MS-84)

**Status:** Drill artifacts only — do **not** cut production until ARCH-MS-75 records
operator Go. Relates to [`AUTH-INDEPENDENCE-GATE.md`](../AUTH-INDEPENDENCE-GATE.md)
and [`deploy/skeleton/Caddyfile.auth-fragment.example`](../../deploy/skeleton/Caddyfile.auth-fragment.example).

## Goal

Prove that routing `/api/auth*` to a second localhost uvicorn can be enabled and
fully reverted without leaving a half-cut network façade.

## Preconditions

1. Independence gate G1–G5 measured (this task fills G2/G5).
2. Auth (or skeleton) unit running on an unused `127.0.0.1` port (e.g. `:8121`).
3. Staging / non-production Caddy first; production only after ARCH-MS-75 Go.

## Cutover (staging)

1. Confirm monolith serves Auth today: `curl -sS https://<host>/api/auth/session`.
2. Start the second process; confirm `curl -sS http://127.0.0.1:8121/health`.
3. Paste the Auth handle blocks from `Caddyfile.auth-fragment.example` **above** the
   catch-all `:8110` handle in a staging Caddyfile; set the port correctly.
4. `caddy validate` then `caddy reload`.
5. Smoke:
   - `GET /api/auth/session` (unauthenticated → 401 or null user; never 403)
   - `POST /api/auth/login` with bad password → **401**
   - Register / login / logout happy path
6. Record wall-clock from reload to first green smoke (target: < 60s).

## Rollback

1. Remove the Auth `handle /api/auth*` blocks (traffic returns to monolith `:8110`).
2. `caddy reload`.
3. Re-run the same smoke suite against the monolith path.
4. Stop the second uvicorn only after smoke is green on `:8110`.
5. Record rollback wall-clock (target: < 60s).

## Pass criteria

| Check | Pass |
|---|---|
| Staging cutover smoke | Auth routes behave; 401/403 contract holds |
| Rollback smoke | Monolith Auth restored; no residual `/api/auth*` → second port |
| Live production Caddy | Still **without** Auth cut until ARCH-MS-75 |
| Timing | Cutover + rollback each under ~60s on the VM |

## Fail / No-Go signals

- Reload leaves Auth unreachable on both ports
- 403 returned for missing session (contract break)
- Need to restart Caddy (not just reload) to recover
- Second process shares SQLite writers unsafely (see ops proof contention harness)
