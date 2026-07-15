# Tasks live cut — exit (ARCH-MS-100)

**Status:** Ready for deliverable closure verification.

Deliverable [`arch-ms-tasks-live-cut`](https://plan.taikunai.com/?project=switchboard&deliverable=arch-ms-tasks-live-cut)
end state: Tasks is a **live** process behind Caddy on `:8122` (day-one Mode A surface).
Chart **Tasks** block is green for mission `arch-ms-segmentation`.

| Field | Value |
|---|---|
| Board task | **ARCH-MS-100** (acceptance / exit) |
| Mission | `arch-ms-segmentation` |
| Deliverable | `arch-ms-tasks-live-cut` |
| Milestone | `arch-ms-tasks-live-cut:finish-path-a` |
| Operator G6 | ARCH-MS-94 / PR #526 |
| Live cut | ARCH-MS-92 / PR #525 · closeout ARCH-MS-95 / PR #528 |
| Independence | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) (`verdict=go`) |
| Closeout | [`tasks_live_cut_close.md`](tasks_live_cut_close.md) |
| Phase 3 gate | `scripts/arch_ms_phase3_exit_gate.py` → Path A |
| Phase 2 Auth | `scripts/arch_ms_phase2_exit_gate.py` → Path A (still green) |
| Proof test | `tests/test_arch_ms100_tasks_live_cut_exit.py` |

## Exit checklist (closure readiness)

| Check | Evidence |
|---|---|
| Caddy live Tasks traffic | `deploy/Caddyfile` → `:8122` for `/api/tasks*` + claim-only TXP |
| Dual-strip / day-one off monolith | `PM_TASKS_HTTP_PRIMARY=service`; `sibling_bc_only` mount |
| Independence Go + G6 | `tasks_independence_verdict.json` |
| No half-cut | phase3 exit `no_half_cut_network_facade` + `no_network_wrap_with_store_imports` |
| Auth still green | phase2 exit `path_a_auth_cut` |
| Sibling BC + MCP stay monolith | dispatch/chat/review_* carve; MCP `:8111` |

## Operator next step

Run **Verify & stamp closure** on deliverable `arch-ms-tasks-live-cut`
(`request_deliverable_closure_verification` / UI). Do **not** set deliverable Done from
this task — Done wait on closure grade + board provenance.
