# ADR-0014 — Deliverables / mission process strangler charter (Mode A)

- **Status:** Accepted — ARCH-MS-97 (plan-of-record for deliverable `arch-ms-deliverables-service`).
- **Date:** 2026-07-16
- **Author:** Platform modernization lane (ARCH-MS) — Deliverables charter session
- **Relates to:** [ADR-0013](0013-coord-board-process-strangler.md) (Coord Mode A) ·
  [ADR-0012](0012-phase3-tasks-process-strangler.md) (Tasks Mode A live) ·
  [ADR-0011](0011-phase2-process-strangler.md) (Auth playbook) ·
  [ADR-0009](0009-microservices-modernization.md) · [ADR-0007](0007-application-shell-cleanup.md)
  (Caddy edge; no nginx) · board deliverable **`arch-ms-deliverables-service`** · mission
  **`arch-ms-segmentation`** · execution tracker [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) ·
  thin surface [`thin_day_one_surface.md`](../deliverables/thin_day_one_surface.md) · workstream
  **`ARCH-MS`** (ARCH-MS-97+).

> **Mode A** means: one BC (Deliverables / mission), yellow-light process cut, and a **thin
> day-one cut surface** (`:8124`; deliverable + mission **read** paths + closure **report**
> read). It does **not** mean “cut MCP,” “cut breakdown approve/write,” or “cut Coord/Tally in
> the same phase.”

---

## Context — why Deliverables next, and why the cut stays yellow

Segmentation mission `arch-ms-segmentation` sequences remaining peels:

> Tasks live cut → Coord / board → **Deliverables / mission** → Tally → Ingest.

Deliverable `arch-ms-deliverables-service` end state:

> Deliverables / mission bounded context is a live process behind Caddy (or documented No-Go).
> Chart Deliverables block green.

The deliverables router still imports root `store` / `auth` / `deliverable_closure` directly
(`src/switchboard/api/routers/deliverables.py`). Cutting without ports would turn that coupling
into a networked half-cut — forbidden by the Auth playbook.

**This ADR is the plan-of-record for deliverable `arch-ms-deliverables-service`.** It does not
reopen Auth, Tasks, or Coord charters; prior live cuts must stay green.

---

## Decision 1 — Deliverables scope (in / out) — Mode A

**In scope:**

| Track | Intent |
|---|---|
| **Charter + thin surface** | This ADR; locked day-one route list + `:8124` (ARCH-MS-97). |
| **Independence gate** | Ports (no root `store` / `auth` / shell imports); exclusive writers / shared-SQLite policy; Auth + Tasks + Coord (when live) not regressed; ops proof; recorded Go/No-Go **before** any process cut. |
| **Process cut (conditional)** | Standalone Deliverables uvicorn on **`:8124`** + side-by-side parity + Caddy cutover + dual-strip **only if** Go. |
| **Exit** | Live cut **or** documented No-Go; prior BC cuts remain green. |

**Out of scope for Mode A:**

- Other BCs (Tally, Ingest, Messaging, Runner, Coord cut execution if still charter-only, …).
- Full deliverables write surface day one — **reads** for status/mission/closure report only.
- Breakdown **mutate** (create/approve/reject/defer/patch proposals), archive, outcome write,
  coordinator tick, closure verify/request (write), narrative patch, mission brief generate.
- MCP process cut — MCP stays on `:8111`.
- Nginx; Postgres migration (ARCH-19); mandatory leave-process if independence fails.

---

## Decision 2 — Process strangler rules (MUST) — reuse Auth playbook

Deliverables inherits ADR-0011 / ADR-0012 / ADR-0013 Decision 2:

1. **One bounded context per cut.** This charter cuts **Deliverables / mission only**.
2. **Green façade always.** Stable REST seams keep working while the slice moves.
3. **Do not regress Auth (`:8121`), Tasks (`:8122`), or Coord (`:8123` when live).**
4. **Deliverables process cut is CONDITIONAL (yellow light).** HOLD until independence records **Go**.
5. **Never convert in-process coupling into network coupling.**
6. **Deploy = FastAPI + uvicorn + Caddy.** Deliverables (when Go) is another localhost uvicorn behind the same Caddy edge.
7. **SQLite default.** Fail closed to in-process if two-process writers are unsafe.
8. **Reuse the Auth/Tasks/Coord cut playbook.** Side-by-side → Caddy fragment → live cut → dual-strip → documented rollback.

---

## Decision 3 — Thin day-one cut surface (Mode A lock)

Canonical table: [`docs/deliverables/thin_day_one_surface.md`](../deliverables/thin_day_one_surface.md).

| Surface | Day-one Deliverables process (`:8124`) | Stays on monolith |
|---|---|---|
| List / get | `GET /api/deliverables`, `GET /api/deliverables/{id}` | `POST /api/deliverables`, archive, outcome |
| Mission status | `GET /api/mission_status`, `GET /api/deliverables/{id}/mission_status` | — |
| Closure read | `GET /api/deliverables/{id}/closure_report` | `POST …/closure_verify`, `POST …/closure_request` |
| Graph read | `GET /api/deliverables/{id}/dependency_graph` | — |
| Breakdown read | `GET /api/deliverables/breakdown_proposals*` | approve/reject/defer/patch/create |
| Process | `src/switchboard/services/deliverables/` (future) · **`:8124`** | Web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`, Coord `:8123` |
| Dual-strip | `PM_DELIVERABLES_HTTP_PRIMARY=service` after parity | Hermetic TestClient may leave unset |
| MCP | Out of Mode A cut | `:8111` |

Expanding mid-mission (writes, coordinator tick, KPI links, MCP move) requires a **new ADR or
explicit Mode B charter**.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after independence:

1. **Do not** ship Deliverables uvicorn / Caddy / dual-strip.
2. **Keep Deliverables/mission in-process** behind modular routers.
3. Deliverable may still exit on Path B with written rationale + measured evidence.
4. No-Go is a **valid terminal outcome**.
5. Auth + Tasks (+ Coord when live) Path A/Path B truth must remain green.

---

## Decision 5 — Exit criteria (`arch-ms-deliverables-service`)

**Path A — Deliverables cut (Go):**

- Independence gate recorded Go (including operator G6 when required).
- Deliverables runs as standalone uvicorn on `:8124` with thin-surface parity.
- Caddy routes the Decision 3 surface; dual-strip proven; rollback documented.
- Prior BC cuts still green; no half-cut façade; CI green.

**Path B — Documented No-Go:**

- Independence (or operator decision) records No-Go with written rationale.
- Deliverables remains in-process; no half-cut network façade.
- Charter + independence evidence landed; prior BCs still green; CI green.

Deliverable `arch-ms-deliverables-service` moves to **done** only with board-recorded merge
provenance on the canonical repo (plus closure verification). Agents use `complete_claim`;
they do not mark Done.

---

## Execution — board and tracker

Live status: `project=switchboard`, deliverable **`arch-ms-deliverables-service`**, mission
**`arch-ms-segmentation`**, workstream **ARCH-MS**. Tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Deliverables section).

| Milestone | Board tasks (indicative) |
|---|---|
| `independence` | ARCH-MS-97 (this ADR + thin surface), successor independence/Go-No-Go tasks |
| `process-cut` | Go-only uvicorn + Caddy + dual-strip |
| Exit | Path A or Path B evidence |

**View on board:**
`?project=switchboard&deliverable=arch-ms-deliverables-service#tab-mission`

---

## Consequences

- Agents must treat Deliverables process extraction as **blocked** until independence Go is recorded.
- Charter + thin-surface work (ARCH-MS-97) can proceed without cutting live traffic.
- Claiming “Deliverables microservice done” without ports / writers / ops proof is a charter violation.
- Mode A thin surface is a hard lock; widen only with a new charter decision.
- Auth + Tasks (+ Coord) cuts are **dependencies of truth**.

## Alternatives rejected

- **Mandatory Deliverables process cut.** Rejected — yellow light; No-Go is first-class.
- **Cut Deliverables before Coord charter.** Rejected by mission sequence + ARCH-MS-96 dependency.
- **Wide day-one surface (all writes + coordinator tick + MCP).** Rejected for Mode A.
- **Network-wrap without independence.** Forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007.
- **Postgres as a Deliverables prerequisite.** Rejected; ARCH-19 remains the SLO gate.
