# Tasks cut playbook (Phase 3 Path A — live Mode A cut)

**Status:** Path A live — ARCH-MS-92. Mode A Tasks traffic is on
`switchboard-tasks` (`:8122`) via Caddy; sibling BCs stay on the monolith.

| Field | Value |
|---|---|
| Board tasks | ARCH-MS-90 · ARCH-MS-91 · ARCH-MS-92 |
| Verdict | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) (`verdict=go`, G6) |
| Edge | `deploy/Caddyfile` — sibling carve → `:8110`; `/api/tasks*` + claim TXP → `:8122` |
| Unit | `deploy/switchboard-tasks.service` |
| Dual strip | `PM_TASKS_HTTP_PRIMARY=service` on `deploy/projectplanner.service` |
| Rollback | [`tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md) |
| Charter | [ADR-0012](../decisions/0012-phase3-tasks-process-strangler.md) |

## Cutover sequence (ops)

1. Deploy code; `systemctl enable --now switchboard-tasks`.
2. `curl -sS http://127.0.0.1:8122/health` → `status=ok`.
3. `caddy validate --config /etc/caddy/Caddyfile` (or site Caddyfile path) + reload.
4. Smoke Mode A list/create/claim; confirm dispatch/chat/review still on `:8110`.
5. Confirm monolith has `PM_TASKS_HTTP_PRIMARY=service` (sibling-only mount).

## What stays on the monolith

- `/api/tasks/*/dispatch*`, `/api/tasks/*/chat*`, `/api/tasks/*/review_*`
- All other `/txp/v1/*` except claim_next|claim_task|complete_claim|abandon_claim|revoke_claim
- MCP (`:8111`)
