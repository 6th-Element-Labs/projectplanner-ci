# Auth process-cut service (ARCH-MS-75)

Side-by-side Auth uvicorn on **`127.0.0.1:8121`**. Production Caddy still serves
`/api/auth*` from the monolith (`:8110`) until **ARCH-MS-76**.

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
uvicorn switchboard.services.auth.app:app --host 127.0.0.1 --port 8121
```

Smoke:

```bash
curl -sS http://127.0.0.1:8121/health
# {"status":"ok","service":"switchboard-auth"}

# register / login / session against :8121 while monolith still serves :8110
```

## Deploy

| File | Role |
|---|---|
| `switchboard-auth.service.example` | systemd unit template (dormant until soak) |
| `../skeleton/Caddyfile.auth-fragment.example` | commented reverse_proxy for ARCH-MS-76 |

**Do not** paste the Auth Caddy fragment into live `deploy/Caddyfile` in this task.
**Do not** remove Auth from `app_impl` — green façade / side-by-side is required.

## Independence gate

Operator Go (G6) recorded when ARCH-MS-75 was started. See
[`docs/AUTH-INDEPENDENCE-GATE.md`](../../docs/AUTH-INDEPENDENCE-GATE.md).
