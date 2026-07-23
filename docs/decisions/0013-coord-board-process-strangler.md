# ADR-0013 — Coordination / board process strangler charter (Mode A)

- **Status:** Accepted — ARCH-MS-96 (plan-of-record for deliverable `arch-ms-coord-service`).
- **Date:** 2026-07-16
- **Author:** Platform modernization lane (ARCH-MS) — Coord charter session
- **Relates to:** [ADR-0012](0012-phase3-tasks-process-strangler.md) (Tasks Mode A live) ·
  [ADR-0011](0011-phase2-process-strangler.md) (Auth strangler playbook) ·
  [ADR-0009](0009-microservices-modernization.md) · [ADR-0007](0007-application-shell-cleanup.md)
  (Caddy edge; no nginx) · board deliverable **`arch-ms-coord-service`** · mission
  **`arch-ms-segmentation`** · execution tracker [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) ·
  thin surface [`thin_day_one_surface.md`](../coord/thin_day_one_surface.md) · workstream
  **`ARCH-MS`** (ARCH-MS-96+).

> **Mode A** means: one BC (Coordination / board), yellow-light process cut, and a **thin
> day-one cut surface** (`:8123`; board summary + lane delta + plan signals + read coordination
> rollups). It does **not** mean “cut MCP,” “cut messaging/wakes/agents,” or “cut Deliverables
> in the same phase.”

---

## Context — why Coord next, and why the cut stays yellow

Auth (ADR-0011) and Tasks (ADR-0012) already prove the conditional strangler playbook.
Segmentation mission `arch-ms-segmentation` sequences remaining peels:

> Tasks live cut → **Coord / board** → Deliverables → Tally → Ingest.

Deliverable `arch-ms-coord-service` end state:

> Board / coordination bounded context is a live process behind Caddy (or documented No-Go).
> Chart Board/coord block green.

Board routers today still import root `store` / `dispatch` / `signals` / `auth` directly
(`src/switchboard/api/routers/board.py`, `coordination.py`). Cutting without ports would turn
that coupling into networked half-cut — forbidden by the Auth playbook.

**This ADR is the plan-of-record for deliverable `arch-ms-coord-service`.** It does not reopen
Auth or Tasks Path A cuts, ADR-0007’s Caddy decision, or ARCH-19’s Postgres SLO gate.

---

## Decision 1 — Coord scope (in / out) — Mode A

**In scope:**

| Track | Intent |
|---|---|
| **Charter + thin surface** | This ADR; locked day-one route list + `:8123` (ARCH-MS-96). |
| **Independence gate** | Ports (no root `store` / `dispatch` / `auth` imports); exclusive writers / shared-SQLite policy; Auth + Tasks not regressed; ops proof; recorded Go/No-Go **before** any process cut. |
| **Process cut (conditional)** | Standalone Coord uvicorn on **`:8123`** + side-by-side parity + Caddy cutover + dual-strip **only if** Go. |
| **Exit** | Live cut **or** documented No-Go; Auth + Tasks live cuts remain green. |

**Out of scope for Mode A:**

- Other BCs (Deliverables/mission, Tally, Ingest, Messaging, Runner, …) as process cuts.
- Full `/ixp/v1/*` edge move — **thin IXP only** (`/ixp/v1/delta`); agents/messaging/wakes/claims stay on monolith (or Tasks for claim TXP).
- MCP process cut — MCP stays on `:8111`.
- Coordinator work starts only through Task Execution; no project-wide dispatch route remains.
- `/api/people`, `/api/dispatch/status`, `/ixp/v1/saturation_signals` day one (ops/UI siblings).
- Nginx; Postgres migration (ARCH-19); mandatory Coord leave-process if independence fails.

---

## Decision 2 — Process strangler rules (MUST) — reuse Auth playbook

Coord inherits ADR-0011 Decision 2 / ADR-0012 Decision 2:

1. **One bounded context per cut.** This charter cuts **Coordination / board only**.
2. **Green façade always.** Stable REST seams keep working while the slice moves.
3. **Do not regress Auth (`:8121`) or Tasks (`:8122`).**
4. **Coord process cut is CONDITIONAL (yellow light).** HOLD until independence records **Go**.
5. **Never convert in-process coupling into network coupling.** No ports / unsafe multi-writer SQLite ⇒ stay in-process.
6. **Deploy = FastAPI + uvicorn + Caddy.** Coord (when Go) is another localhost uvicorn behind the same Caddy edge.
7. **SQLite default.** Fail closed to in-process if two-process writers are unsafe.
8. **Reuse the Auth/Tasks cut playbook.** Side-by-side → Caddy fragment → live cut → dual-strip → documented rollback.

---

## Decision 3 — Thin day-one cut surface (Mode A lock)

Canonical table: [`docs/coord/thin_day_one_surface.md`](../coord/thin_day_one_surface.md).

| Surface | Day-one Coord process (`:8123`) | Stays on monolith |
|---|---|---|
| Board summary | `GET /api/board` | — |
| Plan signals | `GET /api/signals` | — |
| Lane delta | `GET /ixp/v1/delta` | Other `/ixp/v1/*` |
| Coord read | `GET /api/coordination`, `GET /api/coordinator_decisions` | Task Execution owns all work starts |
| Process | `src/switchboard/services/coord/` (future) · systemd · **`:8123`** | Web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122` |
| Dual-strip | `PM_COORD_HTTP_PRIMARY=service` (Auth/Tasks analogue) after parity | Hermetic TestClient may leave unset |
| MCP | Out of Mode A cut | `:8111` |

Expanding the cut surface mid-mission (messaging, agents, wake TXP, deliverables, write dispatch)
requires a **new ADR or explicit Mode B charter** — not a silent widening of Mode A.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after independence:

1. **Do not** ship Coord uvicorn / Caddy / dual-strip.
2. **Keep Coord/board in-process** behind modular routers.
3. Deliverable may still exit on Path B with written rationale + measured evidence.
4. No-Go is a **valid terminal outcome**. Prefer an honest modular monolith over a networked monolith.
5. Auth Path A + Tasks Path A must remain green either way.

---

## Decision 5 — Exit criteria (`arch-ms-coord-service`)

**Path A — Coord cut (Go):**

- Independence gate recorded Go (including operator G6 when required by the gate).
- Coord runs as standalone uvicorn on `:8123` with thin-surface parity.
- Caddy routes the Decision 3 surface; dual-strip proven; rollback documented.
- Auth + Tasks live cuts still green; no half-cut façade; CI green.

**Path B — Documented No-Go:**

- Independence (or operator decision) records No-Go with written rationale.
- Coord remains in-process; no half-cut network façade.
- Charter + independence evidence landed; Auth + Tasks still green; CI green.

Deliverable `arch-ms-coord-service` moves to **done** only with board-recorded merge provenance
on the canonical repo (plus closure verification). Agents use `complete_claim`; they do not mark Done.

---

## Execution — board and tracker

Live status: `project=switchboard`, deliverable **`arch-ms-coord-service`**, mission
**`arch-ms-segmentation`**, workstream **ARCH-MS**. Tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Coord section).

| Milestone | Board tasks (indicative) |
|---|---|
| `independence` | ARCH-MS-96 (this ADR + thin surface), successor independence/Go-No-Go tasks |
| `process-cut` | Go-only uvicorn + Caddy + dual-strip |
| Exit | Path A or Path B evidence |

**View on board:**
`?project=switchboard&deliverable=arch-ms-coord-service#tab-mission`

---

## Consequences

- Agents must treat Coord process extraction as **blocked** until independence Go is recorded.
- Charter + thin-surface work (ARCH-MS-96) can proceed without cutting live traffic.
- Claiming “Coord microservice done” without ports / writers / ops proof is a charter violation.
- Mode A thin surface is a hard lock; widen only with a new charter decision.
- Auth + Tasks live cuts are **dependencies of truth** — Coord work must not undo them.

## Alternatives rejected

- **Mandatory Coord process cut.** Rejected — yellow light; No-Go is first-class.
- **Cut Coord before Tasks live.** Rejected by mission sequence + ARCH-MS-95 dependency.
- **Wide day-one surface (full IXP + messaging + write dispatch).** Rejected for Mode A.
- **Network-wrap Coord without independence.** Forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007.
- **Postgres as a Coord prerequisite.** Rejected; ARCH-19 remains the SLO gate.
