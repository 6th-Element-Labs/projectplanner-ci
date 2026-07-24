# ADR-0019 — Repo constitution: layout truth distinct from repo topology

- **Status:** Accepted when REPO-6 merges.
- **Date:** 2026-07-24
- **Author:** REPO-6 birth-to-merge session (Cursor)
- **Relates to:** [ADR-0007](0007-application-shell-cleanup.md) (target tree / growth redirect) ·
  board task **REPO-6** · deliverable **`DELIV-repo-constitution`** · working-agreement
  `repo_topology` (Git remotes / Done authority — unchanged by this ADR)

> **One sentence:** every Switchboard project may carry a versioned
> `switchboard.repo_constitution.v1` document that freezes *where code and front doors live
> inside the checkout*; that document is **not** `repo_topology`, which only freezes *which
> remotes prove Done and CI*.

---

## Context

Agents already receive `repo_topology` from `get_working_agreement`: canonical vs public_ci
roles, default branches, merge provenance. That answers "which GitHub repo is code truth?"

It does **not** answer:

- where new product code must land (`src/switchboard/` vs repo root)
- where tests and docs entry points live
- whether root shims are allowed, and under what sunset policy
- which paths are frozen against net-new files

ADR-0007 Decision 3/7 already named the Switchboard target shape. Without a project-scoped
wire schema, every new project invents its own folklore, and agents re-derive layout from
stale docs. REPO-6 freezes the schema and one reference profile; runtime enforcement is a
later task.

## Decision

1. **New schema id:** `switchboard.repo_constitution.v1` (Pydantic source:
   `src/switchboard/contracts/projects/repo_constitution.py`; exported under `schemas/`).
2. **Required fields:** `profile_id`, `project_id`, `product_root`, `test_root`,
   `docs_front_door`, `agent_front_door`, `entrypoints`, `forbid_new`, `shim_policy`
   (`timed` | `none`), `required_files`, `archive_roots`, `enforcement_mode`
   (`off` | `warn` | `enforce`).
3. **Reference profile:** `python_modular_monolith` for project `switchboard`, fixture at
   `fixtures/repo_constitution.python_modular_monolith.v1.json`, aligned to ADR-0007
   Decision 7 (`src/switchboard/`, `tests/`, `docs/INDEX.md`, `AGENTS.md`, timed root shims).
4. **Separation:** `repo_topology` remains the only Done/CI remote authority.
   `repo_constitution` never names remotes, status contexts, or merge provenance.
5. **Enforcement:** this ADR ships schema + fixture only. `enforcement_mode` on the
   reference profile is `warn`. Turning `enforce` on is a follow-on task with a real gate.

## Consequences

- New projects can copy the fixture and change `project_id` / roots without inventing a
  second layout dialect.
- Agents should read constitution (when present) before creating root-level modules.
- Missing front-door files (`docs/INDEX.md`, `AGENTS.md`) are allowed while
  `enforcement_mode=warn`; they remain listed in `required_files` so the gap is visible.

## Alternatives considered

- **Fold into `repo_topology`.** Rejected: remotes/authority and checkout layout change at
  different rates and have different owners.
- **Docs-only convention.** Rejected: agents already ignore prose; a versioned `$id` is the
  same pattern as other Switchboard contracts.
- **Enforce immediately.** Rejected for P0: Switchboard itself still has root shims and
  missing front doors; warn-first matches the measure-twice charter of REPO-6.
