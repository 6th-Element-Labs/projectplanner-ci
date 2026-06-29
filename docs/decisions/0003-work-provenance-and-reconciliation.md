# ADR-0003 — Work provenance & reconciliation: make the board the ground truth for *where work is*

- **Status:** Proposed
- **Date:** 2026-06-28
- **Author:** Helm multi-agent session (Claude Code), as a *user* of the board
- **Relates to:** [`MULTI_AGENT_COORDINATION.md`](../MULTI_AGENT_COORDINATION.md) §1.2/§1.3/§3.1 ·
  [`PRD-AGENT-COORDINATION-LAYER.md`](../PRD-AGENT-COORDINATION-LAYER.md) §8.8 ·
  [`IXP-SPEC.md`](../IXP-SPEC.md) §8 (handshake) · [`PRODUCT_ROADMAP.md`](../PRODUCT_ROADMAP.md) #10

---

## Context

The existing coordination layer handles agents talking **to each other** (leases, directed
IM, decisions, delta polling). It does **not** make them agree on **where the work actually
is** in the git lifecycle. Across the Helm fleet (20+ parallel agents in their own worktrees)
this produced a multi-day "local/remote unsync" mess that required a five-agent forensic
reconstruction to untangle before a fresh install could be trusted. Three failures compounded:

1. **Status was self-reported and drifted from git truth.** A task read `Done` on the board
   because *an agent decided it was done* — meaning "code exists in my worktree," not "merged
   to `main`." The inverse of the §1.2 pain ("board says Not Started; it merged") bit harder:
   **board says `Done`; the code was never pushed.** Four branches held real, committed
   feature work (OWNSHIP follow-mode, AIS symbology, SHELL seam, ROUTE-3 editor) that existed
   **only on local disk** — never pushed anywhere — while their tasks read `Done`.

2. **Squash-merge makes git ancestry lie.** `git branch --merged` / `git log main..branch`
   reported **89 branches "unmerged"** when their content was fully in `main` (squash-merge
   gives the merged work a *new* SHA, so the original branch is no longer an ancestor). This
   produced a **false "we're missing tons of features" alarm.** Any reconciliation that trusts
   ancestry instead of content will mislead on every squash-merge.

3. **"Done" is one word for five states** — committed? pushed? PR-merged? present in `main`'s
   actual content? published to the public mirror? Nobody could answer *per task* without
   manual `git` archaeology.

The board is already the hub (`plan.taikunai.com`). The fix is to make it the **authoritative
source of truth for work-state**, not just task status — and to hand every agent the same
rules at connect so 20 agents stop each inventing their own flow.

## Decision

Build a small, sharp **work-provenance + reconciliation layer**, in three parts. The
governing principle:

> **Git events are the source of truth for work state. Agents *propose*; the board + merge
> webhook *decide*; `reconcile` *catches drift* — and everyone gets the same rules at connect.**

### Part 1 — Connect-time *working agreement* (`get_working_agreement`)

The IXP handshake (§8) tells an agent *what to do* (register, drain inbox, claim). It does not
tell it *the rules of this repo*. Add **`get_working_agreement(project)`** as **step 0** of the
handshake — plan hands every agent, regardless of model/runtime, the same canonical policy:

```json
{
  "project": "helm",
  "canonical_main_sha": "10949ed…",          // current origin/main HEAD (kept live by the webhook)
  "branch_convention": "claude/<TASK-ID>-<slug>",
  "definition_of_done": "Done means merged/rebased into the intended branch with recorded GitHub/default-branch provenance",
  "done_policy": {"mode": "git_merge_verified", "agent_may_set_done": false, "requires_merge_provenance": true},
  "push_before_claiming_progress": true,
  "merge_strategy": "squash",                  // => trust board.merged_sha, NOT git ancestry/--merged
  "main_writes": "PR only — never push main directly",
  "ports_doc": "docs/PORTS.md",
  "byo_data": true,                            // charts/ENC/basemaps/weather are user-provided
  "session_start_sequence": ["register_agent", "inbox(unacked)", "get_working_agreement",
                             "check+claim before first write"]
}
```

Stored as per-project board config (`meta`) plus computed fields (`canonical_main_sha` from the
webhook). One source of truth, fetched on connect.

### Part 2 — Branch-proven `Done` (updated)

Stop letting agents self-declare `Done` via naked status flips. The lifecycle becomes:

```
Not Started ─claim_next/start→ In Progress ─complete(evidence)→ In Review
                                      └─PR merged (webhook/reconcile)→ Done ─published→ Released
```

- Agents `claim_next` → work → **`complete(task_id, agent_id, evidence)`** where evidence is the
  branch + head_sha (+ PR if opened) or another concrete verification note. This always moves the
  task to **`In Review`** and releases the claim. If an agent asks for `final_status="Done"`, the
  server records the attempt but keeps the task in review.
- GitHub webhooks and reconcile/default-branch jobs move code tasks to `Done`, stamping
  `merged_sha`.
- Optional **`Released`** when the merge SHA's content reaches the public mirror.

This rule kills failure #1: a task cannot read `Done` from a naked checkbox or agent optimism. It
needs GitHub/default-branch proof.

### Part 3 — Per-task git-lifecycle field + `reconcile` drift detector

Each task carries a git-lifecycle block (new `task_git_state` table, surfaced on `get_task`
and `get_lane_delta`):

```
task_id, branch, head_sha, pushed_at, pr_number, pr_url,
merged_sha, merged_at, in_main_content (bool), published_ref, last_reconciled_at
```

A scheduled **`reconcile(project)`** (the scheduler already exists — the summarize timer) plus
an on-demand MCP/REST tool runs **content-based, not ancestry-based** checks and flags:

| Check | How | The session bug it catches |
|---|---|---|
| Unpushed commits | task's reported `head_sha` not present on `origin` (GitHub API) | the 4 local-only feature branches |
| Uncommitted / dirty worktree | agent heartbeat reports a `dirty` flag; or proxy: `In Progress` with no pushed branch after N h | dirty worktrees holding real work |
| `Done` with no `merged_sha` | board query | self-declared "done" that never merged |
| Merge SHA not in `main` content | `merged_sha` reachable from `origin/main` **and** not subsequently reverted (key-file spot-check) — **never** `git branch --merged` | squash + revert blindness |
| private `main` ↔ public mirror drift | compare `origin/main` (minus export-ignored) against the public mirror's last published ref | the public-mirror staleness/race |

`reconcile` turns the five-agent forensic dig into a five-second dashboard check. Its report is
the "how do we avoid this again" surface; a non-empty report is a release blocker.

### New surface (MCP + REST, idempotent, into the activity log)

- `get_working_agreement(project)` — the connect-time policy (Part 1).
- `complete(task_id, agent_id, evidence)` / `abandon(task_id, agent_id, reason)` — Part 2 /
  PRD FR-13; records provenance, moves to `In Review`.
- `reconcile(project)` — Part 3; returns the drift report; also scheduled.
- `get_task` / `get_lane_delta` gain the git-lifecycle block.
- Webhook extension: `pull_request` open → `In Review`; merge → `Done` + `merged_sha`;
  push to `main` → refresh `canonical_main_sha`.

## Alternatives rejected

- **Trust git ancestry for "merged."** Rejected — squash-merge breaks it (the false
  89-branch alarm). All "is it in main" checks are content-/SHA-reachability-based.
- **Keep agent-set `Done`.** Rejected — self-reported status drifting from git is the root
  cause. Git events decide `Done`.
- **More chat/coordination primitives.** Not the gap — agent-to-agent messaging is already
  covered. The missing layer is work *provenance* + *reconciliation*.
- **A separate dashboard app.** Rejected — reconcile is data the board already can hold; the
  board stays the single pane.

## Open questions

- **Dirty-worktree visibility:** the server can't see uncommitted local work directly. Needs a
  client-side hook/agent to report a `dirty` flag on heartbeat, or we accept the
  `In Progress`-without-pushed-branch proxy. (Ties to IXP presence/heartbeat.)
- **Where reconcile runs:** server-side via the GitHub API (no local clone), or a runner with
  repo access for deeper content checks. Start with the API path.
- **Multi-repo / monorepo tasks** spanning more than one PR — `merged_sha` becomes a set.
- **Agent identity/auth** (shared with ADR-0001, NFR-5): provenance is only as trustworthy as
  the `agent_id` reporting it.
