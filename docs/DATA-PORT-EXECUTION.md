# DATA-PORT execution tracker — backend-agnostic storage ports

**Charter:** [ADR-0018 — Storage ports: backend-agnostic data layer](decisions/0018-storage-ports-backend-agnostic-data-layer.md)
**Board:** `project=switchboard` · workstream **DATA-PORT**
**Deliverable:** `data-port-storage-abstraction`
**End state:** application code knows business operations + project identity (metadata) only;
every backend fact lives in `storage/adapters/`; forbidden-import ratchet at ceiling **0**;
conformance suite proves any adapter behaviorally identical. Swapping SQLite for Postgres /
Oracle / a managed cloud database is an adapter PR — zero app diffs, enforced by CI.

**Relationship to ARCH-19 (ADR-0007):** unchanged. ARCH-19 owns *when* a second backend is
justified (SLO breach evidence); this deliverable owns *how cheap* that swap is. Exit here
requires **no** second adapter.

---

## Milestones

| ID | Title | Intent |
|---|---|---|
| `charter-rails` | Charter + error taxonomy | ADR-0018 accepted; neutral storage errors exist |
| `leak-zero` | Census leak remediation | Nine known violations fixed; no engine types/errors above the storage line |
| `ports-declared` | Port Protocols + adapter consolidation | Ten port groups declared; all SQL under `storage/adapters/sqlite/` |
| `ratchet-locked` | Import ratchet ceiling 0 | Boundary becomes mechanical, permanent |
| `conformance` | Behavioral contract executable | Suite + broken-adapter oracle + SEG-7-under-adapter in CI |
| Exit | Swap playbook + exit gate | Machine-readable exit verdict green |

---

## Task table

| Task | Title | Milestone | Deps | Tracker | Repo evidence |
|---|---|---|---|---|---|
| **DATA-PORT-1** | Charter: ADR-0018 + this tracker | `charter-rails` | — | 🟡 | `docs/decisions/0018-storage-ports-backend-agnostic-data-layer.md`; this file |
| **DATA-PORT-2** | Neutral storage error taxonomy (`StorageError` / `StorageBusy` / `StorageIntegrityError`) + adapter-boundary translation | `charter-rails` | 1 | ⬜ | `src/switchboard/storage/errors.py`; retry sites move off `sqlite3.OperationalError` |
| **DATA-PORT-3** | Application-query leak fixes: `audit_export`, `project_impact`, `control_plane_probe`, `working_agreement` go through repositories/ports; no `sqlite3` types or errors | `leak-zero` | 2 | ⬜ | ADR-0018 census #4–7 |
| **DATA-PORT-4** | Auth storage relocation: `routers/auth/store.py` → `access` port group; `auth/ports.py` drops `sqlite3.Connection` from `registry_conn()`; adapters absorb the shape | `leak-zero` | 2 | ⬜ | ADR-0018 census #1–3 |
| **DATA-PORT-5** | Job/observability leak fixes: `background_jobs.py` stops holding raw connections; `mcp_observability.py` classifies neutral `StorageBusy` | `leak-zero` | 2 | ⬜ | ADR-0018 census #8–9 |
| **DATA-PORT-6** | Declare port Protocols for the ten groups (`tasks`, `coord`, `deliverables`, `tally`, `ingest`, `corpus`, `conversation`, `activity`, `access`, `control-plane`) — extraction from existing repository signatures, no redesign | `ports-declared` | 3,4,5 | ⬜ | `src/switchboard/storage/ports/` |
| **DATA-PORT-7** | Consolidate SQL under `storage/adapters/sqlite/` — root leaf stores + `db/` relocate verbatim (per-group PRs, Phase 0/1 move discipline); compatibility facades where callers linger | `ports-declared` | 6 | ⬜ | `src/switchboard/storage/adapters/sqlite/`; suite green unmodified |
| **DATA-PORT-8** | Forbidden-import ratchet: `sqlite3` / `db.*` / adapter internals banned outside `storage/adapters/`, ceiling **0**, CI-enforced (ARCH-MS-87 pattern) | `ratchet-locked` | 7 | ⬜ | `scripts/data_port_ratchet.py`; `tests/test_data_port_ratchet.py` |
| **DATA-PORT-9** | Port conformance suite: upsert/transaction/ordering/id/concurrency semantics per group + **broken-adapter oracle** (suite must fail a deliberately defective adapter) | `conformance` | 6 | ⬜ | `tests/conformance/`; oracle proof |
| **DATA-PORT-10** | SEG-7 isolation harness parameterized by adapter — project isolation proven per adapter, not per engine | `conformance` | 9 | ⬜ | `scripts/seg7_conformance.py` adapter parameter |
| **DATA-PORT-11** | Backend swap playbook: adapter authoring guide, parity gate (ARCH-MS-91 pattern), per-project cutover + rollback drill doc | Exit | 8,9,10 | ⬜ | `docs/runbooks/storage-adapter-swap.md` |
| **DATA-PORT-12** | Exit gate: machine-readable verdict — leaks 0, ratchet 0, ports complete, conformance + oracle + SEG-7 green | Exit | 11 | ⬜ | `scripts/data_port_exit_gate.py`; `tests/test_data_port_exit_gate.py` |

Update the **Repo evidence** column when a PR merges. Board status follows Switchboard
provenance rules — agents use `complete_claim`; Done requires merge webhook or reconcile.

---

## Hard rules (from ADR-0018 Decision 2)

1. Metadata-only app layer: ports, domain types, `ProjectContext`, neutral errors — nothing else.
2. No engine types (connections/cursors/rows/exceptions) in any port signature.
3. Engine errors translate at the adapter boundary; `sqlite3.*` above `storage/adapters/` fails CI.
4. Project isolation is part of the port contract; SEG-7 must hold under every adapter.
5. One port method = one transaction; the app never composes raw transactions.
6. Ratchet ceiling stays 0; raising it takes an ADR.
7. No behavior drift during moves — verbatim relocation, existing tests green unmodified.
8. **No second adapter under this deliverable** — ARCH-19 owns that trigger.

---

## Changelog

| Date | Actor | Note |
|---|---|---|
| 2026-07-21 | DATA-PORT-1 | Initial charter ADR-0018 + tracker; SQLite-awareness census recorded (9 boundary leaks, ~48 storage modules, ~1,200 statements) |
