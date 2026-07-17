# Coord Caddy cutover and rollback (ARCH-MS-106)

## Live ownership

`switchboard-coord` on `127.0.0.1:8123` owns exactly `/api/board`,
`/api/signals`, `/ixp/v1/delta`, `/api/coordination`, and
`/api/coordinator_decisions`. All adjacent routes remain on the monolith.

## Deploy order

Use `bash deploy/redeploy.sh`. It installs and enables the least-privilege unit,
restarts all process cuts, requires `:8123/health` to pass, then validates and
installs Caddy. The authenticated runtime proof checks the canonical SHA, live
Caddy checksum, unit/listener/health, and all Auth, Tasks, and Coord owners.

Never install the Coord handles while `:8123` is dead. A failed preflight leaves
the prior live Caddyfile untouched.

## Automatic rollback transaction

Before mutation, redeploy snapshots the prior Caddyfile, monolith unit, Coord
unit, and Coord active/enabled state. Any failure through runtime proof restores
the prior monolith unit and waits for `:8110/health` before restoring the prior
edge. Only after that edge is live does it restore the former Coord lifecycle.
This ordering prevents both owners from being stripped at once.

## Manual recovery

1. Restore a monolith unit without `PM_COORD_HTTP_PRIMARY=service`; restart it
   and prove `http://127.0.0.1:8110/health`.
2. Point the five exact Caddy handles to `:8110` or remove them, validate, and
   reload Caddy.
3. Stop/disable `switchboard-coord` only after the prior edge is confirmed.
4. Re-run authenticated reads for all five paths and verify Auth/Tasks remain
   owned by `:8121`/`:8122`.

Never stop Coord first: with dual-strip active that creates a half-cut outage.
