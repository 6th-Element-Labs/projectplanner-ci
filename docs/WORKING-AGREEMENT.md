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

## Fail and fix early
- Surface missing data, broken connections, invalid inputs, stale branches, failed checks, and
  missing permissions as soon as they appear. Do not hide them behind placeholder values, silent
  defaults, or optimistic status updates.
- If a failure is real and fixable inside your scope, fix it before moving on. If it is outside
  your scope, leave an auditable signal where the next actor will see it: a failing PR status,
  task comment, reconcile finding, monitor event, or explicit blocker.
- Fallbacks are allowed only when they are visible and named. A fallback must preserve the failing
  signal and explain what it replaced; it must not make the workflow look green.
- Testing is a discovery loop. When a gate uncovers an environment, ingestion, normalization,
  protocol, auth, or workflow problem, treat the discovered problem as part of the task until it is
  repaired or deliberately handed off.

## Git discipline
- **Push your branch before you claim progress.** Committed-but-unpushed work is invisible to
  the fleet and gets lost (it already did once).
- Open or update a PR for implemented work and include `branch`, `head_sha`, and `pr_url` /
  `pr_number` in `complete_claim` evidence.
- Branch naming: `<runtime>/<TASK-ID>-<slug>` (e.g. `codex/HARDEN-7-ci-gates` or
  `claude/ADAPTER-4-langgraph`).
- **Main writes via PR only** — never push `main` directly.
- We **squash-merge**, so `git branch --merged` / ancestry will *lie* about what's in `main`.
  Trust the board's `merged_sha`, not git ancestry.

## Safe merge protocol
- Merge only when your control registration, task instructions, or the human operator explicitly
  allow it.
- Fetch origin and rebase or merge your task branch onto the current intended target branch.
- Resolve conflicts intentionally. Never overwrite unrelated user or agent work.
- Rerun the relevant tests/checks after rebase or conflict resolution. For Switchboard core work,
  `scripts/switchboard_ci.sh` is the local deployment gate; GitHub Actions runs the same gate on
  PRs with strict dependency and frontend syntax checks.
- Push the updated branch and verify the PR points at the pushed head.
- Merge through GitHub or the configured merge queue only when review and required checks are green.
  For Switchboard PRs, the `Switchboard CI / Core conformance and smoke tests` check should be
  green before merge unless a human explicitly accepts the risk and records why.
- After merge, fetch/pull the target branch, verify the changed content is present, and record the
  resulting `merged_sha` or target branch head.
- Let the GitHub webhook or default-branch provenance path mark `Done`. If the webhook is down, run
  or request reconcile/backfill instead of setting `Done` manually.
- For non-PR/offline work, the agent still uses `complete_claim(...)` to move the task to
  `In Review`. A separate verifier/operator can then use the offline-evidence completion path to
  stamp `provenance_type=offline_evidence` with evidence, artifact/hash, verifier, and review time.
  This is the only non-code `Done` path; naked status edits to `Done` remain invalid.

## Coordination
- `claim`/`check` a file or resource before your first write to it; `release` when done.
- Use `claim_next(lane)` to get your next task atomically (don't hand-pick).
- Ports: see [`docs/PORTS.md`](PORTS.md). Never bind another service's canonical port.

## Data
- **Bring your own data** — charts/ENC/basemaps/weather are user-provided and never committed.
