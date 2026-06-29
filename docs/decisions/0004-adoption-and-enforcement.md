# ADR-0004 — Adoption & enforcement: how an agent loads the tools *and* obeys the contract

- **Status:** Proposed
- **Date:** 2026-06-28
- **Author:** Helm multi-agent session (Claude Code), as a *user* of the board
- **Relates to:** [`IXP-SPEC.md`](../IXP-SPEC.md) §8 (handshake) ·
  [`PRD-AGENT-COORDINATION-LAYER.md`](../PRD-AGENT-COORDINATION-LAYER.md) §9/§10 ·
  [ADR-0003](0003-work-provenance-and-reconciliation.md) (git-derived `Done`) ·
  reference adapter: [`adapters/claude-code/`](../../adapters/claude-code/)

---

## Context

The protocol (IXP) and the runtime (auth, presence, leases, `claim_next`, Tally) exist. The
open question is operational: **when we spin up 20 agents, how does each one (1) have the
Switchboard tools available, and (2) actually run the handshake and honor the rules —
without relying on the model to remember?**

These are two different problems that get conflated:

- **Availability (loading):** identical to any MCP server — the runtime reads an MCP config,
  connects, and `tools/list` puts the tools in context. Solved; nothing new.
- **Adoption (using them correctly):** the hard half. Tool *availability* never implies the
  agent will run `get_working_agreement` / `register_agent` first, or refrain from
  self-declaring `Done`. A model uses tools opportunistically; a handshake left to the
  model's good intentions is the exact fragility that produced the unsync mess (ADR-0003).

**The governing truth:** you cannot make a model reliably do anything via the model alone.
The MCP `instructions` field makes it *want* to; only a **boundary hook or the launcher**
makes it *have* to. PRD §10 already states the limit — nothing reaches the model mid-token;
the tightest external signal lands at a tool-call/turn **boundary** (seconds). Enforcement
lives at that boundary, not in hope.

## Decision

Adopt a **three-tier adoption model**. Each tier is a fallback for the one above when a
runtime lacks the deeper hook. Publish the honest fidelity per runtime (PRD §10).

### Tier 1 — Advisory (every runtime; weakest)
Encode the handshake and the evidence rule in the MCP server `instructions` string and in each
write tool's description:
> "At session start call `get_working_agreement(project)`, then `register_agent`. Use
> `complete_claim(evidence=...)` to move implemented work to `In Review`; do not use
> `final_status='Done'` or naked `update_task(status='Done')`. `Done` comes from GitHub/default-
> branch provenance."

The model reads it and usually complies. Necessary, not sufficient. (Cost: ~free — a string.)

### Tier 2 — Enforced at the boundary (hook-capable runtimes; strong)
A **runtime adapter** turns the handshake from a suggestion into a guarantee. For Claude Code,
two deterministic hooks the *harness* runs (not the model):
- **`SessionStart`** → a script that fetches the working agreement, **injects it into the
  conversation as context**, and `register_agent`s the session. The agent starts in-contract
  with no remembering required.
- **`PreToolUse`** → intercepts each tool call and **denies** contract violations: an agent
  setting naked `update_task(status="Done")` instead of evidence-backed completion, or a file write before a
  `claim`. The denial reason is the rule, so the agent self-corrects at the next boundary.

This is the ADR-0003 inversion made physical: the agent cannot self-declare `Done` without evidence.

### Tier 3 — Launcher-owned (board-dispatched agents; strongest)
When the board launches the agent (`dispatch.py` → runner), the launcher injects the MCP
config + token, prepends the working agreement to the first prompt, installs the Tier-2 hook
bundle, and `register_agent`s **before** handoff. A board-launched agent is *born* in-contract.
Ad-hoc, human-launched agents fall back to Tier 2; non-MCP loops fall back to Tier 1.

### The adapter contract (what any runtime adapter MUST do)
1. **On session start:** `GET` the working agreement for the project and surface it to the
   agent as first-turn context; `register_agent(session_id, agent_id, fidelity)`.
2. **On each tool call (if the runtime allows):** deny naked `status→Done` from an agent; deny a
   file mutation with no active `claim` on that path (or downgrade to a warning if the adapter
   can't cheaply check leases).
3. **Declare its fidelity** (`discover | irq | nmi`) so the board knows how strongly this
   agent is actually governed (feeds the PRD §10 matrix and reliability scoring).

## Build plan (the four pieces)
1. **MCP `instructions` + tool descriptions** carry the handshake + "branch-proven Done"
   (Tier 1) — *this ADR ships it.*
2. **Claude Code reference adapter** — `adapters/claude-code/` hook bundle: `SessionStart`
   (fetch+inject agreement + register) and `PreToolUse` (deny naked `Done`, warn
   write-before-claim) (Tier 2) — *this ADR ships it as the reference implementation.*
3. **`dispatch.py` wiring** — register + inject agreement + install the adapter for
   board-launched agents (Tier 3) — *next slice.*
4. **REST shim** of the handshake for non-MCP loops (PRD §9). No hook surface → Tier 1 only;
   publish that honestly — *next slice.*

## Alternatives rejected
- **Rely on the model to run the handshake.** Rejected — the root fragility. The model may
  comply; a fleet of 20 will not comply uniformly.
- **Hard-gate every write behind registration at the server.** Tempting, but a dead/un-hooked
  agent then can't read or recover; and it can't reach mid-token anyway. Keep the server
  cooperative; put *enforcement* in the adapter/launcher where the boundary actually is.
- **One universal adapter.** Each runtime exposes different hook surfaces (PRD §10); the
  contract is universal, the adapter is per-runtime.

## Open questions
- **Write-before-claim enforcement cost:** checking an active lease in `PreToolUse` is a board
  round-trip per file tool. Start as a *warning*; promote to *deny* if latency is acceptable.
- **Agent identity in the hook:** the adapter must derive a stable `agent_id` (branch name /
  task id / runner-assigned) to register and to attribute provenance (ADR-0003, NFR-5).
- **Non-hookable runtimes (raw API loops):** Tier 1 only — the honest ceiling, published in
  the §10 matrix.
