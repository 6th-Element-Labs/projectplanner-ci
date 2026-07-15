# Tasks independence gate — ownership, writers, Auth binding (ARCH-MS-88)

**Status:** Accepted decisions for deliverable `arch-ms-phase-3` independence gate.  
**Relates to:** [ADR-0012](decisions/0012-phase3-tasks-process-strangler.md) ·
[`ARCH-MS-PHASE2-TASKS-READINESS.md`](ARCH-MS-PHASE2-TASKS-READINESS.md) ·
[AUTH-INDEPENDENCE-GATE.md](AUTH-INDEPENDENCE-GATE.md) (playbook) · board tasks
ARCH-MS-87…89 · conditional cut ARCH-MS-90+.

This document records **Go/No-Go inputs** for a Tasks process cut. It does not authorize the cut.

---

## Decision 1 — Exclusive writers (one owner per concern)

Shared **project** SQLite (per-board `*.db`, not Auth’s `project_registry.db`) is allowed
**in-process** while each table/concern has a single writer BC. Dual writers on the same table
across processes are a **No-Go** for process cut until ARCH-MS-89 measures contention
(ADR-0012 Decision 2 #5 / #7).

| Table / concern | Exclusive writer | Readers | Notes |
|---|---|---|---|
| `tasks` | **Tasks** (application commands + Tasks repos / ports) | Board UI, MCP, deliverables, narration | Status/assignee/sort mutations are Tasks-owned. |
| `task_claims` | **Tasks** (claim lifecycle) | Board, MCP, reconciliation | Claim create/complete/abandon/revoke. |
| `activity` (task-scoped kinds) | **Tasks** (`append_activity` / comment / claim events) | Board, MCP, ops export | Other BCs may append **only** via Tasks ports/APIs after a cut — not direct SQL. |
| Task `resource_leases` (`resource_type='task'`) | **Tasks** (with claim lifecycle) | Board, agents | Claim lease release is Tasks-owned. |
| Task archive / move snapshots | **Tasks** | Ops / cleanup | Archive/move stay on Tasks day-one surface. |
| `work_sessions` | **IXP / work-session BC** (lookup via Tasks port) | Tasks (claim binding) | Tasks **reads** via `WorkSessionLookupPort`; does not own session DDL writers. |
| Git / provenance / external CI on tasks | **Provenance / CI BC** (today via store façade) | Tasks detail hydration | Day-one cut must not become a second writer; hydrate through ports or keep co-located. |
| Review verdicts / remediations | **Review BC** (monolith siblings) | Board | Out of Mode A cut (`…/review_*` stays on monolith). |
| Dispatch / plan chat under `/api/tasks*` | **Monolith siblings** | — | Out of Mode A cut (`…/dispatch`, `…/chat`). |
| Auth sessions / users / grants | **Auth / Access** | Tasks (principal → actor via ports) | Tasks never writes Auth tables. |

**Measured shared-SQLite policy (Phase 3 day one):**

1. **In-process:** exclusive-writer-by-table above; shared file OK.
2. **Process cut (`:8122` + monolith):** same file only if ARCH-MS-89 contention/rollback
   proof passes **and** no second process writes Tasks-owned tables outside the Tasks unit.
3. **Fail closed:** if multi-process writers are unsafe → keep Tasks in-process (Path B No-Go).

**Monolith rule:** do not `INSERT`/`UPDATE` Tasks-owned tables from Auth, Messaging, or
dispatch code paths except through Tasks application commands / ports.

---

## Decision 2 — Auth / write-binding via ports only (fail-closed)

Tasks must not import root `auth` / `store` / `dispatch` inside
`src/switchboard/services/tasks/` (ARCH-MS-87 ratchet). Principal and write-binding
coupling goes through:

| Port | Role |
|---|---|
| `TaskPrincipalPort.actor` | Map Auth principal → public write actor |
| `TaskWriteBindingPort.resolve_write_actor` | Bind shared env tokens / system actors |
| `TaskWriteBindingPort.write_binding_activity_payload` | Audit payload for activity |
| `require_write_binding` (`services/tasks/binding.py`) | **Fail-closed** gate before mutation |

| Surface when binding / Auth coupling is unavailable | Required behavior |
|---|---|
| Task/claim mutation with naked `env-*-token` and no `agent_id` / `system_actor` | **Fail closed** → deny (`unbound_identity`); never author as the shared token. |
| `system_actor` without `system_reason` | **Fail closed** → deny. |
| Write-binding port returns `ok: false` / `error` / empty actor | **Fail closed** → `WriteBindingError`; do not invent a local actor. |
| Auth process down (future remote Auth) | Tasks must not invent offline principal trust; deny writes that need a live binding. |
| `/health` liveness | May stay green; readiness for writes is binding + DB, not liveness alone. |

**Rejected:** falling open to unbound env-token authorship, or calling root `auth` from the
Tasks service package “temporarily” during a cut.

Implemented helper: `switchboard.services.tasks.binding.require_write_binding` (ARCH-MS-88).

---

## Decision 3 — Day-one cut surface stays thin (Mode A)

Ownership above applies to the Mode A surface only (`/api/tasks*` CRUD + claim-only TXP).
Review / dispatch / chat remain monolith-owned and must not be dual-mounted into `:8122`
(see ADR-0012 Decision 3). Expanding ownership mid-phase requires a new ADR or Mode B charter.

---

## Go / No-Go checklist (inputs for ARCH-MS-90)

Measured by ARCH-MS-89 harnesses (ops proof) and ARCH-MS-87/88 ratchets/docs. Operator owns **G6**.

| # | Input | Go requires | Status |
|---|---|---|---|
| G1 | Exclusive writers / shared-SQLite policy (Decision 1) | Matrix above; no dual Tasks-table writers across processes | ✅ Documented (ARCH-MS-88); multi-process proof → ARCH-MS-89 |
| G2 | Auth/write-binding fail-closed (Decision 2) | Ports-only; unbound env-token denied | ✅ Code: `require_write_binding` + ARCH-MS-87 ports |
| G3 | Thin Mode A surface (Decision 3) | No review/dispatch/chat in Tasks process | ✅ Charter (ADR-0012); re-check at cut |
| G4 | Ports independence | ARCH-MS-87 import lint / `tasks_forbidden_imports` ceiling 0 | ✅ Done (ARCH-MS-87) |
| G5 | Ops proof | SQLite contention, second uvicorn budget, Caddy rollback, API parity | ✅ Measured (see below) |
| G6 | Operator decision | Explicit Go recorded on board before ARCH-MS-90 | ⬜ After G1–G5 |

**Harness recommendation (not G6):** **Conditional Go** — hermetic ARCH-MS-89 harnesses
passed. Verdict artifact:
[`docs/phase3/tasks_independence_verdict.json`](phase3/tasks_independence_verdict.json)
(`verdict=go`, `operator_g6_required=true`). This does **not** authorize ARCH-MS-90+ until
the operator records G6.

**No-Go (keep Tasks in-process)** if any of G1–G5 fail, or if cutting would create two writers /
network-wrap unresolved Auth binding. No-Go is a valid Phase 3 exit path (ADR-0012 Decision 4).

---

## ARCH-MS-89 measured results

### Ops proof (hermetic)

Executable: `python scripts/arch_ms89_tasks_ops_proof.py`.

| Proof | Result | Detail |
|---|---|---|
| Multi-process project SQLite contention | **Pass** | Tasks worker + monolith sibling (activity/meta), 40 rounds each on one switchboard DB: **0** lock errors, 80/80 ops ok (~0.7s Tasks / ~0.1s monolith on laptop). Short-load only — production soak still required before G6. |
| Second uvicorn RSS budget | **Pass** | Skeleton uvicorn ~**51 MiB** RSS vs soft budget **80 MiB**; ~**199 MiB** headroom vs interactive `MemoryLow=250M`. Re-measure when ARCH-MS-90 stands up real Tasks `create_app`. |
| Day-one API parity | **Pass** | Unauthenticated `POST /api/tasks` → **401** (never 403); authenticated path ≠ 403; `require_write_binding` denies naked `env-*-token`. |
| Caddy cutover/rollback drill | **Pass (artifacts only)** | Fragment + runbook + unit example present; live `deploy/Caddyfile` has **no** premature `/api/tasks*` → `:8122` cut. |

### Caveats before G6

1. Contention harness is short-load / laptop; VM soak under real board write traffic may differ.
2. Second-uvicorn budget used the ARCH-MS-73 skeleton as a stand-in; Tasks package RSS should be
   re-measured when ARCH-MS-90 stands up the real unit.
3. Live Caddy Tasks cut remains forbidden until operator G6 + ARCH-MS-90…92.

---

## Related evidence

| Artifact | Role |
|---|---|
| `src/switchboard/services/tasks/ports.py` | Protocols (ARCH-MS-87) |
| `src/switchboard/api/tasks_port_adapters.py` | Monolith adapters outside package |
| `src/switchboard/services/tasks/binding.py` | Fail-closed binding gate (ARCH-MS-88) |
| `perf/arch_ms84_ratchet_baseline.json` → `tasks_forbidden_imports` | Import-direction ceiling 0 |
| `tests/test_arch_ms87_tasks_ports.py` | Ports proof |
| `tests/test_arch_ms88_tasks_ownership.py` | Ownership + fail-closed binding proof |
| `scripts/arch_ms89_tasks_ops_proof.py` | Ops proof harness (ARCH-MS-89) |
| `docs/phase3/tasks_independence_verdict.json` | Recorded Go/No-Go verdict |
| `tests/test_arch_ms89_tasks_ops_proof.py` | Executable proof |
