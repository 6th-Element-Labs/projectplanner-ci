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
- Agent completion moves the task to `In Review`, even if the implementation is finished and a PR
  is open.
- `Done` means branch truth: the work was merged, squash-merged, or rebased into the intended
  branch and Switchboard has recorded GitHub/default-branch provenance (`merged_sha` or equivalent).
- Do not pass `final_status="Done"` or use naked `update_task(status="Done")`. Hook-capable
  adapters deny it; the server rejects naked status flips and keeps bypassed claim-completion
  attempts in `In Review`.

## Git discipline
- **Push your branch before you claim progress.** Committed-but-unpushed work is invisible to
  the fleet and gets lost (it already did once).
- Open or update a PR for implemented work and include `branch`, `head_sha`, and `pr_url` /
  `pr_number` in `complete_claim` evidence.
- Branch naming: `claude/<TASK-ID>-<slug>` (e.g. `claude/ENGINE-15-basemap-boot`).
- **Main writes via PR only** — never push `main` directly.
- We **squash-merge**, so `git branch --merged` / ancestry will *lie* about what's in `main`.
  Trust the board's `merged_sha`, not git ancestry.

## Safe merge protocol
- Merge only when your control registration, task instructions, or the human operator explicitly
  allow it.
- Fetch origin and rebase or merge your task branch onto the current intended target branch.
- Resolve conflicts intentionally. Never overwrite unrelated user or agent work.
- Rerun the relevant tests/checks after rebase or conflict resolution.
- Push the updated branch and verify the PR points at the pushed head.
- Merge through GitHub or the configured merge queue only when checks/review are green.
- After merge, fetch/pull the target branch, verify the changed content is present, and record the
  resulting `merged_sha` or target branch head.
- Let the GitHub webhook or default-branch provenance path mark `Done`. If the webhook is down, run
  or request reconcile/backfill instead of setting `Done` manually.

## Coordination
- `claim`/`check` a file or resource before your first write to it; `release` when done.
- Use `claim_next(lane)` to get your next task atomically (don't hand-pick).
- Ports: see [`docs/PORTS.md`](PORTS.md). Never bind another service's canonical port.

## Data
- **Bring your own data** — charts/ENC/basemaps/weather are user-provided and never committed.
