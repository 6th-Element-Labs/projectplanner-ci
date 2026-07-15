# ADR-0016 — Ingest / inbox process strangler charter (Mode A)

- **Status:** Accepted — ARCH-MS-99 (plan-of-record for deliverable `arch-ms-ingest-service`).
- **Date:** 2026-07-16
- **Author:** Platform modernization lane (ARCH-MS) — Ingest charter session
- **Relates to:** [ADR-0015](0015-tally-economics-process-strangler.md) (Tally Mode A) ·
  [ADR-0014](0014-deliverables-mission-process-strangler.md) ·
  [ADR-0013](0013-coord-board-process-strangler.md) · [ADR-0012](0012-phase3-tasks-process-strangler.md) ·
  [ADR-0011](0011-phase2-process-strangler.md) (Auth playbook) ·
  [ADR-0009](0009-microservices-modernization.md) · [ADR-0007](0007-application-shell-cleanup.md)
  (Caddy edge; no nginx) · board deliverable **`arch-ms-ingest-service`** · mission
  **`arch-ms-segmentation`** · execution tracker
  [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) · thin surface
  [`thin_day_one_surface.md`](../ingest/thin_day_one_surface.md) · workstream **`ARCH-MS`**
  (ARCH-MS-99+).

> **Mode A** means: one BC (Ingest / inbox), yellow-light process cut, and a **thin day-one
> intake surface** (`:8126`; inbox **read** + text `/api/intake`). It does **not** mean “cut
> upload/transcribe,” “cut confirm/dismiss board apply,” or “cut mailbox poll” in the same
> phase — those stay on the monolith until a later Mode B (if ever Go-authorized).

---

## Context — why Ingest next, and why the cut stays yellow

Segmentation mission `arch-ms-segmentation` sequences remaining peels:

> Tasks live cut → Coord / board → Deliverables / mission → Tally / economics → **Ingest**.

Deliverable `arch-ms-ingest-service` end state:

> Ingest / inbox is a live process behind Caddy (or documented No-Go). Chart Ingest block green.

The intake/inbox router still imports root `store` / `inbox` / `intake` / `attachments` /
`transcribe` directly (`src/switchboard/api/routers/intake_inbox.py`). Cutting without ports
would turn that coupling into a networked half-cut — forbidden by the Auth playbook.

**This ADR is the plan-of-record for deliverable `arch-ms-ingest-service`.** It does not reopen
prior BC charters; Auth/Tasks live cuts (and Coord/Deliverables/Tally when live) must stay green.
This charter completes the master-plan BC peel set for `arch-ms-segmentation`.

---

## Decision 1 — Ingest scope (in / out) — Mode A

**In scope:**

| Track | Intent |
|---|---|
| **Charter + thin surface** | This ADR; locked day-one intake list + `:8126` (ARCH-MS-99). |
| **Independence gate** | Ports (no root `store` / `auth` imports); exclusive writers / shared-SQLite policy; prior BCs not regressed; ops proof; recorded Go/No-Go **before** any process cut. |
| **Process cut (conditional)** | Standalone Ingest uvicorn on **`:8126`** + side-by-side parity + Caddy cutover + dual-strip **only if** Go. |
| **Exit** | Live cut **or** documented No-Go; prior BC cuts remain green. |

**Out of scope for Mode A:**

- Other BCs (Messaging, Runner, …) as process cuts.
- Day-one **heavy / mutate-board** routes: `/api/intake/upload`, inbox confirm/dismiss/confirm_all,
  simulate, mailbox poll.
- MCP process cut — MCP stays on `:8111`.
- Nginx; Postgres migration (ARCH-19); mandatory leave-process if independence fails.

---

## Decision 2 — Process strangler rules (MUST) — reuse Auth playbook

Ingest inherits ADR-0011…0015 Decision 2:

1. **One bounded context per cut.** This charter cuts **Ingest / inbox only**.
2. **Green façade always.** Stable `/api/intake` and `/api/inbox*` seams keep working while the slice moves.
3. **Do not regress Auth (`:8121`), Tasks (`:8122`), Coord (`:8123`), Deliverables (`:8124`), Tally (`:8125`) when live.**
4. **Ingest process cut is CONDITIONAL (yellow light).** HOLD until independence records **Go**.
5. **Never convert in-process coupling into network coupling.**
6. **Deploy = FastAPI + uvicorn + Caddy.** Ingest (when Go) is another localhost uvicorn behind the same Caddy edge.
7. **SQLite default.** Fail closed to in-process if two-process writers are unsafe.
8. **Reuse the Auth/Tasks cut playbook.** Side-by-side → Caddy fragment → live cut → dual-strip → documented rollback.

---

## Decision 3 — Thin day-one cut surface (Mode A lock)

Canonical table: [`docs/ingest/thin_day_one_surface.md`](../ingest/thin_day_one_surface.md).

| Surface | Day-one Ingest process (`:8126`) | Stays on monolith |
|---|---|---|
| Inbox read | `GET /api/inbox` | — |
| Text intake | `POST /api/intake` | `POST /api/intake/upload` (transcribe/extract) |
| Confirm / dismiss | — | `POST /api/inbox/{id}/confirm`, `…/dismiss`, `POST /api/inbox/confirm_all` |
| Mailbox / drill | — | `POST /api/inbox/simulate`, `POST /api/inbox/poll` |
| Process | `src/switchboard/services/ingest/` (future) · **`:8126`** | Web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`, Coord `:8123`, Deliverables `:8124`, Tally `:8125` |
| Dual-strip | `PM_INGEST_HTTP_PRIMARY=service` after parity | Hermetic TestClient may leave unset |
| MCP | Out of Mode A cut | `:8111` |

Expanding mid-mission (file upload on the cut, confirm/apply on the cut, mailbox poll relocation)
requires a **new ADR or explicit Mode B charter**.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after independence:

1. **Do not** ship Ingest uvicorn / Caddy / dual-strip.
2. **Keep Ingest/inbox in-process** behind modular routers.
3. Deliverable may still exit on Path B with written rationale + measured evidence.
4. No-Go is a **valid terminal outcome**.
5. Prior BC Path A/Path B truth must remain green.

---

## Decision 5 — Exit criteria (`arch-ms-ingest-service`)

**Path A — Ingest cut (Go):**

- Independence gate recorded Go (including operator G6 when required).
- Ingest runs as standalone uvicorn on `:8126` with thin-surface parity.
- Caddy routes the Decision 3 surface; dual-strip proven; rollback documented.
- Prior BC cuts still green; no half-cut façade; CI green.

**Path B — Documented No-Go:**

- Independence (or operator decision) records No-Go with written rationale.
- Ingest remains in-process; no half-cut network façade.
- Charter + independence evidence landed; prior BCs still green; CI green.

Deliverable `arch-ms-ingest-service` moves to **done** only with board-recorded merge provenance
on the canonical repo (plus closure verification). Agents use `complete_claim`; they do not mark Done.

---

## Execution — board and tracker

Live status: `project=switchboard`, deliverable **`arch-ms-ingest-service`**, mission
**`arch-ms-segmentation`**, workstream **ARCH-MS**. Tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Ingest section).

| Milestone | Board tasks (indicative) |
|---|---|
| `independence` | ARCH-MS-99 (this ADR + thin surface), successor independence/Go-No-Go tasks |
| `process-cut` | Go-only uvicorn + Caddy + dual-strip |
| Exit | Path A or Path B evidence |

**View on board:**
`?project=switchboard&deliverable=arch-ms-ingest-service#tab-mission`

---

## Consequences

- Agents must treat Ingest process extraction as **blocked** until independence Go is recorded.
- Charter + thin-surface work (ARCH-MS-99) can proceed without cutting live traffic.
- Claiming “Ingest microservice done” without ports / writers / ops proof is a charter violation.
- Mode A thin surface is a hard lock; widen only with a new charter decision.
- Prior BC cuts are **dependencies of truth**.
- Completing this charter closes the planned BC peel set for `arch-ms-segmentation` (further peels need a new mission).

## Alternatives rejected

- **Mandatory Ingest process cut.** Rejected — yellow light; No-Go is first-class.
- **Cut Ingest before Tally charter.** Rejected by mission sequence + ARCH-MS-98 dependency.
- **Wide day-one surface (upload + confirm/apply + mailbox poll).** Rejected for Mode A.
- **Network-wrap without independence.** Forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007.
- **Postgres as an Ingest prerequisite.** Rejected; ARCH-19 remains the SLO gate.
