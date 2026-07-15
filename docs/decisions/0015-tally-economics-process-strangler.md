# ADR-0015 — Tally / economics process strangler charter (Mode A)

- **Status:** Accepted — ARCH-MS-98 (plan-of-record for deliverable `arch-ms-tally-service`).
- **Date:** 2026-07-16
- **Author:** Platform modernization lane (ARCH-MS) — Tally charter session
- **Relates to:** [ADR-0014](0014-deliverables-mission-process-strangler.md) (Deliverables Mode A) ·
  [ADR-0013](0013-coord-board-process-strangler.md) · [ADR-0012](0012-phase3-tasks-process-strangler.md) ·
  [ADR-0011](0011-phase2-process-strangler.md) (Auth playbook) ·
  [ADR-0009](0009-microservices-modernization.md) · [ADR-0007](0007-application-shell-cleanup.md)
  (Caddy edge; no nginx) · [ADR-0002](0002-llm-cost-attribution.md) (cost attribution) · board
  deliverable **`arch-ms-tally-service`** · mission **`arch-ms-segmentation`** · execution tracker
  [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) · thin surface
  [`thin_day_one_surface.md`](../tally/thin_day_one_surface.md) · workstream **`ARCH-MS`**
  (ARCH-MS-98+).

> **Mode A** means: one BC (Tally / economics), yellow-light process cut, and a **thin day-one
> ledger surface** (`:8125`; OXP **read** tallies for project/task/KPI/deliverable/outcome). It
> does **not** mean “cut spend ingest,” “cut outcome verify/reject writes,” or “cut Ingest BC in
> the same phase.”

---

## Context — why Tally next, and why the cut stays yellow

Segmentation mission `arch-ms-segmentation` sequences remaining peels:

> Tasks live cut → Coord / board → Deliverables / mission → **Tally / economics** → Ingest.

Deliverable `arch-ms-tally-service` end state:

> Tally / economics is a live process behind Caddy (or documented No-Go). Chart Tally block green.

The tally router still imports root `store` / `auth` directly
(`src/switchboard/api/routers/tally.py`). Cutting without ports would turn that coupling into a
networked half-cut — forbidden by the Auth playbook.

**This ADR is the plan-of-record for deliverable `arch-ms-tally-service`.** It does not reopen
prior BC charters; Auth/Tasks live cuts (and Coord/Deliverables when live) must stay green.

---

## Decision 1 — Tally scope (in / out) — Mode A

**In scope:**

| Track | Intent |
|---|---|
| **Charter + thin surface** | This ADR; locked day-one ledger list + `:8125` (ARCH-MS-98). |
| **Independence gate** | Ports (no root `store` / `auth` imports); exclusive writers / shared-SQLite policy; prior BCs not regressed; ops proof; recorded Go/No-Go **before** any process cut. |
| **Process cut (conditional)** | Standalone Tally uvicorn on **`:8125`** + side-by-side parity + Caddy cutover + dual-strip **only if** Go. |
| **Exit** | Live cut **or** documented No-Go; prior BC cuts remain green. |

**Out of scope for Mode A:**

- Other BCs (Ingest, Messaging, Runner, …) as process cuts.
- Day-one **writes**: spend ingest, outcome create/verify/reject, KPI create/patch, outcome↔KPI links.
- MCP process cut — MCP stays on `:8111` (MCP report_usage may later call Tally via edge; not Mode A).
- Nginx; Postgres migration (ARCH-19); mandatory leave-process if independence fails.

---

## Decision 2 — Process strangler rules (MUST) — reuse Auth playbook

Tally inherits ADR-0011…0014 Decision 2:

1. **One bounded context per cut.** This charter cuts **Tally / economics only**.
2. **Green façade always.** Stable `/tally/v1/*` seams keep working while the slice moves.
3. **Do not regress Auth (`:8121`), Tasks (`:8122`), Coord (`:8123`), Deliverables (`:8124`) when live.**
4. **Tally process cut is CONDITIONAL (yellow light).** HOLD until independence records **Go**.
5. **Never convert in-process coupling into network coupling.**
6. **Deploy = FastAPI + uvicorn + Caddy.** Tally (when Go) is another localhost uvicorn behind the same Caddy edge.
7. **SQLite default.** Fail closed to in-process if two-process writers are unsafe.
8. **Reuse the Auth/Tasks cut playbook.** Side-by-side → Caddy fragment → live cut → dual-strip → documented rollback.

---

## Decision 3 — Thin day-one cut surface (Mode A lock)

Canonical table: [`docs/tally/thin_day_one_surface.md`](../tally/thin_day_one_surface.md).

| Surface | Day-one Tally process (`:8125`) | Stays on monolith |
|---|---|---|
| KPI / outcome lists | `GET /tally/v1/kpis`, `GET /tally/v1/outcomes` | `POST /tally/v1/kpis`, `PATCH …/kpis/{id}`, outcome writes |
| Rollups | `GET /tally/v1/project`, `GET /tally/v1/task/{id}`, `GET /tally/v1/kpi/{id}`, `GET /tally/v1/deliverable/{id}` | — |
| Spend / links | — | `POST /tally/v1/spend/ingest`, `POST /tally/v1/outcome_kpi_links` |
| Outcomes mutate | — | `POST /tally/v1/outcomes`, `…/verify`, `…/reject` |
| Process | `src/switchboard/services/tally/` (future) · **`:8125`** | Web `:8110`, MCP `:8111`, Auth `:8121`, Tasks `:8122`, Coord `:8123`, Deliverables `:8124` |
| Dual-strip | `PM_TALLY_HTTP_PRIMARY=service` after parity | Hermetic TestClient may leave unset |
| MCP | Out of Mode A cut | `:8111` |

Expanding mid-mission (spend ingest on the cut, outcome writes, MCP report_usage relocation)
requires a **new ADR or explicit Mode B charter**.

---

## Decision 4 — Explicit keep-in-process No-Go exit

If Go/No-Go = **No-Go** after independence:

1. **Do not** ship Tally uvicorn / Caddy / dual-strip.
2. **Keep Tally/economics in-process** behind modular routers.
3. Deliverable may still exit on Path B with written rationale + measured evidence.
4. No-Go is a **valid terminal outcome**.
5. Prior BC Path A/Path B truth must remain green.

---

## Decision 5 — Exit criteria (`arch-ms-tally-service`)

**Path A — Tally cut (Go):**

- Independence gate recorded Go (including operator G6 when required).
- Tally runs as standalone uvicorn on `:8125` with thin-surface parity.
- Caddy routes the Decision 3 surface; dual-strip proven; rollback documented.
- Prior BC cuts still green; no half-cut façade; CI green.

**Path B — Documented No-Go:**

- Independence (or operator decision) records No-Go with written rationale.
- Tally remains in-process; no half-cut network façade.
- Charter + independence evidence landed; prior BCs still green; CI green.

Deliverable `arch-ms-tally-service` moves to **done** only with board-recorded merge provenance
on the canonical repo (plus closure verification). Agents use `complete_claim`; they do not mark Done.

---

## Execution — board and tracker

Live status: `project=switchboard`, deliverable **`arch-ms-tally-service`**, mission
**`arch-ms-segmentation`**, workstream **ARCH-MS**. Tracker:
[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) (Tally section).

| Milestone | Board tasks (indicative) |
|---|---|
| `independence` | ARCH-MS-98 (this ADR + thin surface), successor independence/Go-No-Go tasks |
| `process-cut` | Go-only uvicorn + Caddy + dual-strip |
| Exit | Path A or Path B evidence |

**View on board:**
`?project=switchboard&deliverable=arch-ms-tally-service#tab-mission`

---

## Consequences

- Agents must treat Tally process extraction as **blocked** until independence Go is recorded.
- Charter + thin-surface work (ARCH-MS-98) can proceed without cutting live traffic.
- Claiming “Tally microservice done” without ports / writers / ops proof is a charter violation.
- Mode A thin surface is a hard lock; widen only with a new charter decision.
- Prior BC cuts are **dependencies of truth**.

## Alternatives rejected

- **Mandatory Tally process cut.** Rejected — yellow light; No-Go is first-class.
- **Cut Tally before Deliverables charter.** Rejected by mission sequence + ARCH-MS-97 dependency.
- **Wide day-one surface (spend ingest + outcome writes + MCP).** Rejected for Mode A.
- **Network-wrap without independence.** Forbidden by Decision 2 #5.
- **Nginx or edge redesign.** Rejected by ADR-0007.
- **Postgres as a Tally prerequisite.** Rejected; ARCH-19 remains the SLO gate.
