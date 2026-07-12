# ADR-0009 — Microservices modernization: Phase 0 charter (ADR-007 rails + `src/switchboard/` scaffold)

- **Status:** Accepted — ARCH-MS-1 merged as PR #314; Phase 0 exit is enforced by ARCH-MS-24.
- **Date:** 2026-07-12
- **Author:** Platform modernization lane (ARCH-MS) — charter session
- **Relates to:** [ADR-0007](0007-application-shell-cleanup.md) (Decision 7 upgrade: compass → committed
  program; ratchet, redirect rules, target shape) · [ADR-0006](0006-control-plane-done-enough.md)
  (subtraction rule; control-plane freeze) · [ADR-0005](0005-store-module-decomposition.md) (what
  *not* to repeat — scheduled horizontal reorder) ·
  [`AUTH-MICROSERVICE-DESIGN.md`](../AUTH-MICROSERVICE-DESIGN.md) (strangler pattern; auth as slice
  0) · board deliverable **`arch-ms-phase-0`** · execution tracker
  [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) · workstream **`ARCH-MS`** (ARCH-MS-1 …
  ARCH-MS-24).

---

## Context — why a charter, and why now

ADR-0007 (2026-07-11) did three things that matter here:

1. **Held the line** on the four shell monoliths with an automated size ratchet (Decision 2) and
   redirected new growth to typed routers, tool modules, leaf stores, and `tests/` (Decision 3).
2. **Named a target shape** — `src/switchboard/` with `api/`, `mcp/tools/`, `application/`,
   `domain/`, `storage/` — as the place on-touch extractions should land (Decision 7).
3. **Upgraded that shape from compass to committed program** after operator review: the ratchet
   *holds* `store.py` at ~15k lines but never *shrinks* it; compass-only meant "maintainable"
   never arrives.

The operator scoped the staged rearchitecture as deliverable **`arch-ms-phase-0`** on the
Switchboard board: **Phase 0 — ADR-007 rails + platform scaffold**. Its end state:

> ADR-007 rails complete; `src/switchboard/` scaffold live; one REST+MCP pair uses
> `application/` commands; monolith ratchets stop growing.

**This ADR is the plan-of-record charter for that deliverable.** It does not reopen ADR-0006's
control-plane verdicts, ADR-0007's edge/Caddy decision, or ARCH-19's SQLite-vs-Postgres gate. It
commits to *how* the modular-monolith program runs and what Phase 0 must prove before later
service extraction.

**What "microservices modernization" means here:** not a big-bang split. The sequence is:

1. **Phase 0 (this ADR):** modular monolith rails — typed `application/` commands, thin REST/MCP
   adapters, `src/switchboard/` package skeleton, enforcement cuts from ADR-0007 still landing.
2. **Later phases (out of scope for Phase 0):** vertical-slice extractions into deployable
   bounded contexts (auth is slice 0 and largely shipped via ACCESS-16; tasks/deliverables
   coordination/tally follow the same strangler pattern documented in
   `AUTH-MICROSERVICE-DESIGN.md`).

ADR-0005 failed because it turned a correct destination into a **17-step horizontal schedule** on
the fleet's hottest file. ADR-0009 inherits the destination and explicitly rejects that failure
mode.

---

## Decision 1 — Phase 0 scope (in / out)

**In scope (mandatory before Phase 0 exit):**

| Track | Intent |
|---|---|
| **0.1 Enforcement** | ADR-0007 cuts CONSOL-6…9: pytest discovery gate (the size ratchet is retired 2026-07-12), dead-surface deletion, Caddy hardening + mission poll parity, H2 census for zero-call tools/flags. |
| **0.2 Scaffold** | `pyproject.toml` + Python 3.12 pin; `src/switchboard/` package skeleton; `create_task` / `get_task` / `update_task` as `application/` commands with REST+MCP wired through thin adapters; `test_arch_ms0_scaffold` CI gate; on-touch extractions (`api/routers/tasks.py`, `mcp/tools/*`, `runner_*` leaf store) as tasks complete. |
| **0.3 Security P0** | MCP read auth (bearer required on `/mcp`); `/health/deep` stops leaking project identifiers. |

**Already shipped on master (count toward Phase 0; tracker marks repo evidence):**

- Global auth cutover — ACCESS-16 / PR #300 (`PM_GLOBAL_AUTH` and legacy per-project login removed).
- Numbered, ledgered schema migrations — BUG-47 / PR #301.
- Reproducible builds — HARDEN-54 / PR #303 (`pyproject.toml`, Python 3.12, `uv.lock`).
- Readiness probe hygiene — BUG-48 / PR #299.
- Runtime least-privilege — HARDEN-55 / PR #302.

**Out of scope for Phase 0 (filed elsewhere; do not block Phase 0 exit on them):**

- Full `store.py` decomposition program (ADR-0005 remainder stays retired).
- Postgres migration (ARCH-19 gate).
- SSE/WebSockets, frontend framework, Nginx swap (ADR-0007 rejected).
- Operator UI surface mission (separate deliverable).
- Deploying physically separate microservice processes (Phase 0 proves seams in-process).

---

## Decision 2 — Guardrails (how the program stays safe)

1. **Vertical slices, not horizontal marches.** Each ARCH-MS task subtracts what it replaces in
   the same PR. No lane is assigned "build all of `src/switchboard/`."
2. **Green facade.** `store.py` remains the persistence facade until a repository extraction
   lands; callers move to `application/` first, storage second.
3. **Redirect, not a shared counter.** New feature code does not land in `store.py` / `app.py` /
   `mcp_server.py` — it lands in `src/switchboard/` (ADR-0007 Decision 3). In-place edits are
   allowed only for verbatim extractions and P0 security fixes. Enforced by ARCH-MS task
   boundaries + review, **not** by a global size counter. (The exact-match `test_size_ratchet.py`
   was retired 2026-07-12: a single integer every concurrent PR had to CAS against a moving
   `master` produced continuous merge wars — see ADR-0007 Decision 2 retirement banner. When the
   fleet runs parallel again, replace it with a *per-PR diff guard* — CI fails only if a PR's own
   diff adds net lines to a monolith without a `MONOLITH-TOUCH:` justification — which is
   commutative and never edits a shared line.)
4. **REST + MCP = one command.** New task mutations go through typed `application/commands/*`
   and `application/queries/*`; `api/routers/*` and `mcp/tools/*` are adapters only. This is
   the subtraction that pays for the layer (ADR-0007 Decision 7 invariant 1).
5. **SQL only in `storage/repositories/`.** New SQL does not land in `application/` or adapters.
   Existing `store.py` SQL moves on-touch (invariant 2).
6. **Board authority.** Task boundaries, dependencies, and Done provenance live on
   `project=switchboard` workstream ARCH-MS and deliverable `arch-ms-phase-0`. Repo docs follow
   the board; [`ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md) is the human-readable tracker.

---

## Decision 3 — Target shape (Phase 0 establishes the roots)

Phase 0 does not realize the full tree. It **creates and proves** the roots:

```
src/switchboard/
  settings.py             typed settings (Phase 0)
  api/routers/            tasks.py first; auth package lives under routers/auth (ARCH-MS-18)
  mcp/tools/              tasks.py, then board.py pattern (ARCH-MS-17, ARCH-MS-19)
  application/
    commands/             create_task, update_task, …
    queries/              get_task, …
    contracts/            Pydantic DTOs shared by REST + MCP
  domain/                 package stubs; extractions land on-touch
  storage/
    migrations/           numbered migrations (BUG-47 pattern extends here)
    repositories/         stubs; SQL moves on-touch from store.py
  integrations/           stubs
static/js/                ES-module split deferred to ARCH-MS-21
tests/                    new tests only (ARCH-MS-14); root tests attrition
```

**Phase 0 proof artifact:** `test_arch_ms0_scaffold` asserts the package imports, the
`create_task` command is callable, and both REST and MCP paths invoke the same application
handler (ARCH-MS-8, ARCH-MS-9).

---

## Decision 4 — Strangler trajectory (microservices after modular monolith)

Service extraction order (each its own PR, monolith keeps running):

| Slice | Bounded context | Phase 0 touchpoint | Notes |
|---|---|---|---|
| 0 | Auth / access | ARCH-MS-18, ARCH-MS-23 | ACCESS-16 cutover done; router migration + flag removal remain |
| 1 | Tasks | ARCH-MS-8, ARCH-MS-15–17 | First full REST+MCP → `application/` proof |
| 2 | Board / coordination | ARCH-MS-19+ | MCP tool module pattern |
| 3+ | Deliverables, tally, ingest | post–Phase 0 | On-touch; highest-change-first within slice |

Physical process split (separate Uvicorn/service units) is **not** Phase 0. The seam is
**package + API boundary** first; deploy boundary follows when a slice has tests, contracts, and
an operator-approved cutover plan.

---

## Decision 5 — Phase 0 exit criteria (ARCH-MS-24)

Phase 0 closes when **all** of the following hold (see tracker for per-task status):

1. **Enforcement:** CONSOL-6…9 tasks complete; pytest discovery gate active; dead surfaces from
   CONSOL-7 deleted; mission pollers at TTL+ETag parity; census executed.
2. **Scaffold:** `src/switchboard/` live; `create_task` + `get_task` + `update_task` unified;
   `test_arch_ms0_scaffold` green in CI; task REST/MCP adapters extracted.
3. **Security:** MCP reads require bearer; `/health/deep` does not leak project identifiers.
4. **Extraction proof (replaces the retired ratchet):** `store.py` is reduced by **≥500 lines via
   verbatim moves** (e.g. ARCH-MS-20 `runner_*` → `runner_store.py`), **or** the `store.py` facade
   is **≤14,000 lines** — measured, not asserted by a shared-counter test. Plus **no new monolith
   growth**: zero net feature lines added to `store.py` / `app.py` / `mcp_server.py` during Phase 0
   (operator diff audit, or the per-PR diff guard once the fleet parallelizes).
5. **Hygiene:** `tests/` directory + shim for new tests; `PM_*` unread flags deleted; numbered
   migrations path authoritative.

ARCH-MS-24 implements this as `scripts/arch_ms_phase0_exit_gate.py`: a fixed-baseline,
machine-readable audit that checks the three monoliths, AST-verifies verbatim repository moves,
and proves the application/CI/security/hygiene artifacts. The immutable `5305090` baseline avoids
the retired ratchet's shared-counter merge conflicts. Deliverable `arch-ms-phase-0` moves to
**done** only with board-recorded merge provenance on the canonical repo (Switchboard working
agreement).

---

## Execution — board tasks and tracker

All execution detail — milestones, dependencies, repo-verified status, and evidence links —
lives in **[`docs/ARCH-MS-EXECUTION.md`](../ARCH-MS-EXECUTION.md)**. That file is updated as
ARCH-MS tasks move; this ADR is the stable charter and does not duplicate the live task table.

**Milestones on deliverable `arch-ms-phase-0`:**

- **0.1 Enforcement** — ADR-0007 CONSOL cuts + flag census
- **0.2 Scaffold** — `src/switchboard/` + application commands
- **0.3 Security P0** — MCP read auth + readiness hygiene

**View on board:** `?project=switchboard&deliverable=arch-ms-phase-0#tab-mission`

---

## Consequences

- Phase 0 adds package structure and reduces `store.py` by more than 500 lines against the fixed
  baseline. The ARCH-MS-24 extraction-proof gate prevents "scaffold only" from counting as done.
- Task work may land through non-ARCH-MS IDs (HARDEN-*, BUG-*, ACCESS-*); the tracker records
  repo evidence so the mission graph stays honest.
- Agents and operators must read `ARCH-MS-EXECUTION.md` before claiming ARCH-MS-2+ work to avoid
  duplicating shipped cuts.
- Naming this ADR "microservices modernization" is intentional: Phase 0 is the mandatory
  foundation; skipping it recreates ADR-0005's conflict surface on `store.py`.

## Alternatives rejected

- **Big-bang microservices split.** Maximum blast radius; no green facade; rejected in
  `AUTH-MICROSERVICE-DESIGN.md` and ADR-0007.
- **Restart ADR-0005 horizontal decomposition.** Scheduled reorder on the hot file; dependency
  graph invalidated twice; remains retired.
- **Compass-only (no committed program).** Operator decision 2026-07-11 upgraded Decision 7;
  ratchet alone does not deliver maintainability.
- **Fold Phase 0 into ADR-0007.** ADR-0007 owns shell cleanup; this ADR owns the multi-phase
  modernization program and board deliverable boundaries.
- **Repo-local EPICS/docs as authority.** Switchboard project contract + ARCH-MS board tasks are
  canonical; repo docs are artifacts referenced by tasks.
