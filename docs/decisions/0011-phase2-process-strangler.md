# ADR-0011 — Phase 2 process strangler charter (auth-first, cut conditional)

- **Status:** Accepted — ARCH-MS-72 (plan-of-record for deliverable `arch-ms-phase-2`).
- **Date:** 2026-07-15
- **Author:** Platform modernization lane (ARCH-MS) — Phase 2 charter session
- **Relates to:** [ADR-0009](0009-microservices-modernization.md) (Phase 0 charter; Decision 4
  strangler trajectory) · [ADR-0007](0007-application-shell-cleanup.md) (Caddy edge; no nginx) ·
  [`AUTH-MICROSERVICE-DESIGN.md`](../AUTH-MICROSERVICE-DESIGN.md) (Auth as service #1; strangler
  pattern) · board deliverable **`arch-ms-phase-2`** · execution tracker
  [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) · workstream **`ARCH-MS`** (ARCH-MS-72+).

> **Numbering note.** Board task ARCH-MS-72 asked for “ADR-0010 (or ADR-0009 Phase 2 addendum).”
> ADR-0010 is already the CI-concurrency charter. This document is therefore **ADR-0011**, a
> peer Phase 2 charter (not an addendum rewrite of ADR-0009). ADR-0009 remains the Phase 0
> foundation; this ADR owns process-cut rules that Phase 0 explicitly deferred.

---

## Context — why Phase 2, and why the cut is yellow

Phase 0 (ADR-0009) and Phase 1 (`arch-ms-phase-1`, closed by ARCH-MS-71) delivered a **clean
FastAPI modular monolith** behind Caddy: thin `store` / `app` / `mcp` façades, typed
`application/` commands and queries, and Auth already living under
`src/switchboard/api/routers/auth` with global login shipped (ACCESS-16).

Physical process split was **out of scope** for Phase 0/1. The operator mission for Phase 2 is:

> Safe service-cut playbook proven. Auth process cut only if independence gate passes;
> otherwise Auth stays in-process. Never convert in-process coupling into network coupling.
> Caddy+uvicorn; SQLite default.

That is a **conditional strangler**, not a mandated microservice migration. Extracting Auth
into a second uvicorn before ports, ownership, and ops proof land would turn today's cheap
in-process calls into brittle network coupling — the failure mode this charter forbids.

**This ADR is the plan-of-record for deliverable `arch-ms-phase-2`.** It does not reopen
ADR-0006 control-plane freeze, ADR-0007's Caddy decision, or ARCH-19's Postgres SLO gate.

---

## Decision 1 — Phase 2 scope (in / out)

**In scope:**

| Track | Intent |
|---|---|
| **2A Charter + rails** | This ADR; reusable FastAPI service skeleton + health + systemd/Caddy pattern (ARCH-MS-73); playbook docs. |
| **2B0 Auth independence gate** | Ports (ARCH-MS-82); ownership / outage / secrets (ARCH-MS-83); architecture ratchets + ops proof harness (ARCH-MS-84). Must complete **before** any Auth process cut. |
| **2B Auth process cut (conditional)** | Lift Auth into a standalone uvicorn **only if** Go/No-Go = Go (ARCH-MS-75+). Side-by-side before Caddy path cutover. |
| **2C Tasks readiness** | Tasks remains candidate service #2; Phase 2 proves the cut playbook and leaves Tasks ready for a later conditional cut — not a second simultaneous process split. |

**Out of scope for Phase 2:**

- Big-bang rewrite of the monolith into many services.
- Frontend framework swap (static operator UI stays).
- Nginx (or any edge other than Caddy) — deploy remains **FastAPI + uvicorn + Caddy**.
- Postgres migration — SQLite remains default; Postgres only via the existing **ARCH-19 SLO** trigger.
- Mandating Auth leave the process if independence fails (see Decision 4 No-Go exit).

---

## Decision 2 — Process strangler rules (MUST)

1. **One bounded context per cut.** Each process extraction is a single vertical slice with
   contracts, ports, and a reversible Caddy cutover. No multi-service “day one.”
2. **Green façade always.** Callers keep working through stable REST/MCP/application seams while
   a slice moves. Old in-process path stays until the new path is proven; then delete.
3. **Auth is candidate service #1; Tasks is #2.** Order matches ADR-0009 Decision 4 and
   `AUTH-MICROSERVICE-DESIGN.md`. Do not cut Tasks before Auth independence is resolved
   (Go *or* documented No-Go).
4. **Auth process cut is CONDITIONAL (yellow light).** ARCH-MS-75 and downstream cut tasks
   **HOLD** until the independence gate (ARCH-MS-82 … ARCH-MS-84) records **Go**. Yellow means
   “prove independence first”; it is not a soft promise that the cut will happen.
5. **Never convert in-process coupling into network coupling.** If Auth still imports root
   `store` / `auth` / `notify` / shell modules, or two writers share one SQLite without an
   exclusive owner, the cut is forbidden — keep the package boundary and stay in-process.
6. **Deploy = FastAPI + uvicorn + Caddy.** No nginx. Second processes (when Go) are additional
   uvicorn units reverse-proxied by the same Caddy edge.
7. **SQLite default.** Postgres only when ARCH-19's SLO trigger fires; Phase 2 does not invent
   a new database program.

---

## Decision 3 — Service order and independence gate

| Slice | Bounded context | Process cut? | Gate |
|---|---|---|---|
| #1 | Auth / access | **Conditional** | ARCH-MS-82 (ports) → ARCH-MS-83 (ownership, outage, secrets) → ARCH-MS-84 (ratchets + ops proof) → Go/No-Go → ARCH-MS-75 only on Go |
| #2 | Tasks | Later / after Phase 2 readiness | Same strangler rules; not a Phase 2 mandatory second cut |

Independence gate intent (detail lives on the board tasks +
[`AUTH-INDEPENDENCE-GATE.md`](../AUTH-INDEPENDENCE-GATE.md)):

- **Ports:** Auth package must not import root monolith modules; repository + notification ports
  with injected adapters.
- **Ownership / outage / secrets (ARCH-MS-83):** Exclusive writers per table (Auth owns
  users/sessions/resets; Access owns grants); Auth-down fail-closed (no offline JWT trust);
  production `PM_JWT_SECRET` fail-fast. Go/No-Go checklist documented for ARCH-MS-75.
- **Ratchets + ops proof:** Import-direction CI, contention/memory/Caddy rollback drills, 401/403
  parity — measured inputs for the Go/No-Go checklist on ARCH-MS-75.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after ARCH-MS-82…84:

1. **Do not start** ARCH-MS-75 (Auth standalone uvicorn) or Caddy Auth path cutover tasks.
2. **Keep Auth in-process** behind the modular package boundary (green façade retained).
3. **Phase 2 still exits successfully** when the No-Go path criteria in Decision 5 hold:
   documented No-Go with measured evidence, architecture ratchets landed, and Tasks readiness
   for a future cut.
4. No-Go is a **valid terminal outcome**, not a failed mission. Prefer an honest modular
   monolith over a networked monolith.

---

## Decision 5 — Phase 2 exit criteria (`arch_ms_phase2_exit_gate`)

Phase 2 closes when **either** of the following paths is true (machine-checkable gate to be
implemented as `scripts/arch_ms_phase2_exit_gate.py` / board acceptance — pointer only in this
ADR):

**Path A — Auth cut (Go):**

- Independence gate recorded Go.
- Auth runs as a standalone uvicorn with behavior parity (register / login / session / logout /
  grants).
- Caddy cutover + rollback proven; playbook complete.
- Ratchets green; Tasks readiness documented for service #2.

**Path B — Documented No-Go:**

- Independence gate (or operator decision) records No-Go with written rationale + measured
  evidence from ARCH-MS-84.
- Auth remains in-process; no half-cut network façade.
- Architecture ratchets landed and enforced.
- Tasks readiness for a later conditional cut is documented.

Deliverable `arch-ms-phase-2` moves to **done** only with board-recorded merge provenance on the
canonical repo (Switchboard working agreement). Agents use `complete_claim`; they do not mark
Done.

---

## Execution — board and tracker

All live task status lives on `project=switchboard`, deliverable **`arch-ms-phase-2`**,
workstream **ARCH-MS**. Human-readable tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Phase 2 section).

**View on board:**
`?project=switchboard&deliverable=arch-ms-phase-2#tab-mission`

---

## Consequences

- Agents must treat Auth process extraction as **blocked** until independence Go is recorded.
- Skeleton and playbook work (ARCH-MS-73+) can proceed without cutting live traffic.
- Claiming “microservices done” because a second process exists without ports/ownership/ops
  proof is a charter violation.
- Naming collision: future board text should say **ADR-0011**, not ADR-0010, for this charter.

## Alternatives rejected

- **Mandatory Auth process cut.** Rejected — yellow light; No-Go is a first-class exit.
- **Big-bang multi-service split.** Rejected in ADR-0009 and `AUTH-MICROSERVICE-DESIGN.md`.
- **Network-wrap the monolith without independence.** Converts in-process coupling into network
  coupling; forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007; Phase 2 keeps Caddy.
- **Postgres as a Phase 2 prerequisite.** Rejected; ARCH-19 remains the SLO gate.
- **ADR-0009 Phase 2 addendum only.** Phase 2 process-cut rules need a stable numbered charter
  distinct from Phase 0 exit criteria; ADR-0010 was already taken.
