# Tasks process cut (ARCH-MS-90…92) — waived (Phase 3 Path B)

**Decision:** **Waived** for Phase 3. Do **not** ship a live Tasks uvicorn / Caddy /
dual-strip cut now. Exit via Path B No-Go (ARCH-MS-93).

| Field | Value |
|---|---|
| Board tasks | ARCH-MS-90 · ARCH-MS-91 · ARCH-MS-92 |
| Board AC option | waived — Path B No-Go accepted as Phase 3 exit |
| No-Go rationale | [`tasks_nogo_rationale.md`](tasks_nogo_rationale.md) |
| Verdict | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) (`verdict=nogo`) |
| Charter | [ADR-0012](../decisions/0012-phase3-tasks-process-strangler.md) Decision 4–5 |
| Drill only (no live cut) | [`tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md) |

## Why waive

1. ARCH-MS-89 recorded **Conditional Go** with `G6_operator_go=false` — ARCH-MS-90+ HOLD
   until operator G6; G6 was never recorded.
2. ADR-0012: Tasks process cut is yellow-light / conditional. No-Go keep-in-process is a
   first-class Phase 3 exit.
3. Shipping unit/Caddy without authorized Go would fail
   `arch_ms_phase3_exit_gate.py` (`half_cut` / network-wrap checks).

## What this is not

- Not a forever ban on Tasks-as-service — reopen after operator Go on a later mission.
- Not a live `src/switchboard/services/tasks/` deployment or production dual-strip.
- Not a regression of Auth Path A (`:8121`) or Phase 2 exit green.

## Proof checklist

- [x] Path B No-Go rationale present
- [x] Independence verdict `nogo`
- [x] This waive artifact present
- [x] Board comments on ARCH-MS-90…92 record the waive
- [x] Phase 3 exit gate Path B green
