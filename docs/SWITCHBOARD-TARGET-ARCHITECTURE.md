# Switchboard — target module architecture (post-`store.py` decomposition)

- **Status:** Target (approved plan; not yet built)
- **Date:** 2026-07-07
- **Companion to:** [ADR-0005](decisions/0005-store-module-decomposition.md) (the decision) ·
  [`SWITCHBOARD-STORE-ENDSTATE.md`](SWITCHBOARD-STORE-ENDSTATE.md) (the finished tree + guardrails) ·
  [`SWITCHBOARD-STORE-DECOMPOSITION.md`](SWITCHBOARD-STORE-DECOMPOSITION.md) (every function → module)
- **Tracks as:** board workstream `ARCH` (ARCH-1 … ARCH-17)

This is the destination `store.py` is being strangled toward: **~37 modules in 6 layers**, one
import direction, nothing over ~900 lines. Function bodies move verbatim; this document says
*where each one lands* and *what may import what*.

---

## The one rule: imports flow downward

Layers are numbered 0 (foundation) → 5 (product surface). **A module may import from its own
layer or any layer below it, never above.** That single constraint is what turns a 16k-line
mud ball into a dependency graph you can reason about — and it is machine-checkable (ARCH-17).

```
        ┌─────────────────────────────────────────────────────────────┐
Layer 5 │  product surface  (deliverables, kpis, narration, inbox, …)  │
        ├─────────────────────────────────────────────────────────────┤
Layer 4 │  provenance & verify  (merge_gate, reconcile, evidence, CI)  │
        ├─────────────────────────────────────────────────────────────┤
Layer 3 │  execution & coordination  (work_sessions, preflight, …)     │
        ├─────────────────────────────────────────────────────────────┤
Layer 2 │  board & tasks  (tasks, claims, activity/audit, cleanup)     │
        ├─────────────────────────────────────────────────────────────┤
Layer 1 │  identity & tenancy  (auth, projects, repo_topology, boards) │
        ├─────────────────────────────────────────────────────────────┤
Layer 0 │  db foundation  (constants, db/core, db/schema)              │
        └─────────────────────────────────────────────────────────────┘
                    imports may only point DOWNWARD
```

Cross-layer *data* flow (a Layer-4 gate reads a Layer-2 task) happens by Layer 4 importing
Layer 2 — allowed, it's downward. What is forbidden is Layer 2 importing Layer 4 (the board
core reaching up into merge-gate logic), which is exactly the entanglement we have today.

**What does not change:** per-project physical DB isolation. `constants.py` still defines one
SQLite file per project (`maxwell`/`helm`/`switchboard`); `_resolve` in `projects_store` still
routes each request to its own file. The decomposition is a *code* reorganization on top of an
unchanged storage boundary.

---

## Layer 0 — DB foundation

Everything sits on these. No domain logic; pure plumbing.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `constants.py` | compiled regex, path/env vars, `BUILTIN_PROJECTS`, `BUILTIN_REPO_TOPOLOGIES`, seed config | — | ~300 | ARCH-2 |
| `db/core.py` | `_registry_conn`, sqlite busy/timeout, `_retry_on_locked`, JSON + coercion helpers, idempotency/hash primitives, `_insert_row`, `_table_columns` | 24 | ~300 | ARCH-3 |
| `db/schema.py` | `apply_schema` (all 47 `CREATE TABLE`s + additive migrations), `seed_from_plan`, `init_project_registry` — the extracted bodies of `init_db`/`seed_if_empty` | 3 | ~790 | ARCH-4 |
| `db/connection.py` | project→path resolution + sqlite connection factory: `_conn`, `_resolve`, `_project_map`, `_dynamic_projects` | 4 | ~30 | ARCH-5 |

> **Reality check (post-extraction).** The rows above reflect what actually shipped, which
> deviates from the original layer sketch in two spots the ADR anticipated:
> - **`db/connection.py` was pulled out early (ARCH-5).** *Every* domain module needs
>   `_conn`, and a module can't import it from the `store.py` façade without a cycle — so the
>   connection/resolution core (a clean, zero-upward-dep closure) had to come out before the
>   leaf/domain modules, front-loading the core of ARCH-15. It imports `db.core`/`db.schema`
>   (downward) and is imported by everything above.
> - **`db/schema.py` takes a connection, not a project.** `apply_schema(c)`/`seed_from_plan(c, …)`
>   stay Layer-0 pure; the project-aware `init_db`/`seed_if_empty` wrappers — and the
>   `_control_plane_conn`/`_control_plane_timeout_s`/`_control_plane_unavailable` factories —
>   remain in `store.py` until ARCH-15 (they orbit `_conn`/`_resolve`).

## Layer 1 — Identity & tenancy

Who is acting, which project, which repo, what they may touch.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `auth_store.py` | principals, passwords, sessions, scopes, role grants, project access, orgs/users, `resolve_write_actor` + unbound-identity risk (HARDEN-27), bootstrap owner | 40 | 953 | ARCH-15 |
| `repo_topology.py` | GitHub repo + role topology get/set/validate, repo-slug normalize, role guide | 16 | 426 | ARCH-15 |
| `projects_store.py` | registry resolution (`_resolve`, `_project_map`, `_dynamic_projects`), `create_project`, `normalize_project_id`, project context/hierarchy | 12 | 416 | ARCH-15 |
| `project_boards_store.py` | per-project boards CRUD | 5 | 97 | ARCH-15 |

## Layer 2 — Board & tasks

The core execution units and their audit trail. Highest importer count → migrated late.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `tasks_store.py` | task CRUD, move/archive, board payload + rollups, project/task tally + receipts, dependency resolution, `_task_row` + derived state | 28 | 876 | ARCH-13 |
| `claims_store.py` | `claim_task`, `claim_next`, `_claim_next_mission_scoped`, abandon/revoke/`complete_claim`, work-session claim gate | 7 | 710 | ARCH-13 |
| `cleanup.py` | cleanup candidates + apply, stale-state proof helpers | 5 | 375 | ARCH-13 |
| `activity_audit.py` | activity log, activity delta, `add_comment`, `_audit_*`, `audit_export` | 14 | 340 | ARCH-13 |
| `decisions_store.py` | decision log | 3 | 44 | ARCH-5 |

## Layer 3 — Execution & coordination

Agent runtime: sessions, repo hygiene, the tool boundary, dispatch, messaging, missions.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `work_sessions_store.py` | session CRUD + validation + claim binding | ~20 | ~700 | ARCH-10 |
| `managed_workspace.py` | `_managed_*` worktree/clone provisioning + archive (uses `git_ops`) | ~12 | ~450 | ARCH-10 |
| `session_health.py` | session/task health scoring, health lists | ~8 | ~350 | ARCH-10 |
| `session_policy.py` | session-policy profiles + defaults | ~5 | ~200 | ARCH-10 |
| `preflight.py` | `repo_preflight`, `preflight_work_session`, `_repo_*` scanners, file + resource leases | ~24 | ~500 | ARCH-9 |
| `git_ops.py` | shared raw git-shell helpers (the dedup of `_git_*` vs `_managed_*` git) | ~4 | ~120 | ARCH-9 |
| `pre_tool.py` | `pre_tool_check` (252) + `_pre_tool_*`, `control_plane_probe` + `_control_plane_*` | 11 | 463 | ARCH-11 |
| `agents_store.py` | agents/hosts/presence, host eligibility, dispatch scoring, `simulate_dispatch`, capability match | 23 | 459 | ARCH-11 |
| `runner_store.py` | runner sessions + runner control requests | 15 | 445 | ARCH-11 |
| `coordination_store.py` | wake intents, coordination monitors, unblock requests | 15 | 546 | ARCH-12 |
| `messaging_store.py` | agent messages, acks, ack monitors, protocol envelope/compat | 9 | 261 | ARCH-12 |
| `missions_store.py` | mission briefs/status, coordinator tick, mission narrative, blockers/next-actions | 9 | 545 | ARCH-12 |

## Layer 4 — Provenance & verification

The trust logic: is this work really merged / really tested / really published. This is the
highest-value, highest-conflict code and the concentration of `git`/GitHub coupling.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `merge_gate.py` | `merge_gate` (293) + `_merge_gate_*`, PR-evidence inference, terminal-done view | 23 | 676 | ARCH-6 |
| `reconcile.py` | `reconcile`, orphan-merge discovery, `mark_task_*` provenance writes, GitHub client helpers | 17 | 816 | ARCH-7 |
| `completion_evidence.py` | executed-test-run + completion-evidence gating, evidence hashing | 16 | 331 | ARCH-8 |
| `external_ci_store.py` | external CI mirror runs + task summary | 14 | 497 | ARCH-8 |
| `publication_store.py` | publication evidence + review gate | 10 | 326 | ARCH-8 |
| `side_effects_store.py` | exactly-once external side-effect ledger | 10 | 200 | ARCH-8 |
| `receipts_store.py` | coordination receipts | 2 | 17 | ARCH-5 |

## Layer 5 — Product surface

Everything that produces operator/stakeholder-facing value on top of the core.

| Module | Responsibility | ~fns | ~lines | Task |
|---|---|--:|--:|---|
| `deliverables_store.py` | deliverable CRUD, milestones, links, dependency graph, progress/tally, outcomes | ~22 | ~750 | ARCH-14 |
| `deliverable_breakdown_store.py` | breakdown-proposal state machine (propose/approve/reject/defer/validate) | ~11 | ~410 | ARCH-14 |
| `kpis_economics.py` | kpis, outcomes, spend/economics rollups, model recommendation, `report_usage` | 22 | 424 | ARCH-14 |
| `narration_store.py` | CEO-voice narration queue + state | 8 | 108 | ARCH-5 |
| `bug_intake.py` | bug intake + policy | 5 | 183 | ARCH-5 |
| `inbox_store.py` | live inbox / triage | 8 | 58 | ARCH-5 |
| `summaries_store.py` | task summaries | 4 | 56 | ARCH-5 |
| `chat_contacts.py` | plan chat + contacts | 5 | 44 | ARCH-5 |
| `jobs_store.py` | background jobs / DBOS eval | 5 | 44 | ARCH-5 |
| `digests_store.py` | activity digests | 4 | 25 | ARCH-5 |
| `rag_store.py` | RAG corpus | 5 | 30 | ARCH-5 |
| `meta_misc.py` | meta kv, working agreement, replay-verify, misc | 5 | 194 | ARCH-5/16 |

---

## Two modules get a second-level split

Round-one bucketing leaves two files still too large; each divides cleanly and is planned that
way in its task:

- **`work_sessions_store` (1,685)** → `work_sessions_store` + `managed_workspace` +
  `session_health` + `session_policy` (ARCH-10). The `_managed_*` git operations are also the
  dedup target for `git_ops` (ARCH-9).
- **`deliverables_store` (1,160)** → `deliverables_store` + `deliverable_breakdown_store`
  (ARCH-14). The breakdown-proposal state machine is a distinct concern from deliverable CRUD.

## Cross-cutting seams (decide explicitly, don't drift)

- `_claim_next_mission_scoped` — claims ↔ missions bridge; provisionally `claims_store`.
- `git_ops.py` — must be the *single* home for git-shell calls; `preflight` and
  `managed_workspace` both import it, neither re-copies.
- `_task_identity_state_in` — auth-shaped, completion-consumed; filed under
  `completion_evidence`.

## The layering contract (enforced by ARCH-17)

CI fails a PR when either holds:

1. **Size:** any module exceeds the line ceiling. Start the ratchet at the largest post-split
   module (~900) and tighten as extraction proceeds.
2. **Direction:** an import points upward (e.g. `tasks_store` → `merge_gate`, or anything →
   the `store.py` façade after cutover). Expressed as an import-graph/`import-linter` contract:
   `db < identity < board < execution < provenance < product`.

Test modules are exempt from the direction rule (a test may import any layers it exercises)
but not from a generous size ceiling.
