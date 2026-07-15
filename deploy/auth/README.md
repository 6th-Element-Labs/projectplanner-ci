# Auth process-cut service (ARCH-MS-75 → ARCH-MS-76)

Standalone Auth uvicorn on **`127.0.0.1:8121`**.

**ARCH-MS-76 (live):** production Caddy routes `/api/auth*` → `:8121` via
`deploy/Caddyfile`. The systemd unit is `deploy/switchboard-auth.service`.
Monolith (`:8110`) still mounts the Auth router for **rollback**.

## Package

```
src/switchboard/services/auth/
  app.py           # FastAPI factory + module-level `app`
  health.py        # GET /health
  settings.py      # SWITCHBOARD_AUTH_* env
  __main__.py      # python -m switchboard.services.auth
```

Reuses `switchboard.api.routers.auth` (routes + store + session) and binds ports
via `configure_auth_ports()` — no root `store` / `auth` / `notify` imports inside
the Auth BC package.

## Local run

```bash
# default binds 127.0.0.1:8121
python -m switchboard.services.auth

# or
uvicorn --factory switchboard.services.auth.app:create_app --host 127.0.0.1 --port 8121
```

Smoke:

```bash
curl -sS http://127.0.0.1:8121/health
# {"status":"ok","service":"switchboard-auth"}
```

## Deploy

| File | Role |
|---|---|
| `../switchboard-auth.service` | **Live** systemd unit (ARCH-MS-76) |
| `switchboard-auth.service.example` | Copy under `deploy/auth/` for soak/docs parity |
| `../skeleton/Caddyfile.auth-fragment.example` | Historical drill snippet (now applied in live Caddyfile) |
| `../Caddyfile` | Live `/api/auth*` → `:8121` handles |
| `../../docs/runbooks/auth-caddy-cutover-rollback.md` | Cutover + rollback drill |

Enable on the Plan VM (Auth **before** Caddy reload):

```bash
sudo cp deploy/switchboard-auth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switchboard-auth
curl -sS http://127.0.0.1:8121/health
# then sync Caddy via redeploy.sh or the PROVISION.md checklist
```

Prefer `bash deploy/redeploy.sh` — it starts Auth, proves `:8110` + `:8121` health, then
reloads Caddy.

## Rollback

Remove the `/api/auth*` handle blocks from Caddy (traffic returns to monolith `:8110`),
`caddy reload`, re-smoke, then optionally stop `switchboard-auth`. See the runbook.

## Independence gate

Operator Go (G6) recorded for ARCH-MS-75; live edge cutover is ARCH-MS-76.
See [`docs/AUTH-INDEPENDENCE-GATE.md`](../../docs/AUTH-INDEPENDENCE-GATE.md).
