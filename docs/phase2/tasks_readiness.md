# Tasks service readiness (Phase 2 exit artifact)

**Canonical document:** [`docs/ARCH-MS-PHASE2-TASKS-READINESS.md`](../ARCH-MS-PHASE2-TASKS-READINESS.md)

This path exists so `scripts/arch_ms_phase2_exit_gate.py` (`TASKS_READINESS`) can
score Phase 2 exit without requiring a live Tasks process cut.

| Field | Value |
|---|---|
| Task | ARCH-MS-78 |
| Decision | **readiness-only** (do not ship Tasks uvicorn / Caddy cut in Phase 2) |
| Follow-on live cut | Deferred — ARCH-MS-79 **waived** (see [`tasks_cut_waived.md`](tasks_cut_waived.md)); reopen only after a Tasks independence gate |
| Milestone | `arch-ms-phase-2:2c-tasks-cut-or-readiness` |

See the canonical doc for boundary map, HTTP/MCP contracts, coupling ledger, and
extract/rollback plan.
