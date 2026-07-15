# ADR-0012 — Phase 3 Tasks process strangler charter (Mode A)

- **Status:** Accepted — ARCH-MS-85 (plan-of-record for deliverable `arch-ms-phase-3`).
- **Date:** 2026-07-15
- **Author:** Platform modernization lane (ARCH-MS) — Phase 3 charter session
- **Relates to:** [ADR-0011](0011-phase2-process-strangler.md) (Phase 2 Auth strangler; playbook
  proven) · [ADR-0009](0009-microservices-modernization.md) (Tasks = service #2) ·
  [ADR-0007](0007-application-shell-cleanup.md) (Caddy edge; no nginx) ·
  [`ARCH-MS-PHASE2-TASKS-READINESS.md`](../ARCH-MS-PHASE2-TASKS-READINESS.md) (Mode A surface +
  coupling ledger) · [`TASKS-INDEPENDENCE-GATE.md`](../TASKS-INDEPENDENCE-GATE.md) (ownership /
  writers / Auth binding — ARCH-MS-88) · [`tasks_cut_waived.md`](../phase2/tasks_cut_waived.md)
  (Phase 2 deferred the live Tasks cut) · board deliverable **`arch-ms-phase-3`** · execution
  tracker [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) · workstream **`ARCH-MS`**
  (ARCH-MS-85+).

> **Mode A** means: one BC (Tasks), yellow-light process cut, and a **thin day-one cut surface**
> (`:8122`; `/api/tasks*` + claim-only TXP). It does **not** mean “cut MCP,” “cut every TXP
> sibling,” or “cut another BC in the same phase.”

---

## Context — why Phase 3, and why the cut stays yellow

Phase 2 (ADR-0011) closed on **Path A**: Auth runs as a standalone uvicorn behind Caddy after an
independence gate (ARCH-MS-82…84 → Go → ARCH-MS-75…77). The cut playbook is proven:
[`auth-caddy-cutover-rollback.md`](../runbooks/auth-caddy-cutover-rollback.md),
[`auth_cut_playbook.md`](../phase2/auth_cut_playbook.md).

Tasks was **explicitly not** cut in Phase 2. ARCH-MS-78 accepted **readiness-only**; ARCH-MS-79
was **waived**. The readiness package already names the day-one surface (port **`:8122`**,
`/api/tasks*`, claim-only `/txp/v1/claim_*`) and the coupling that still forbids a blind cut
(shared project SQLite, `_store_facade()` density, Auth/write-binding, fat review/dispatch/chat
attachments).

The operator mission for Phase 3 (`arch-ms-phase-3`) is:

> Tasks either runs as its own uvicorn behind Caddy (Path A) or stays in-process with documented
> No-Go (Path B). No half-cut. Phase 2 Auth cut remains green. Thin day-one cut surface only.
> Deploy stays FastAPI + uvicorn + Caddy; SQLite default.

That is the same **conditional strangler** pattern as Auth — applied to service #2 — not a
mandate to ship a Tasks microservice.

**This ADR is the plan-of-record for deliverable `arch-ms-phase-3`.** It does not reopen
ADR-0006, ADR-0007, ARCH-19, or the Auth Path A cut.

---

## Decision 1 — Phase 3 scope (in / out) — Mode A

**In scope:**

| Track | Intent |
|---|---|
| **3A Charter + rails** | This ADR; Phase 3 exit harness stub (`arch_ms_phase3_exit_gate.py`) that fails closed until Path A or Path B evidence exists (ARCH-MS-86). |
| **3B0 Tasks independence gate** | Ports (no root `store` / `auth` / `dispatch` imports); exclusive writers (or measured shared-SQLite policy); Auth/write-binding via ports; ops proof (contention / rollback / API parity); recorded Go/No-Go **before** any process cut (ARCH-MS-87…89). |
| **3B Tasks process cut (conditional)** | Standalone Tasks uvicorn on **`:8122`** + side-by-side parity + Caddy cutover + dual-strip **only if** Go (ARCH-MS-90…92). |
| **Exit** | `arch_ms_phase3_exit_gate` — Tasks cut **or** documented No-Go; Auth Phase 2 exit still green (ARCH-MS-93). |

**Out of scope for Phase 3 (Mode A):**

- Other bounded contexts (Access, Messaging, Narration, Deliverables, …) as process cuts.
- Full `/txp/v1/*` edge move — **claim-only** TXP paths; wakes and other TXP siblings stay on the
  monolith.
- MCP process cut / MCP proxy redesign — MCP stays on `:8111` unless a later mission redesigns it.
- Task subpaths that are other BCs day one: `…/dispatch`, `…/chat`, `…/review_*` stay on the
  monolith (same idea as Auth’s `/api/auth/me*` carve-out).
- Nginx (or any edge other than Caddy).
- Postgres migration — SQLite remains default; Postgres only via **ARCH-19 SLO**.
- Mandating Tasks leave the process if independence fails (see Decision 4 No-Go exit).
- Big-bang rewrite / frontend framework swap.

---

## Decision 2 — Process strangler rules (MUST) — reuse Auth playbook

Phase 3 **inherits** ADR-0011 Decision 2 and applies it to Tasks:

1. **One bounded context per cut.** Phase 3 cuts **Tasks only**.
2. **Green façade always.** Stable REST / application / (later) MCP seams keep working while the
   slice moves; delete the old path only after the new path is proven.
3. **Auth remains service #1; Tasks is #2.** Do not regress the Auth cut while cutting Tasks.
4. **Tasks process cut is CONDITIONAL (yellow light).** ARCH-MS-90+ **HOLD** until 3B0 records
   **Go**. Yellow means “prove independence first.”
5. **Never convert in-process coupling into network coupling.** If Tasks still reaches root
   `store` / `auth` / `dispatch` / shell modules without ports, or shared SQLite writers are
   unsafe under multi-process load, the cut is forbidden — stay in-process.
6. **Deploy = FastAPI + uvicorn + Caddy.** No nginx. Tasks (when Go) is another localhost uvicorn
   behind the same Caddy edge.
7. **SQLite default.** Shared project DB until contention is measured; fail closed to in-process
   if two-process writers are unsafe.
8. **Reuse the Auth cut playbook.** Side-by-side → Caddy fragment → live cut → dual-strip →
   documented rollback. Structure after
   [`auth-caddy-cutover-rollback.md`](../runbooks/auth-caddy-cutover-rollback.md) and the Phase 2
   Auth Path A evidence under `docs/phase2/`.

---

## Decision 3 — Thin day-one cut surface (Mode A lock)

| Surface | Day-one Tasks process | Stays on monolith |
|---|---|---|
| HTTP | `/api/tasks*` (CRUD + move/archive as decided in readiness) | `…/dispatch`, `…/chat`, `…/review_*` |
| TXP | **Claim-only:** `/txp/v1/claim_next`, `/txp/v1/claim_task`, `/txp/v1/complete_claim`, `/txp/v1/abandon_claim`, `/txp/v1/revoke_claim` | Other `/txp/v1/*` (wakes, etc.) |
| Process | `src/switchboard/services/tasks/` · systemd · **`:8122`** | Web `:8110`, MCP `:8111`, Auth `:8121` |
| Dual-strip | `PM_TASKS_HTTP_PRIMARY=service` (Auth analogue) after parity | Hermetic TestClient may leave unset |
| MCP | Out of Phase 3 Mode A cut | `:8111` |

Expanding the cut surface mid-phase (blanket TXP, MCP move, second BC) requires a **new ADR or
explicit Mode B charter** — not a silent widening of Mode A.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after ARCH-MS-87…89:

1. **Do not start** ARCH-MS-90…92 (Tasks uvicorn / Caddy / dual-strip).
2. **Keep Tasks in-process** behind the modular package / readiness boundary.
3. **Phase 3 still exits successfully** when Path B criteria in Decision 5 hold.
4. No-Go is a **valid terminal outcome**. Prefer an honest modular monolith over a networked
   monolith. Auth Path A must remain green either way.

---

## Decision 5 — Phase 3 exit criteria (`arch_ms_phase3_exit_gate`)

Phase 3 closes when **either** path is true (machine-checkable gate — ARCH-MS-86 lands the
harness; this ADR owns the criteria):

**Path A — Tasks cut (Go):**

- Tasks independence gate recorded Go.
- Tasks runs as standalone uvicorn on `:8122` with thin-surface parity.
- Caddy routes `/api/tasks*` + claim-only TXP; dual-strip proven; rollback documented.
- Phase 2 Auth exit still green; no dual-Tasks / half-cut façade; CI green.

**Path B — Documented No-Go:**

- Independence gate (or operator decision) records No-Go with written rationale + measured
  evidence from 3B0.
- Tasks remains in-process; no half-cut network façade.
- Charter + independence evidence landed; Auth Phase 2 exit still green; CI green.

Deliverable `arch-ms-phase-3` moves to **done** only with board-recorded merge provenance on the
canonical repo. Agents use `complete_claim`; they do not mark Done.

---

## Execution — board and tracker

All live task status lives on `project=switchboard`, deliverable **`arch-ms-phase-3`**,
workstream **ARCH-MS**. Human-readable tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Phase 3 section).

| Milestone | Board tasks (indicative) |
|---|---|
| `3a-charter-rails` | ARCH-MS-85 (this ADR), ARCH-MS-86 (exit harness) |
| `3b0-tasks-independence` | ARCH-MS-87…89 |
| `3b-tasks-process-cut` | ARCH-MS-90…92 (**Go only**) |
| `exit-gate` | ARCH-MS-93 |

**View on board:**
`?project=switchboard&deliverable=arch-ms-phase-3#tab-mission`

---

## Consequences

- Agents must treat Tasks process extraction as **blocked** until independence Go is recorded.
- Charter + exit-harness work (ARCH-MS-85/86) can proceed without cutting live traffic.
- Claiming “Tasks microservice done” without ports / writers / ops proof is a charter violation.
- Mode A thin surface is a hard lock for Phase 3; widen only with a new charter decision.
- Phase 2 Auth cut is a **dependency of truth**, not something Phase 3 may quietly undo.

## Alternatives rejected

- **Mandatory Tasks process cut.** Rejected — yellow light; No-Go is first-class (same as Auth).
- **Cut Tasks in Phase 2 alongside Auth.** Already rejected / waived (ARCH-MS-79); stacking BCs
  violates one-BC-per-cut.
- **Wide day-one surface (full TXP + MCP + review/dispatch/chat).** Rejected for Mode A — turns
  service #2 into a second monolith over the network.
- **Network-wrap Tasks without independence.** Forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007.
- **Postgres as a Phase 3 prerequisite.** Rejected; ARCH-19 remains the SLO gate.
- **Reopen Auth No-Go / undo Auth Path A.** Out of scope; Auth cut stays green.
