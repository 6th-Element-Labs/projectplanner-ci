# Working agreement (the rules every agent gets at connect)

> This is the **fallback / template** copy. The live, per-project copy is served by
> `get_working_agreement(project)` (PRD §8.8 / [ADR-0003](decisions/0003-work-provenance-and-reconciliation.md))
> and injected automatically by the runtime adapter ([ADR-0004](decisions/0004-adoption-and-enforcement.md)).
> When the live endpoint is unavailable, the adapter injects this file verbatim.

You are one of many agents working a shared repo + board. Follow these rules for the whole
session so the fleet stays in sync.

## Session start (do this first)
1. `get_working_agreement(project)` — fetch the live rules; they override this file.
2. `register_agent(...)` — announce presence (the adapter does this for you when installed).
3. Drain your inbox (`list_unacked_messages` / `inbox`) and `ack` anything handled.

## Definition of Done (the one that bit us)
- You may move a task **only as far as `In Review`**, via `complete(task_id, agent_id,
  evidence={branch, head_sha, pr?})`.
- **You MUST NOT set a task to `Done`.** Only the merge webhook marks `Done` (it records the
  `merged_sha`). `Done` means *merged to `main`*, never "works on my machine."
- Bootstrap repair exception: a system-owned reconcile/backfill job may stamp legacy
  direct-to-default commits that already landed before this PR-only rule was enforced. Agents
  still must not use that path for normal work.

## Git discipline
- **Push your branch before you claim progress.** Committed-but-unpushed work is invisible to
  the fleet and gets lost (it already did once).
- Branch naming: `claude/<TASK-ID>-<slug>` (e.g. `claude/ENGINE-15-basemap-boot`).
- **Main writes via PR only** — never push `main` directly.
- We **squash-merge**, so `git branch --merged` / ancestry will *lie* about what's in `main`.
  Trust the board's `merged_sha`, not git ancestry.

## Coordination
- `claim`/`check` a file or resource before your first write to it; `release` when done.
- Use `claim_next(lane)` to get your next task atomically (don't hand-pick).
- Ports: see [`docs/PORTS.md`](PORTS.md). Never bind another service's canonical port.

## Data
- **Bring your own data** — charts/ENC/basemaps/weather are user-provided and never committed.
