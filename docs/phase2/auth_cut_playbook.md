# Auth cut playbook (Phase 2 exit artifact)

**Canonical runbook:** [`docs/runbooks/auth-caddy-cutover-rollback.md`](../runbooks/auth-caddy-cutover-rollback.md)

This path exists so `scripts/arch_ms_phase2_exit_gate.py` (`AUTH_CUT_PLAYBOOK`) can
score Phase 2 Path A without duplicating the live cutover/rollback drill.

| Field | Value |
|---|---|
| Task | ARCH-MS-81 (Path A close) / ARCH-MS-76–77 (live cut) |
| Independence verdict | [`auth_independence_verdict.json`](auth_independence_verdict.json) → **go** |
| Auth process | `src/switchboard/services/auth/` · `deploy/switchboard-auth.service` · `:8121` |
| Edge | `deploy/Caddyfile` — `/api/auth/me*` → monolith `:8110`; `/api/auth*` → Auth `:8121` |
| Dual strip | Monolith `PM_AUTH_HTTP_PRIMARY=service` (no full Auth router mount) |
| Rollback | See canonical runbook — restore Auth unit / Caddy handles; emergency remount only as last resort |

## Quick cutover checklist

1. Independence G1–G6 Go (see `docs/AUTH-INDEPENDENCE-GATE.md`).
2. `systemctl enable --now switchboard-auth` · `curl -sS http://127.0.0.1:8121/health`.
3. Confirm Caddy Auth handles; `caddy validate` · reload (or `bash deploy/redeploy.sh`).
4. Edge smoke: session 401, bad login 401, register/login/logout.
5. Confirm monolith does not dual-mount Auth HTTP.

## Pass criteria (Path A)

- Exit gate Path A checks green (`independence_verdict_go`, service unit, Caddy route, this playbook).
- No dual-auth markers; Phase 1 exit still green; Tasks readiness present.
