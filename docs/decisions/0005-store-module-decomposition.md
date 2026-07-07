# ADR-0005 — Decompose `store.py`: strangler-split the god module into layered stores

- **Status:** Proposed
- **Date:** 2026-07-07
- **Author:** Switchboard architecture session (Claude Code / Opus), at the operator's request
- **Relates to:** [`SWITCHBOARD-TARGET-ARCHITECTURE.md`](../SWITCHBOARD-TARGET-ARCHITECTURE.md)
  (the target module map) · [`SWITCHBOARD-STORE-ENDSTATE.md`](../SWITCHBOARD-STORE-ENDSTATE.md)
  (what the tree looks like when this is done) ·
  [`SWITCHBOARD-STORE-DECOMPOSITION.md`](../SWITCHBOARD-STORE-DECOMPOSITION.md)
  (the full per-function map) · board workstream **`ARCH`** (ARCH-1 … ARCH-17) ·
  [ADR-0003](0003-work-provenance-and-reconciliation.md) (the merge-provenance logic that lives inside the module we're splitting)

---

## Context

`store.py` is **15,817 lines** — **513 top-level functions, zero classes**, and roughly
**32% of the entire Python codebase** (48.7k lines). It is imported by **~84 files**. It is
the single point every feature passes through.

It began (its own docstring still says so) as *"SQLite store … tasks + activity."* It is no
longer that. It has silently accreted three layers that should be separate:

| Layer | What it should be | What `store.py` actually holds |
|---|---|---|
| **Schema / migrations** | its own concern | `init_db` alone is **673 lines**; **48 `CREATE TABLE`s** |
| **Data access (DAL)** | thin CRUD | ~90 `get_*` / `list_*` / `create_*` functions |
| **Business logic** | domain modules | `merge_gate` (293), `pre_tool_check` (252), `repo_preflight` (190), `reconcile`, `cleanup`, completion-evidence gating |

The business-logic tell is decisive: **88 lines inside `store.py` call `subprocess` / `urllib`
/ GitHub**. A data-access layer does not shell out to `git` or hit the GitHub API — but this
one does, because merge-gating, reconciliation, and repo preflight were written *in the same
file as the SQL*. It is not a fat-but-boring persistence file we can ignore; it is where the
product's trust decisions are made, tangled into the schema. It is also **three products in
one file** — `maxwell`, `helm`, and `switchboard` all persist through it.

**Why this hurts, concretely (not aesthetics):**

- **Merge-conflict magnet.** A fleet of concurrent agents touches this one file on nearly
  every task; it routinely lands in conflict (it arrived at this very session in an unmerged
  `UU` state). Every lane conflicts on `store.py`.
- **No test isolation.** You cannot exercise `merge_gate` without dragging in the whole
  schema and forty unrelated subsystems.
- **Unbounded blast radius.** A change to one helper can reach 84 importers; no one holds the
  file in their head.
- **It defeats our own agents.** A 16k-line file overflows context budgets, so agents (and
  reviewers) grep-and-peek instead of reading — the exact conditions in which regressions slip
  through the fail-fix net.

**What is already right (and constrains the fix):**

- **The seams are clean.** Functions are consistently prefixed by domain (`claim_*`,
  `deliverable_*`, `runner_*`, `mission_*`, `external_ci_*`, `_merge_gate_*`). This is stacked
  lasagna, not spaghetti — it splits along dotted lines.
- **Physical DB isolation stays.** One SQLite file per project (ADR-era decision) is a genuine
  strength: a Helm request cannot read Maxwell's rows because there is no shared table. The
  decomposition changes *code* organization only; it does **not** touch the per-project-file
  boundary.

## Decision

**Strangler-fig the module into ~37 domain modules across 6 layers, behind a re-export
facade, enforced by a CI layering + size guard. This is a reorganization, not a rewrite —
function bodies move verbatim.**

Four commitments:

1. **Layered modules, one direction.** Group the 513 functions into 6 layers; imports flow
   strictly downward. Lower layers never import higher ones. (Full map in the target-arch doc.)

   ```
   Layer 0  db foundation      constants.py, db/core.py, db/schema.py
   Layer 1  identity & tenancy  auth_store, projects_store, repo_topology, project_boards_store
   Layer 2  board & tasks       tasks_store, claims_store, activity_audit, cleanup, decisions
   Layer 3  execution & coord   work_sessions(+managed_workspace,health,policy), preflight+git_ops,
                                pre_tool, agents_store, runner_store, coordination_store,
                                messaging_store, missions_store
   Layer 4  provenance & verify merge_gate, reconcile, completion_evidence, external_ci_store,
                                publication_store, side_effects_store, receipts_store
   Layer 5  product surface     deliverables_store(+breakdown), kpis_economics, narration_store,
                                inbox_store, summaries_store, digests_store, chat_contacts,
                                rag_store, jobs_store, bug_intake, meta_misc
   ```

2. **Facade during migration.** `store.py` keeps `from <module> import *` re-exports so all
   ~84 importers keep working while modules move out one at a time. The monolith is retired
   only in the final cutover (ARCH-16), importer by importer — never big-bang.

3. **Bodies move verbatim; behavior is invariant.** Each extraction is a pure move. The gate
   is the existing test suite (`test_merge_gate`, `test_deliverables_model`,
   `test_access_auth_sessions`, …) going green unchanged, plus schema-hash equality for the
   DDL move. No logic is "improved" in the same PR that moves it.

4. **A ratchet so it can't regrow.** CI (ARCH-17) fails any module over a line ceiling
   (ratchet down from the largest post-split module) and any upward import. Without the
   ratchet, the next 18 months rebuild the monolith.

**Sequencing** (why this order): lowest-risk / highest-relief first, then highest-conflict
business logic, then the high-importer core last.

1. `constants.py` → `db/core.py` → `db/schema.py` (mechanical; carves ~1.1k lines; unblocks all)
2. leaf stores (no back-references; proves the shim+test pattern)
3. **provenance cluster** (`merge_gate`, `reconcile`, completion/CI/publication/side-effects) —
   the highest-conflict, highest-value logic and where the git/GitHub coupling concentrates
4. execution cluster (`preflight`+`git_ops`, then the `work_sessions` split, pre_tool/agents/runner, coord/messaging/missions)
5. board core + product + identity (most importers) last
6. cutover (retire the facade) + CI guard

This maps 1:1 to board tasks **ARCH-2 … ARCH-17**; ARCH-1 is this documentation set.

## Alternatives rejected

- **Do nothing / "it still works."** Rejected. It works *today* but the cost is paid every
  sprint in merge conflicts, un-runnable unit tests, and agent context thrash. The trend line
  is 16k → 20k. The pain is already here, not hypothetical.
- **Big-bang rewrite into a new package.** Rejected — this is the module the whole product
  depends on; a from-scratch rewrite is a multi-week freeze with a giant untestable diff and
  no safe rollback. The strangler keeps the suite green at every step.
- **Mechanical split by line count** (store_1.py, store_2.py …). Rejected — it fixes the byte
  count and nothing else: no isolation, no layering, no testability, arbitrary seams that make
  future conflicts *worse*.
- **One class-based God object** (`class Store:` with 513 methods). Rejected — same coupling,
  now with `self` threading state through everything; moves the monolith, doesn't dissolve it.
- **Split the database too** (a DB per module). Rejected and explicitly out of scope — the
  per-*project* file boundary is the isolation that matters and it stays. Per-*module* DBs
  would break cross-domain transactions (a claim that writes activity + provenance in one txn)
  for no benefit.
- **Extract to microservices.** Rejected — this is a two-process, single-VM system by
  deliberate design (see the design log's "why no workflow engine?"). Network boundaries where
  a function call belongs would be a strategic error. Modules, not services.

## Open questions (carried into the ARCH tasks)

- **Cross-cutting `_claim_next_mission_scoped` (256 lines).** Bridges `claims_store` and
  `missions_store`; it is the single knottiest seam. Provisionally lands in `claims_store`
  (ARCH-13) importing mission helpers — confirm the direction there.
- **Duplicate git helpers.** `work_sessions`' `_managed_*` git operations overlap
  `preflight`'s `_git_*`/`_repo_*`. ARCH-9 introduces a shared `git_ops.py`; ARCH-10 must
  consume it rather than re-copy.
- **`_task_identity_state_in`.** Reads as auth but is consumed by completion gating — filed
  under `completion_evidence` (ARCH-8), revisit if the auth tests pull it back.
- **Does `store.py` survive?** Either a thin, *curated* public-API façade (a deliberate,
  documented aggregator — not a dumping ground) or deleted outright. Decided in the end-state
  doc during ARCH-16, based on how many callers genuinely want an aggregate import.
- **Test-suite coupling.** Many tests `import store` directly. They migrate with the cutover;
  the CI guard must not fire on test files importing multiple domain modules.
