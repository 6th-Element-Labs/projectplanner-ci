# Tasks process cut — No-Go rationale (ARCH-MS-93 / Path B)

> **SUPERSEDED (2026-07-16):** ARCH-MS-94 recorded operator G6 and reopened Path A
> (`verdict=go`, `process_cut_authorized=true`). Live Mode A cut shipped via ARCH-MS-92
> (#525). Keep this file for audit only — see
> [`tasks_independence_verdict.json`](tasks_independence_verdict.json) and
> [`tasks_cut_waived.md`](tasks_cut_waived.md).

**Historical decision (ARCH-MS-93):** **No-Go** for a live Tasks uvicorn / Caddy cut
in Phase 3. Tasks remained **in-process** under the provisional Path B exit of
[ADR-0012](../decisions/0012-phase3-tasks-process-strangler.md) Decision 4–5.

| Field | Value |
|---|---|
| Board task | ARCH-MS-93 (exit) · independence update from ARCH-MS-89 |
| Verdict artifact | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) (`verdict=nogo`) |
| Waive of Go-only cut | [`tasks_cut_waived.md`](tasks_cut_waived.md) (ARCH-MS-90…92) |
| Independence gate | [`TASKS-INDEPENDENCE-GATE.md`](../TASKS-INDEPENDENCE-GATE.md) |
| Ops / drill evidence | ARCH-MS-89 · [`arch_ms89_tasks_ops_proof.py`](../../scripts/arch_ms89_tasks_ops_proof.py) · [`tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md) |
| Phase 2 Auth | Remains Path A green (`arch_ms_phase2_exit_gate.py`) |

## Measured evidence that informed No-Go

1. **ARCH-MS-89 Conditional Go** recorded hermetic G1–G5 green (ports, writers/binding,
   thin Mode A surface, ops proof) but **`G6_operator_go=false`** /
   `operator_g6_required=true`. The exit gate therefore must **not** treat Conditional
   Go as process-cut authorization.
2. **ARCH-MS-90…92** were never started — yellow-light HOLD until operator G6. No live
   `src/switchboard/services/tasks/` package, no production `switchboard-tasks` unit, no
   Caddy `/api/tasks*` edge cut.
3. Charter rule (ADR-0012 Decision 2 #5): never convert remaining in-process coupling into
   network coupling. Without operator G6 + cut playbook completion, Path A would be a
   half-cut.

## Why Path B (not force Path A)

- Prefer an honest modular monolith over a networked monolith without authority.
- Phase 3 deliverable end state explicitly allows Path B (Tasks stays in-process with
  documented No-Go; Auth Path A remains green; no half-cut).
- Re-opening a Tasks process cut later is allowed after a fresh Go (including G6) on a
  future mission — this No-Go is terminal for **Phase 3**, not forever.

## Proof checklist

- [x] Independence verdict `nogo` (this close)
- [x] Written rationale with 3B0 measured pointers (this file)
- [x] Tasks remains in-process (no live unit / Caddy Tasks route)
- [x] ARCH-MS-90…92 waived (see waive artifact + board comments)
- [x] Phase 2 exit still green
- [x] `scripts/arch_ms_phase3_exit_gate.py` → `passed=true` via Path B
