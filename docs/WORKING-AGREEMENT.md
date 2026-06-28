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

## Definition of Done
- Use `complete_claim(claim_id, evidence={...})` to release your claim and record what you verified.
- Omit `final_status` when the work needs review; the task moves to `In Review`.
- Pass `final_status="Done"` only when the task is truly complete under the project working
  agreement. Code tasks should include branch/head SHA/PR or merge SHA when available; planning and
  coordination tasks should include a concrete verification note.
- Do not use naked `update_task(status="Done")` as a checkbox flip. It lacks completion evidence and
  reconcile will flag it.

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
