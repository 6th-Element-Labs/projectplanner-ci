# ADR-0018 — Storage ports charter: backend-agnostic data layer (DATA-PORT)

- **Status:** Proposed — plan-of-record for deliverable `data-port-storage-abstraction`
  (operator accepts when the deliverable is scoped on the board).
- **Date:** 2026-07-21
- **Author:** Data abstraction lane (DATA-PORT) — charter session
- **Relates to:** [ADR-0007](0007-application-shell-cleanup.md) (ARCH-19 SQLite-vs-Postgres SLO
  gate — **unchanged by this ADR**) · [ADR-0009](0009-microservices-modernization.md) ·
  [ADR-0011](0011-phase2-process-strangler.md)…[ADR-0016](0016-ingest-inbox-process-strangler.md)
  (service peels — the between-services half of data abstraction) · SEG-1…SEG-7 (project
  segmentation; per-project physical isolation this ADR must preserve) · execution tracker
  [`DATA-PORT-EXECUTION.md`](../DATA-PORT-EXECUTION.md) · workstream **`DATA-PORT`**.

> **One sentence:** application code may know **business operations and project identity
> (metadata) only**; every fact about the storage backend — engine, dialect, connections,
> transactions, lock/error types — lives in a swappable storage adapter behind a declared
> port, enforced by a zero-ceiling import ratchet, so choosing SQLite, Postgres, Oracle, or a
> managed cloud database is an **adapter decision, not an application change**.

---

## Context — measured, not assumed

The app layer is already ~95% storage-ignorant by convention: routers, MCP tools, the plan
agent, triage, and digests call ~30 repository modules (`src/switchboard/storage/repositories/`)
plus root leaf stores (`rag_store.py`, `inbox_store.py`, …) and receive plain dicts. All
connections funnel through one seam (`db/connection.py::_conn(project)`), one SQLite file per
project (SEG isolation). Schema changes ride ledgered, numbered migrations (BUG-47).

What is **missing** is the contract that makes this guaranteed rather than habitual:

- ~48 non-test modules import `sqlite3`; ~1,200 raw SQL statements; ~25 `INSERT OR
  REPLACE/IGNORE`; ~70 `PRAGMA` references. Concentrated in the storage layer — but nothing
  stops the next PR from adding number 49 anywhere.
- **Known boundary violations (day-one fix list, census 2026-07-21):**

| # | File | Leak |
|---|---|---|
| 1 | `src/switchboard/api/routers/auth/ports.py` | Port Protocol returns `sqlite3.Connection` (`registry_conn()`) — the abstraction's own signature names the backend |
| 2 | `src/switchboard/api/routers/auth/store.py` | Storage module living inside the API router package; catches `sqlite3.OperationalError` |
| 3 | `src/switchboard/api/auth_port_adapters.py` | Adapter is legitimately SQLite-aware, but its contract (#1) forces `sqlite3.Connection` across the boundary |
| 4 | `src/switchboard/application/queries/audit_export.py` | Application query typed against `sqlite3.Connection`; raw table reads |
| 5 | `src/switchboard/application/queries/project_impact.py` | `sqlite3.Error` handling in application layer |
| 6 | `src/switchboard/application/queries/control_plane_probe.py` | `sqlite3.OperationalError` handling in application layer |
| 7 | `src/switchboard/application/queries/working_agreement.py` | "database is locked" awareness in application layer |
| 8 | `background_jobs.py` | `_persist_run(c: sqlite3.Connection, …)` — job code holds a raw connection |
| 9 | `mcp_observability.py` | Classifies `sqlite3.OperationalError` for observability |

Proven in-house patterns this charter reuses (no invention):

- **Ports + forbidden-import ratchet, ceiling 0** — ARCH-MS-87 (`services/tasks/ports.py`,
  `tests/test_arch_ms87_tasks_ports.py`).
- **Side-by-side parity gates** — ARCH-MS-91 (Tasks), ARCH-MS-110 (Deliverables).
- **Mechanical scope ratchets** — `scripts/seg6_scope_ratchet.py`, `scripts/seg4_endpoint_census.py`.
- **Behavioral conformance harness with a known-leak oracle** — `scripts/seg7_conformance.py`.

---

## Decision 1 — Scope (in / out)

**In scope:**

| Track | Intent |
|---|---|
| **Charter** | This ADR; DATA-PORT execution tracker; board deliverable scoping. |
| **Error taxonomy** | Backend-neutral storage exceptions (`StorageError`, `StorageBusy`, `StorageIntegrityError`); translation at the adapter boundary only. |
| **Leak remediation** | The nine files above stop importing/typing/catching anything SQLite. |
| **Port declaration** | Protocol-only storage ports per bounded context, extracted from existing repository signatures (dict-in/dict-out + `project` identity). |
| **Adapter consolidation** | All SQL/PRAGMA/connection code relocates under `src/switchboard/storage/adapters/sqlite/`; repositories become the SQLite adapter's implementation. |
| **Import ratchet** | `sqlite3` / `db.*` / adapter internals forbidden outside `storage/adapters/`, ceiling **0**, enforced in CI. |
| **Conformance suite** | Executable behavioral contract per port group; any adapter must pass 100% to be eligible. |
| **Swap playbook** | Documented, gated procedure for introducing a second backend (per BC, per project, reversible). |

**Out of scope:**

- **Writing any second adapter now.** No Postgres/Oracle/RDS/Aurora code lands under this
  charter. ARCH-19's SLO trigger still owns *when*; this charter owns *how cheap*.
- **ORM / SQL dialect translation layer.** Rejected (see Alternatives).
- **Schema changes, behavior changes, endpoint changes.** This is a boundary-hardening
  refactor; every existing test stays green unmodified (except imports).
- **Non-relational port redesign** (DynamoDB-class stores). A key-value backend changes the
  port *contract*, not just the implementation — that is a future ADR.
- **Service process cuts.** The peel missions (ADR-0012…0016) proceed independently; this
  charter must not block or be blocked by them.

---

## Decision 2 — Architecture rules (MUST)

1. **Metadata-only app layer.** Above the port line, code may reference: port Protocols,
   domain types, `ProjectContext` / project ids, and the neutral error taxonomy. Nothing else
   about storage.
2. **No backend types in port signatures.** A port method may not accept or return
   connections, cursors, rows, or engine exception types. (Rule #1's fix for leak #1.)
3. **Errors translate at the boundary.** Adapters catch engine exceptions and raise the
   neutral taxonomy. `sqlite3.OperationalError` appearing above `storage/adapters/` is a CI
   failure.
4. **Per-project isolation is part of the port contract.** Every port operation carries
   explicit project identity; an adapter maps project → physical store (file, database,
   schema — adapter's choice). The SEG-7 guarantee (foreign data invisible and immutable)
   must hold under **every** adapter, and is re-proven per adapter (Decision 5).
5. **Transactions are port-scoped.** Multi-write atomicity is expressed as a port operation
   (one method = one transaction), never by the app composing raw transactions across calls.
6. **The ratchet is the contract.** Forbidden-import ceiling stays 0 forever; raising it
   requires an ADR, not a PR comment.
7. **One adapter in production per bounded context at a time**, selected by explicit
   configuration; dual-running is only for parity gates.
8. **No behavior drift during consolidation.** Verbatim-move discipline (Phase 0/1 playbook):
   moved SQL is AST/behavior-identical, proven by the existing test suite.

---

## Decision 3 — Port taxonomy and layout

Ports group by bounded context, mirroring the service peel map so each future service owns a
port group outright:

| Port group | Today's modules (indicative) |
|---|---|
| `tasks` | `tasks_store.py`, `repositories/tasks.py`, claims |
| `coord` | `coordination_store.py`, `repositories/coordination.py`, decisions, signals |
| `deliverables` | `deliverables_store.py`, `repositories/deliverables.py`, closure, breakdown |
| `tally` | `kpis_economics_store.py`, `repositories/kpis_economics.py` |
| `ingest` | `inbox_store.py`, `repositories/*` inbox/intake, attachments |
| `corpus` | `rag_store.py` (rag_docs), summaries, publication |
| `conversation` | `repositories/plan_chat.py`, narration stores |
| `activity` | `activity_store.py`, `repositories/activity.py`, digests |
| `access` | `auth_store.py`, `repositories/access.py`, auth router store (leak #2 relocates here) |
| `control-plane` | project registry, provenance, jobs, runner, work sessions |

Layout:

```
src/switchboard/storage/
  ports/            # Protocols only — importable by anyone, imports nothing about engines
  adapters/
    sqlite/         # ALL SQL, PRAGMAs, connections, migrations live here
  errors.py         # neutral taxonomy
```

Port granularity follows the existing repository function signatures — extraction, not
redesign. Where a signature already leaks (a connection, an engine error), the port gets the
corrected shape and the adapter absorbs the difference.

---

## Decision 4 — Conformance suite and the swap protocol

**Conformance suite (per port group):** executable behavioral contract every adapter must
pass — upsert semantics, transaction atomicity/rollback, ordering guarantees, id generation,
empty/missing-row behavior, concurrent-writer behavior (the 8-agent gate), **and project
isolation** (SEG-7 harness parameterized by adapter). Includes a known-defect oracle in the
SEG-7 style: the suite must demonstrably fail against a deliberately broken adapter, so green
is meaningful.

**Swap protocol (when ARCH-19 fires or the operator elects a backend):**

1. Write `adapters/<backend>/` for the target port group(s). App code untouched — enforced,
   not promised, by the ratchet.
2. Pass the conformance suite at 100%; pass SEG-7 under the new adapter.
3. Side-by-side parity against the SQLite adapter on recorded workloads (ARCH-MS-91 pattern).
4. Cut over **per project** (per-project physical mapping makes tenant-at-a-time migration and
   rollback natural), or per BC where the service peel already isolates traffic.
5. Rollback = repoint configuration at the SQLite adapter; documented before cutover, drilled
   once.

---

## Decision 5 — Exit criteria (`data-port-storage-abstraction`)

- Neutral error taxonomy exists; the nine census leaks are fixed; zero `sqlite3` references
  above `storage/adapters/` — ratchet green at ceiling 0.
- Port Protocols declared for all ten groups; app/application/api/mcp/services layers import
  ports only.
- All SQL consolidated under `storage/adapters/sqlite/`; full test suite green with no
  behavioral edits.
- Conformance suite runs in CI against the SQLite adapter, including the broken-adapter
  oracle proof and SEG-7-under-adapter.
- Swap playbook documented; exit gate script reports all of the above machine-readably.
- **Explicitly not required for exit:** any second adapter.

Deliverable moves to **done** only with board-recorded merge provenance plus closure
verification (Switchboard provenance rules; agents use `complete_claim`).

---

## Execution — board and tracker

Live status: `project=switchboard`, deliverable **`data-port-storage-abstraction`**,
workstream **DATA-PORT**. Tracker: [`docs/DATA-PORT-EXECUTION.md`](../DATA-PORT-EXECUTION.md).

| Milestone | Board tasks (indicative) |
|---|---|
| `charter-rails` | DATA-PORT-1 (this ADR + tracker), DATA-PORT-2 (error taxonomy) |
| `leak-zero` | DATA-PORT-3…5 (the nine census fixes) |
| `ports-declared` | DATA-PORT-6…7 (Protocols + adapter consolidation) |
| `ratchet-locked` | DATA-PORT-8 (forbidden-import ceiling 0) |
| `conformance` | DATA-PORT-9…10 (suite + SEG-7 per adapter + oracle) |
| Exit | DATA-PORT-11…12 (playbook + exit gate) |

---

## Consequences

- After `ratchet-locked`, "swap the database" is a storage-layer PR by CI-enforced
  construction; app diffs in a backend swap are a charter violation.
- New storage features must land as port + adapter from day one; direct `sqlite3` use
  anywhere else fails CI.
- ARCH-19 stays the *when*; this charter reduces its cost to: one adapter per BC + gates.
- Combined with the service peels, both halves of data abstraction hold: **between** services
  (database-per-service, API-only access) and **within** a service (ports/adapters).
- The SEG-7 isolation guarantee becomes portable — proven per adapter, not per engine.

## Alternatives rejected

- **ORM / dialect layer under existing call sites (SQLAlchemy et al.).** Pays the ~1,200-
  statement translation cost immediately, adds a permanent dependency and query-plan
  opacity, and still cannot cover non-relational targets. Ports defer that cost until a
  second adapter is actually wanted, and confine it to the storage layer.
- **Universal any-datastore DAL (relational + key-value behind one interface).** Collapses to
  lowest-common-denominator key-value; loses transactions, ordering, and query capability
  the app relies on. A non-relational backend is a port-contract redesign — future ADR.
- **Write the Postgres adapter now "while we're in there."** Rejected; ARCH-19's evidence
  bar stands. Building the second adapter without the SLO breach is speculative work and
  weakens the conformance suite's honesty (it would be tuned to two engines, not to a
  contract).
- **Big-bang relocation of all storage code in one PR.** Rejected; verbatim-move discipline
  per port group, exactly like Phase 0/1 extractions.
- **Do nothing until ARCH-19 fires.** Rejected by the operator's standing requirement:
  backend choice must be a swap, and only a ratcheted boundary makes that true over time.
