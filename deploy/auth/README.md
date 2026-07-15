# Auth process-cut service (ARCH-MS-75 → ARCH-MS-77)

Standalone Auth uvicorn on **`127.0.0.1:8121`**.

**ARCH-MS-76 (live):** production Caddy routes `/api/auth*` → `:8121` via
`deploy/Caddyfile`. The systemd unit is `deploy/switchboard-auth.service`.

**ARCH-MS-77:** production dual-strip — `projectplanner.service` sets
`PM_AUTH_HTTP_PRIMARY=service` so the monolith does not mount Auth HTTP
(shared package + `/api/auth/me` only). Caddy carves `handle /api/auth/me*` → `:8110`.
Hermetic TestClient suites leave the env unset and mount the same shared router
in-process.

## Package

```
src/switchboard/services/auth/
  app.py           # FastAPI factory (`create_app`)
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
| `../Caddyfile` | Live `/api/auth/me*` → `:8110`, `/api/auth*` → `:8121` |
| `../../docs/runbooks/auth-caddy-cutover-rollback.md` | Cutover + recovery drill |
| `../../tests/test_arch_ms77_auth_cutover_parity.py` | Hermetic parity + dual-strip ratchet |

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

## Recovery

Auth on `:8121` is the Auth HTTP source of truth after ARCH-MS-77. Prefer
restarting `switchboard-auth` / fixing Caddy over expecting monolith `:8110` to
serve login/session. See the runbook for emergency remount.

## Independence gate

Operator Go (G6) recorded for ARCH-MS-75; live edge cutover is ARCH-MS-76;
parity + dual-strip is ARCH-MS-77.
See [`docs/AUTH-INDEPENDENCE-GATE.md`](../../docs/AUTH-INDEPENDENCE-GATE.md).
