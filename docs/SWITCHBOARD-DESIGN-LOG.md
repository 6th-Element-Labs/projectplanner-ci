# Switchboard — design log & decision trail

- **Status:** Living record
- **Date:** 2026-06-27
- **Purpose:** the reasoning *behind* the PRD and the IXP spec, so the team can follow how
  we got here — including the original code review (which otherwise lived only in chat) and
  every major decision with a pointer to where it's formalized.

> Formal artifacts: [`PRD-AGENT-COORDINATION-LAYER.md`](PRD-AGENT-COORDINATION-LAYER.md)
> (product + strategy) · [`IXP-SPEC.md`](IXP-SPEC.md) (the `IXP-core` wire contract, the IP)
> · [`P0-SPEC.md`](P0-SPEC.md) · [`RUNTIME-ADAPTERS-SPEC.md`](RUNTIME-ADAPTERS-SPEC.md)
> · [`INTERRUPT-TIERS-SPEC.md`](INTERRUPT-TIERS-SPEC.md) ·
> [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md) · [`TALLY-SPEC.md`](TALLY-SPEC.md).
> This file is the narrative; those are the source of truth.

---

## 0. Origin — code review of ProjectPlanner (the thing this grew out of)

The work started as a review of the existing ProjectPlanner app (FastAPI + SQLite + a bundled
LiteLLM gateway + an MCP server; per-task "Ask Taikun" agent; multi-agent coordination
primitives derived from a real six-agent Helm build). Verdict, recorded here because it was
never filed:

**The code is genuinely good.**
- Right-sized architecture (README's "why no workflow engine?" is the tell — deliberate
  restraint; two processes on a ~$6 VM). Borrows only the shared gateway; no core coupling.
- Consistent safety invariant: agent edits are **propose-then-confirm** everywhere; dispatched
  dev work goes to a `claude/<task>` branch + PR, never auto-merged.
- Fail-closed / fail-loud where it matters: unknown project → 400; a dependency edge to a
  non-existent task is *rejected*, not written as a dangling reference.
- The multi-agent layer is the real innovation and it's *earned* — every primitive (leases,
  directed IM, decisions log, delta polling) traces to an observed coordination failure.
- Sophisticated token-economics instinct (deterministic serialization for prompt-cache hits;
  delta-polling vs full board reads).
- Reads well — docstrings explain *why*; vanilla-JS frontend; single-responsibility modules.

**Real caveats (carried forward):**
- 🔴 **Auth gap** — writes are open by default (web task CRUD has no auth; MCP writes gate
  only if `PM_MCP_TOKEN` is set). On a public host this is a *today* risk. → now PRD §17 P0 /
  IXP-SPEC §12.
- Thin tests (one test file).
- **"Codex handoff" claim corrected:** there is *no* Codex integration in the code (grep =
  zero hits). What's true: the board is a shared MCP substrate any agent can read/write, and
  the push-side dispatch is **Claude Code only**. "Claude Code ↔ Claude Code, any MCP client
  can join" is accurate; "Claude Code → Codex" was aspirational.
- The **live app at `plan.taikunai.com` was never reachable** from the review environment
  (network policy blocked it, 403 at the proxy). All assessment is code-grounded, not
  UI-verified. *Still open: a live UI pass.*

Marketing read (since superseded/absorbed into the PRD): lead with the lived six-agent story
and the cost-per-outcome number; wedge = cost + oversight where Linear-likes and the SDK
makers are weakest; the moat is the protocol becoming a convention, not the code.

---

## 1. The decision trail (what we asked → what we concluded → where it lives)

| # | Question explored | Conclusion | Formalized in |
|---|---|---|---|
| 1 | Is the code good / how to market it? | Good, with an auth gap; market on lived story + cost-per-outcome | §0 above; PRD §13 |
| 2 | Can agents live-ping each other to stop/redirect across runtimes? | Yes, but only at **boundaries** — no mid-token interrupt. Bus exists (`send_agent_message`); delivery is per-runtime | PRD §8.5, §10; IXP §7 |
| 3 | Can we build an "IRQ" for an agent? | Yes — a real CPU IRQ is also checked at *instruction* boundaries; the agent's instruction boundary is the tool call. Full machinery maps (controller, ISR, context save/restore, maskable vs NMI) | PRD §7, §8.5 |
| 4 | Is the bus Ethernet / hub→switch / TIBCO? | Transport is *already switched* (DB serializes). Collisions live at the **resource** layer = CSMA/**CA** (reserve-then-act). Messaging = **TIBCO-style pub/sub**; finish that, don't build a switch | PRD §7, §12 |
| 5 | Rewrite in Rust/Go to be "hyperfast"? | No — Amdahl: the kernel is ~0.05% of the agent loop. Rewrite for **throughput/concurrency/fan-out**, not speed. Likely **NATS JetStream** under the hood; keep MCP as the swappable interface | PRD §12 |
| 6 | The real thesis? | **Orchestration across the LLM voids** — model-agnostic narrow waist via MCP + REST shim | PRD §1, §9 |
| 7 | Direct agents to lower cost-per-outcome? | Yes — don't moralize, **change the economics**: six levers (budget governor, model right-sizing, context economy, kill re-work, verified denominator, reliability-weighted dispatch). Vendors are incentivized toward tokenmax; only the neutral layer toward cost-per-outcome | PRD §20 |
| 8 | Make it a standard like MCP? Won't it die like SIP? | Not MCP's way (vertical, vendor-pulled) — **Kubernetes' way** (horizontal, buyer-pulled, neutral, single-player value day one). Cost is the adoption wedge; the invoice is the trojan horse | PRD §13 |
| 9 | Telco / 1B-msgs/day fit? | Agents **never** touch the firehose (Kafka/Flink stay). Agents = the judgment **tail**. Don't justify by volume (that's a scaling problem) | PRD §19 (operational, deferred) |
| 10 | Doesn't a workflow engine already do this? | Different machine: workflow **orchestrates execution**; Switchboard **coordinates** peers with judgment. Value only on investigative-AND-coordinated work | PRD §18, §19 |
| 11 | LangGraph already routes — what's different? | LangGraph routes *within one run*; Switchboard routes *work to workers across an open, durable, multi-vendor fleet* with cost-per-outcome. **LangGraph is a program; Switchboard is the scheduler programs run under** | PRD §18 |
| 12 | Generalize the parallel profile — where? | **Beachhead = high-intelligence knowledge work** (software, consulting, IB, sales/RFPs). Industrial/streaming is a later generalization, not the wedge | PRD §3, §19 |
| 13 | Naming | **Switchboard** (product) · **IXP — Instruction Exchange Protocol** (spec) · **Tally** (cost-per-outcome ledger, 正 motif, Tab÷Tally) | PRD §13 |
| 14 | Is the `_XP` layer-stack real IP or marketing? | The IP is the **wire semantics**, not names/analogies. Layering earns its keep **only as conformance profiles** (IXP-core → +TXP → +OXP) | PRD §13 checklist; IXP §11 |
| 15 | Is there a hierarchy AXP>TXP>IXP? | Not a `>` chain — it's an **hourglass**: IXP-core is the *waist* (the universal contract), TXP is an app above it, **OXP is a cross-cutting plane** (a projection over the activity log). My earlier four-layer table was too tidy (it wrongly put presence above signaling) | PRD §17 design note |

---

## 2. Reframes worth not losing

- **No mid-token interrupt.** The tightest external signal lands at a tool-call/turn boundary
  — for a working agent, seconds. Enough for stop/redirect/heads-up; not a freeze.
- **A tool call is the agent's syscall.** A `PreToolUse` hook is the kernel checking pending
  signals on syscall return — the agent harness is already shaped like a signal mechanism.
- **Narrow waist ≠ foundation.** It's the *convergence point* (the thin middle of the
  hourglass), like IP. IXP-core is the waist — that's why it's specced first and named canon.
- **Advisory over hard locks; prevent over resolve.** CSMA/CA reservation + `retry_after_seconds`,
  not a lock server. Cleanup costs 10× prevention.
- **Cost alignment is the defensible position.** Model vendors profit from wasted tokens; a
  neutral, buyer-aligned layer is the only party incentivized toward cost-per-outcome. No
  vendor can credibly make that claim → the cross-island neutral spot is the moat.
- **Volume ≠ coordination.** A big queue is a *scaling* problem (Kafka consumer groups +
  elastic workers). Coordination value appears only when parallel workers with judgment must
  not collide / must dedup / share decisions.
- **The rotting-decision-tree signal:** when you keep adding branches to a workflow engine to
  handle cases it can't really decide, the work has gone agentic — that tail is the
  Switchboard-shaped hole.
- **Tally = a projection over the log**, not new mutating ops (carry into the OXP spec).

---

## 3. Open items (full list lives in PRD §17)

- 🔴 **Auth gap — P0, today-risk.** Reference impl is *not* `IXP-core` conformant (writes open
  by default). Fix: per-agent bearer auth on every write surface; record identity as `actor`.
  Implementation floor: [`P0-SPEC.md`](P0-SPEC.md).
- 🟡 **`TXP` / `OXP` specs — drafted, implement after P0.** `claim_next` lives in
  [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md); Tally lives in [`TALLY-SPEC.md`](TALLY-SPEC.md).
- **Codex enforcement surface** — verify `PreToolUse`-style deny vs convention+kill.
- **Engine extraction timing** (Go/NATS) — trigger: first real multi-tenant write load.
- **Name/availability check** (trademark · domain · GitHub · npm) before the public spec drops.
- **Live UI pass** of `plan.taikunai.com` — never completed (network-blocked at review time).
- **Measure, don't estimate** the delta-poll token-savings number before publishing it.
