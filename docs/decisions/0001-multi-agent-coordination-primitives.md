# ADR-0001 — Multi-agent coordination: build the lease layer first

- **Status:** Proposed
- **Date:** 2026-06-27
- **Author:** NATIVE agent (Claude Code), Helm multi-agent session
- **Relates to:** [`docs/MULTI_AGENT_COORDINATION.md`](../MULTI_AGENT_COORDINATION.md)

---

## Context

The `taikun-plan` MCP server has been used in production by parallel Claude Code agents
for the first time during the Helm build (six concurrent agents, 2026-06-26/27). The board's
existing primitives — `update_task`, `add_comment`, `board_summary`, `get_plan_signals` —
handled single-agent operation well. Under multi-agent load, three categories of failure
emerged:

1. **Collision failures** — two agents editing the same file with no signal of the conflict
   until merge time. The `owns` list in `EPICS.md` is a *documentation* boundary, not an
   enforced one. An agent can read it and still unknowingly overlap with a live edit in
   another session.

2. **Stale board failures** — the board showed tasks "Not Started" that had already merged
   to `main`. Agents had to manually reconcile board state against `git log` before acting.
   The divergence between "board truth" and "code truth" was a recurrent overhead.

3. **Infra-contention failures** — TCP port `:10110`, the shared `/tmp/helm-opencpn` build
   clone, and open worktrees were all contentious shared resources with no registry. One
   agent's allocation silently broke another's test harness with misleading failure messages.

The existing roadmap (Phases 0–7 in `AGENT_ROADMAP.md`) does not address any of these — it
was designed for single-agent operation. The features in `MULTI_AGENT_COORDINATION.md` are
the proposed fix. This ADR decides the implementation order and architecture.

---

## Decision

**Build the lease layer first, then git↔board sync, then async signals.** Specifically:

### Phase MA-0 — File leases (the collision firewall)

Add a `file_leases` table and four MCP tools — `claim_files`, `release_files`, `check_files`,
`list_active_leases` — before any other coordination primitive. Rationale:

- Collisions are the most expensive failure (hand-reconciliation at merge time; sometimes
  lost work). They are also the easiest to prevent advisory-style with zero new infra.
- The lease model is simple enough to ship in a single PR: one table, four tools, one
  TTL-expiry cron. The more complex features (event subscriptions, decisions log) depend on
  this substrate being stable first.
- Agents can adopt it incrementally — `check_files` before an edit is a one-liner. The
  board doesn't have to enforce it; agents that use it benefit immediately.

The lease model is **advisory** (not hard). This is a deliberate choice: hard locks in a
distributed agent system introduce deadlock risk, require a lock-server, and can permanently
block an agent whose session was killed mid-task. Advisory leases degrade gracefully —
an agent that ignores them gets flagged, not blocked.

### Phase MA-1 — Board↔git auto-sync

Add a GitHub webhook handler that extracts task ids from branch names and commit messages
and advances task status on PR events. Rationale:

- The board-versus-code ambiguity hit every agent every session. It's a correctness problem
  (agents acted on stale status) not just a display problem.
- The implementation is small (webhook endpoint + regex + `update_task` call) relative to
  its impact — it eliminates an entire class of manual cross-reference.
- It is a prerequisite for trusting `board_summary` as a reliable source: once merged PRs
  auto-close their tasks, the board becomes the authoritative record the agents already
  assumed it was.

### Phase MA-2 — Directed IM + ack

Directed messages with read-receipts. Rationale: `add_comment` is fire-and-forget; the ack
primitive is the only way an agent can get a handshake ("I need to know this landed before
I write"). Ship after leases because leases eliminate most uncoordinated writes; IM handles
the cases where the agent needs affirmative confirmation before acting.

### Phase MA-3 — Decisions log + "main moved" push

Both are small (append-only table + RAG index for decisions; scheduler delta + notify for
push). Ship together as one PR.

### Phase MA-4+ — Resource broker, agent state field, event subscriptions

These are real wins but not emergency-class. The resource broker (§3.1) can be stubbed by
having agents `claim_files` on the `/tmp/helm-opencpn` path as a convention until a proper
`resource_type` enum is warranted.

---

## Alternatives considered

### A · Hard file locks (rejected)

A hard lock would block any write to a claimed file, not just signal. Rejected because:
- It requires a reliable lock-server (the existing SQLite store works but not across network
  partitions). If an agent's session dies mid-task, the lock is held until TTL — which may
  block legitimate work for up to 30 minutes.
- Agent sessions can be interrupted, context-window-killed, or simply forget to release.
  Advisory leases degrade gracefully in all these cases.
- 90% of the collision-prevention benefit comes from the signal, not the enforcement.

### B · Wiki / long-form docs instead of decisions log (rejected)

A freeform wiki creates sync debt — it goes stale without active curation. The append-only
decisions log is structured, attached to tasks, and indexed into the existing RAG corpus.
`ask_plan` can already answer questions from RAG-indexed docs; adding decisions to that corpus
means `ask_plan` answers "why is the collision.js guard `getStyle()` not `isStyleLoaded()`?"
without any new UI surface.

### C · Full chat channel (IM without ack) (rejected)

A broadcast chat channel is high-noise for agents. Agents don't have continuous context to
monitor a stream. The directed-message model (§2.1) is pull-on-demand from the
receiving agent's perspective, which matches how agents actually consume information.

### D · Ship all coordination features simultaneously (rejected)

The features interact (leases are a dependency for resource broker; git-sync is a dependency
for trustworthy status reads that IM handshakes rely on). Simultaneous shipping risks
integration bugs and makes the surface hard to evaluate. The phased order above gives each
primitive a stable foundation.

---

## Consequences

- The `file_leases` table is a new dependency for multi-agent workflows. Single-agent and
  human-only workflows are unaffected — the tools are additive, not replacing existing ones.
- The GitHub webhook requires one outbound webhook configured in the Helm repo settings
  (or equivalent). The fallback (scheduler poll of `git log`) works without it.
- The decisions log grows the RAG corpus over time. This is desirable — it makes `ask_plan`
  richer — but the incremental RAG index (`rag.add_document`, already in Phase 5) must be
  stable before MA-3 ships.
- Advisory leases may occasionally be violated (an agent ignores `check_files` or its session
  dies without `release_files`). This is acceptable. Stale leases expire on TTL; violated
  leases are flagged in the activity log. The fallback is the same manual reconciliation that
  exists today — leases make it rarer, not impossible.

---

## Open

- **Agent identity:** today the board uses a shared identity ("Maxwell (confirmed)"). The
  coordination layer needs per-agent identity to route directed messages and attribute leases.
  Minimum viable: `agent_id` is a string the agent sets itself (e.g. `"claude/native-session-4"`
  derived from the task it's working). Full OAuth agent identity is deferred (same as per-user
  auth in the human roadmap).
- **Lease TTL calibration:** 30 minutes is a guess. The right TTL is "how long does a typical
  agent task take between commits?" — may need tuning after first use.
- **Cross-project coordination:** the board is multi-project (`helm`, `maxwell`). The lease
  and messaging primitives should scope to a project (pass `project=` like all other tools)
  so Helm and Maxwell agents can't accidentally claim each other's files.
