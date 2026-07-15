# Tasks process cut (ARCH-MS-90…92) — Path B waive superseded

**Decision:** Path B No-Go waive for *live* cut is **superseded**.

| Field | Value |
|---|---|
| Operator G6 / reopen | **ARCH-MS-94** — formal Path A reopen (`G6_operator_go=true`) |
| Live cut execution | **ARCH-MS-92** / PR #525 (Caddy + dual-strip) |
| Live cut closeout | **ARCH-MS-95** — [`tasks_live_cut_close.md`](tasks_live_cut_close.md) |
| Prior waive | Phase 3 Path B exit (ARCH-MS-93 provisional) |
| Current truth | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) (`verdict=go`, `process_cut_authorized=true`) |
| Live cut | [`tasks_cut_playbook.md`](tasks_cut_playbook.md) |
| Rollback | [`../runbooks/tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md) |

Historical Path B rationale remains in [`tasks_nogo_rationale.md`](tasks_nogo_rationale.md)
for audit; do not treat it as the active exit path.
