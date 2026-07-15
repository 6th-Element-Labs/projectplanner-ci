# Tasks cut playbook pointer (Phase 3 Path B — no live cut)

**Status:** Path B exit — **no live cutover**. Full cutover/rollback steps remain in the
ARCH-MS-89 drill runbook for a future Go:

[`docs/runbooks/tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md)

Phase 3 closed via No-Go:

- [`tasks_independence_verdict.json`](tasks_independence_verdict.json)
- [`tasks_nogo_rationale.md`](tasks_nogo_rationale.md)
- [`tasks_cut_waived.md`](tasks_cut_waived.md)

Do not enable production `PM_TASKS_HTTP_PRIMARY=service`, live `switchboard-tasks`, or
Caddy `/api/tasks*` → `:8122` until a later mission records operator Go (G6) and completes
ARCH-MS-90…92 analogues.
