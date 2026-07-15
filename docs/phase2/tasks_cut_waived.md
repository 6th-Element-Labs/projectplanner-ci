# Tasks process cut — waived (ARCH-MS-79)

**Decision:** **Waived** for Phase 2. Do **not** ship a live Tasks uvicorn / Caddy cut now.

**Exit path used:** readiness accepted (ARCH-MS-78).

| Field | Value |
|---|---|
| Board task | ARCH-MS-79 |
| Board AC option | cancelled/waived with readiness accepted as exit path |
| Readiness artifact | [`docs/phase2/tasks_readiness.md`](tasks_readiness.md) |
| Canonical readiness | [`docs/ARCH-MS-PHASE2-TASKS-READINESS.md`](../ARCH-MS-PHASE2-TASKS-READINESS.md) |
| Charter | [ADR-0011](../decisions/0011-phase2-process-strangler.md) Decision 1 track **2C** |

## Why waive

1. ARCH-MS-78 recorded **readiness-only** — Phase 2 exit does not require a second process cut.
2. ADR-0011: one BC per cut; Auth Go path just shipped; stacking Tasks would convert
   store/Auth coupling into network coupling without a Tasks independence gate
   (Auth ARCH-MS-82…84 analogue).
3. Phase 2 exit gate already scores `tasks_cut_or_readiness` green via the readiness file
   (`scripts/arch_ms_phase2_exit_gate.py`).

## What this is not

- Not a No-Go forever: a future mission may reopen a Tasks cut **after** ports,
  exclusive writers, and ops proof.
- Not a live `src/switchboard/services/tasks/` deployment.
- Not a change to Auth (`:8121`) or monolith task routers.

## Proof checklist

- [x] Gate readiness file present
- [x] Canonical readiness doc records readiness-only
- [x] This waive artifact present
- [x] Board comment on ARCH-MS-79 records the waive
