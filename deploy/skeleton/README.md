# Service-cut skeleton (ARCH-MS-73)

Reusable FastAPI unit + deploy templates for a **future** process cut (Auth/Tasks).
Nothing here is live: the monolith remains the only process behind production Caddy.

## Package

```
src/switchboard/services/_skeleton/
  app.py           # FastAPI factory + module-level `app`
  health.py        # GET /health → {"status":"ok","service":...}
  contracts/       # Pydantic + OpenAPI package boundary
  routers/example.py
```

Importable as `switchboard.services._skeleton`. Not mounted from `app_impl.py`.

## Local run

From the repo root (with the project venv active and `src/` on `PYTHONPATH` via the
usual Switchboard path bootstrap):

```bash
# default binds 127.0.0.1:8120 — does not collide with :8110 / :8111
python -m switchboard.services._skeleton

# or
uvicorn switchboard.services._skeleton.app:app --host 127.0.0.1 --port 8120
```

Optional env:

| Variable | Default | Purpose |
|---|---|---|
| `SWITCHBOARD_SKELETON_SERVICE_NAME` | `switchboard-skeleton` | Value of `/health` → `service` |
| `SWITCHBOARD_SKELETON_HOST` | `127.0.0.1` | Bind address |
| `SWITCHBOARD_SKELETON_PORT` | `8120` | Bind port |

Smoke check:

```bash
curl -sS http://127.0.0.1:8120/health
# {"status":"ok","service":"switchboard-skeleton"}
```

## Deploy templates (dormant)

| File | Role |
|---|---|
| `switchboard-skeleton.service.example` | systemd unit — localhost uvicorn on `:8120` |
| `Caddyfile.fragment.example` | commented `reverse_proxy` snippet for `/skeleton*` |

**Do not** enable the unit or paste the Caddy fragment into the live
`deploy/Caddyfile` until a cutover task flips traffic. Enabling either without a
cutover plan would convert an in-process seam into network coupling prematurely
(Phase 2 charter / ADR-0009 Decision 4).

## Cutover checklist

Use this when cloning the skeleton into a real service (e.g. Auth):

1. Copy `src/switchboard/services/_skeleton/` → `src/switchboard/services/<name>/` and rename symbols.
2. Replace `routers/example.py` with real domain routes; keep contracts in the service package.
3. Prove independence gate (no hidden monolith imports / shared SQLite writers across the wire).
4. Install `*.service.example` as a real unit on an unused port; verify `/health` from the host.
5. Add a **commented** Caddy fragment first; enable only after soak on the new unit.
6. Keep the monolith path available until the cut is proven; never cut live traffic in the same PR as the skeleton land.

## CI

`tests/test_arch_ms73_service_skeleton.py` imports the package, hits `/health` via
`TestClient`, and asserts the skeleton is **not** referenced from live
`app_impl` / production `Caddyfile` / enabled systemd unit names.
