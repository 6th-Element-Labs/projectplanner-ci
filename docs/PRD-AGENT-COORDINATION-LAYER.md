# PRD — Taikun Mesh: the model-agnostic agent coordination layer

- **Status:** Draft v0.1 (for review)
- **Date:** 2026-06-27
- **Author:** Steve (Taikun) · drafted from the multi-agent build sessions and the
  product-roadmap thesis
- **Codename:** *Taikun Mesh* (naming open — see §13)
- **One-line:** The narrow waist for agent coordination — any agent, behind any model,
  coordinates through one neutral substrate as long as it loads our tools first.

> Builds on what already ships: the coordination primitives in
> [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md), the MCP surface in
> `mcp_server.py`, the store in `store.py`, and the strategy in
> [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md). This PRD promotes that layer from "a
> coordination feature on a PM board" to "the product."

---

## 1. The thesis

You don't care which model is behind an agent. Claude Code, Codex, Cursor, a LangGraph
loop, a raw GPT-4 tool-calling loop — they are all just processes compiled by different
compilers (LLMs). What they lack is a **shared operating system**: a neutral place to
claim resources, message each other, hand off work, record decisions, and be supervised —
*across the LLM voids* between vendors and runtimes.

The mechanism that spans those voids already exists and is model-agnostic: **MCP** (plus a
thin REST / function-calling shim for agents that don't speak MCP). If every agent is
**loaded with our coordination toolset**, they coordinate **through us** regardless of the
model behind them.

**We become the narrow waist** — the TCP/IP of agent coordination; more precisely, the
**POSIX/syscall layer** any agent links against. We do not run the models. We do not own
the agents. We own the *contract they coordinate over*.

---

## 2. Problem

Teams now run **fleets** of heterogeneous agents against shared state (a repo, a plan, an
environment). Observed, first-hand, in the six-agent Helm session (2026-06-26/27):

| Pain | Root cause | Cost |
|---|---|---|
| Two agents edit one file at once | No cross-runtime lock signal | hand-reconciliation |
| Board says "Not Started"; it merged | No shared source of truth | re-verification every read |
| One agent can't tell another to stop/redirect | No cross-agent message bus | runaway, wasted tokens |
| Handoff Claude Code → Codex loses context | No durable, model-neutral state | re-derivation from zero |
| Port/build-dir contention across agents | No shared-resource broker | false failures, detective work |
| No one knows what a fleet cost to ship a feature | No cost-per-outcome accounting | budget blindness |

Every orchestration framework (LangGraph, CrewAI, AutoGen, the vendor SDKs) coordinates
agents **inside one run** — ephemeral, headless, single-vendor, dies with the process. No
durable substrate, no human window, no cross-runtime/cross-session coordination. That gap
**is the product.**

---

## 3. Vision & positioning

- **Category:** not "AI project management" (Linear/Asana win), not "agent orchestration
  framework" (dies with the process). The category is the **durable, model-agnostic
  coordination substrate** beneath whatever agents you run.
- **Shape:** a control plane (the contract + the bus + the supervisor) that any runtime
  links against, with a human window preserved on top.
- **The bet:** *coordination-first, model-agnostic, human-inspectable.* Everyone else
  builds from the wrong end (one vendor, one run, no human).
- **The moat (restated from the roadmap):** not the code — the **protocol/convention**
  agents speak, the **accumulated cross-session state**, the **two-sided habit**, and the
  **cost-economics framing**. Win protocol adoption → become the default.

---

## 4. Goals / Non-goals

### Goals
1. **One coordination contract** callable from any runtime via MCP and via REST.
2. **Cross-runtime coordination:** Claude Code ↔ Codex ↔ Cursor ↔ custom loops, sharing
   leases, messages, work queue, decisions, and state.
3. **Tiered control fidelity:** from advisory (poll) → enforced (hooks / per-tool-call) →
   guaranteed (process-level kill), honestly surfaced per runtime.
4. **Human oversight preserved:** peek in *and* step in (approval gates, audit trail).
5. **Cost-per-outcome accounting:** tokens/$ per task, per agent, per epic.
6. **Swappable engine:** semantics defined once; storage/transport replaceable (SQLite →
   Go/NATS/Postgres) behind an unchanged interface.

### Non-goals
- We are **not** building or hosting models, nor a new agent runtime.
- We are **not** an in-run orchestration DAG framework (LangGraph et al. stay; they become
  *clients* that coordinate through us when multiple runs share state).
- We do **not** route around the human as merge/approval authority.
- No full chat/IM stream, no wiki, no voting/consensus engine (see §15).

---

## 5. Users

| User | What they get | Primary surface |
|---|---|---|
| **Agent** (any runtime/model) | syscalls: claim, message, claim_next, ack, record, set_state, get_delta | MCP tools / REST |
| **Fleet operator** (human) | live presence, cost-per-outcome, approval gates, kill switch | Web UI + Slack/Gmail |
| **PM / lead** (human) | plan board, ask-the-plan agent, weekly digest | Web UI |
| **Integrator** | embeds the toolset into their agents; speaks the protocol | SDK + protocol spec |

The defining property: **two-sided.** Humans and agents look at the same durable state.

---

## 6. The model: control plane vs data plane (kernel vs userland)

- **Kernel (the hot core):** identity/presence, resource leases, the message bus
  (pub/sub + directed), the work queue (`claim_next`), the interrupt path, the
  append-only activity/decision log. Must be concurrent, durable, low tail-latency.
- **Userland (Python, fine to stay slow):** the web UI, the ask-the-plan ReAct agent,
  RAG, intake/triage, exports, OCR/rebrand. These call slow LLMs anyway.

**Honest latency note:** an agent action is dominated by inference (seconds). The kernel's
ops are sub-millisecond today. We optimize the kernel for **throughput, concurrency, and
fan-out tail latency at fleet scale — never for single-op "speed."** (See §11, §12.)

---

## 7. Core concepts — the "syscalls"

The protocol is a small, stable ABI. Everything else is built from these.

| Concept | Networking analog | Today | Target |
|---|---|---|---|
| **Agent identity + presence** | a switch's CAM table | `agent_state`, leases imply presence | first-class `register`/heartbeat + `list_active_agents` |
| **Resource lease** (file/port/build-dir/branch) | CSMA/CA reservation (RTS/CTS) | `claim_files`/`check_files`/`release_files` | + non-file resource types; TTL = KV TTL |
| **Directed message + ack** | EMS queue (certified delivery) | `send_agent_message`/`ack_message` | + `signal` (heads_up/redirect/stop) + priority |
| **Subjects / topics** | TIBCO RV subject + wildcards | per-task activity log (implicit) | explicit `lane.ENGINE.>` subject addressing |
| **Durable subscription** | durable consumer / cursor | `get_lane_delta(since_cursor)` | keep; generalize to any subject |
| **Work queue** (one-of-N) | TIBCO RVDQ / JetStream work-queue | — | `claim_next(lane)` atomic lease-on-claim |
| **Interrupt** | IRQ checked at instruction boundary | — | `signal` consumed at tool-call boundary (hook) |
| **Hard stop** | NMI (unmaskable) | — | runner process-kill (`/job/{id}/kill`) |
| **Decision record** | append-only ledger | `record_decision`/`list_decisions` | keep; index into RAG |
| **Working state** | saved registers (for IRET) | `set_agent_state`/`get_agent_state` | keep; the "stack" for resume-after-interrupt |
| **Cost record** | metering | — | per-task/agent/epic token+\$ ledger |

---

## 8. Functional requirements (the ABI)

> Every tool is exposed identically over **MCP** and **REST**. All writes are idempotent
> and carry `actor`/`agent_id` + timestamp into the append-only activity log. Responses use
> deterministic serialization (`sort_keys`, volatile fields stripped) for prompt-cache hits.

### 8.1 Identity & presence
- **FR-1** `register_agent(agent_id, runtime, model, lane?, task?)` → registers a live
  session with a heartbeat TTL.
- **FR-2** `heartbeat(agent_id)` → renews presence; expiry marks the agent stale.
- **FR-3** `list_active_agents(project?, lane?)` → the CAM table: who's live, on what, in
  which runtime (so a sender can discover a target).

### 8.2 Resource coordination (CSMA/CA)
- **FR-4** `claim(resource_type, name, agent_id, ttl, task?)` → lease or
  `{conflict, holder, retry_after_seconds}`. `resource_type ∈ {file, port, build_dir,
  worktree, binary, branch}`.
- **FR-5** `check(resource_type, names[])` → which are held, by whom, until when.
- **FR-6** `release(lease_id)` → idempotent release.
- **FR-7** Advisory by default; **merge-queue mode** (opt-in per resource) serializes
  access so there is no collision to detect (the "switched port" upgrade).

### 8.3 Messaging (pub/sub + directed)
- **FR-8** `publish(subject, payload, agent_id)` → append to the bus on a subject
  (`lane.ENGINE.status`, `task.CHART-8.comment`, `event.main.advanced`).
- **FR-9** `subscribe_delta(subject_pattern, since_cursor)` → durable catch-up; returns
  only what changed + a new cursor (0 tokens when nothing did). Wildcards (`*`, `>`).
- **FR-10** `send(to_agent, message, signal?, priority?, requires_ack?, ack_deadline?)` →
  directed delivery; `signal ∈ {heads_up, redirect, stop}`.
- **FR-11** `ack(message_id, response?)`, `inbox(agent_id, unacked?, signal?)`,
  `message_status(message_id)`.

### 8.4 Work dispatch (RVDQ)
- **FR-12** `claim_next(agent_id, lane)` → atomically returns the highest-priority task
  that is unblocked (deps satisfied), unclaimed, and in-lane, and leases it to the caller.
  Turns the board from a passive ledger into an active dispatcher.
- **FR-13** `complete(task_id, agent_id, evidence)` / `abandon(task_id, agent_id, reason)`
  → release the claim; record outcome for reliability scoring.

### 8.5 Interrupts (IRQ / NMI)
- **FR-14** A `stop`/`redirect` `signal` is consumed at the **next tool-call boundary** by
  a runtime adapter (e.g. Claude Code `PreToolUse` hook) which denies the pending tool with
  the message as the reason → the agent halts/redirects before acting.
- **FR-15** Before servicing an interrupt, the agent snapshots `set_agent_state` (context
  save); after, it restores and resumes (IRET). Heads-ups deliver at a `Stop`/turn boundary.
- **FR-16** **NMI:** an operator (or watchdog on budget/time/ignored-stops) triggers
  `kill(agent_id|job_id)` at the runner — the guaranteed, unmaskable stop.

### 8.6 Decisions, state, audit
- **FR-17** `record_decision(...)` (append-only, supersede-only), `list_decisions`,
  `get_decision`; indexed into RAG so `ask_plan` can cite them.
- **FR-18** `set_agent_state` / `get_agent_state` — per-agent working memory per task.
- **FR-19** Immutable, replayable **audit trail**: every state change carries who/what/why.

### 8.7 Oversight & cost
- **FR-20** **Approval gates:** an agent hits a `needs_human` decision → it pauses, the
  layer pings the human (Slack/Gmail), and resumes on approval. "Peek in *and* step in."
- **FR-21** **Cost-per-outcome ledger:** record tokens/$ per task, per agent, per epic;
  surface "this feature cost 340k tokens / \$4.20 across 3 agents." Per-task/epic budgets
  that warn or halt. (Two honest streams: gateway-metered vs agent-reported.)
- **FR-22** **Reliability scoring:** which agents complete vs abandon vs get reverted.

---

## 9. Interop surface — spanning the voids

| Surface | For | Notes |
|---|---|---|
| **MCP (Streamable HTTP)** | Claude Code, Cursor, Claude Desktop, Codex (MCP) | primary; already in `mcp_server.py` |
| **REST + JSON** | any non-MCP loop (LangGraph, custom GPT-4) | thin shim, same semantics, same audit log |
| **Runtime adapters** | per-runtime enforcement (hooks, SDK events) | optional, upgrades fidelity (§10) |
| **Notify channels** | humans | Slack + Gmail (already wired) |

**The interface is the product; the engine is swappable.** Agents always see the same
tools; underneath, storage/transport can move from SQLite to Go/NATS/Postgres with no
client change — the same pattern the README already uses for the LLM gateway.

---

## 10. Delivery / enforcement fidelity (honest matrix)

Control is **cooperative**, and fidelity scales with how deeply a runtime lets us hook.
We expose a uniform API but publish the real guarantees:

| Runtime | Discover/poll | Per-tool-call interrupt (IRQ) | Native turn-level redirect | Guaranteed stop (NMI) |
|---|---|---|---|---|
| Claude Code (CLI) | ✅ MCP | ✅ `PreToolUse` hook deny | — | ✅ via runner kill |
| Claude Agent SDK / cloud | ✅ MCP | ⚠️ at boundaries | ✅ `user.interrupt`+`user.message` | ✅ session stop |
| Codex | ✅ MCP | ⚠️ TBD (verify hook surface) | ⚠️ TBD | ✅ via runner kill |
| Cursor / others (MCP) | ✅ MCP | ⚠️ depends | — | ✅ if we launch it |
| Raw API loop (REST shim) | ✅ poll | only if integrator adds the check | integrator-defined | ✅ if we own the process |

**Fundamental limit (state it in the docs):** nothing reaches the model mid-token. The
tightest external signal lands at a **boundary** (tool call / turn). For a working agent
that's seconds — enough for stop/redirect/heads-up, not a freeze.

---

## 11. Non-functional requirements

- **NFR-1 Throughput:** sustain N concurrent agents × M tenants writing leases/messages/
  claims without write-lock starvation. (SQLite single-writer is the first ceiling.)
- **NFR-2 Tail latency under fan-out:** p99 for delta/inbox reads stays low while holding
  many long-lived subscribers.
- **NFR-3 Durability + idempotency:** at-least-once delivery; dedup on message-id and
  lease-claim so agent retries never corrupt state.
- **NFR-4 Determinism:** stable serialization for prompt-cache hits across sessions.
- **NFR-5 Security:** authn on **every** write surface (web + MCP + REST). *Current gap:
  writes are open by default — close this before any public/multi-tenant deploy.* Agent
  identity/auth (per-agent tokens), tenant isolation, RBAC.
- **NFR-6 Multi-tenancy:** workspace isolation; one tenant's bus never bleeds into another.
- **NFR-7 Observability:** the audit trail is queryable and replayable.

---

## 12. Architecture & engine choice

- **Now (validate semantics):** keep Python + FastAPI + SQLite (WAL). Cheapest place to
  iterate the *protocol*, which is what matters first. Add the new tools (presence,
  subjects, `claim_next`, signals, cost ledger) here.
- **At load (scale the kernel):** extract the hot core behind the unchanged MCP/REST
  interface. Strong candidate: **NATS JetStream (Go)** — it natively *is* the target model:

  | Our concept | JetStream primitive |
  |---|---|
  | subjects / lanes (`lane.ENGINE.>`) | subjects + wildcards |
  | `subscribe_delta(cursor)` | durable consumers |
  | `claim_next` / RVDQ | work-queue retention streams |
  | leases + presence (TTL) | KV buckets with per-key TTL |
  | certified delivery | ack'd delivery |

  Trade: NATS/Postgres = scale but ops weight; SQLite = one cheap box but a ceiling. The
  choice is "one team" vs "multi-tenant SaaS," not a language preference. **Don't write a
  message bus someone already wrote in Go.**

- **Rule:** rewrite the kernel for concurrency/scale only when load justifies it. Never
  rewrite for raw speed (Amdahl — it's ~0.05% of the agent loop).

---

## 13. The protocol as moat — adoption strategy

The durable advantage is the **convention**, so treat the protocol as a published artifact:

1. **Publish the spec** (the syscalls in §7–8, the session-start sequence, lease/ack
   semantics) under an open name — aim for it to become *the* way agents coordinate.
2. **Ship dead-simple onboarding:** a one-liner to load the toolset into each runtime
   (MCP config + `AGENTS.md`/`CLAUDE.md` session-start protocol + REST shim).
3. **Reference adapters** for Claude Code (hooks), the Agent SDK (events), and a generic
   REST loop — so "load us first" is minutes, not a project.
4. **Lead with the lived story** (the six-agent session) and the **cost-per-outcome**
   number — the two things competitors can't fake.
5. **Stay neutral Switzerland:** cross-vendor by design. No single platform will build the
   cross-vendor layer well, because each wants lock-in. That's the opening.

> Naming: *Mesh* (fabric/pub-sub), *Switchboard* (directed), *Conductor* (orchestration),
> or keep it boring and protocol-forward (e.g. "ACP — Agent Coordination Protocol").
> Decide before the public spec drops.

---

## 14. Metrics / KPIs

- **Adoption:** # agents registered/week; # distinct runtimes; # tenants; protocol
  re-use (agents that complete the session-start sequence).
- **Coordination value:** collisions prevented (lease conflicts caught); stale-status
  reads eliminated; stop/redirects honored before damage.
- **Token economics:** tokens saved by delta-polling vs full reads (*measure it — don't
  ship the estimate*); cost-per-outcome per epic.
- **Oversight:** approval-gate response time; runaway stops; revert rate by agent.
- **Reliability:** task complete vs abandon vs revert per agent.

---

## 15. What we are NOT building (scope guard)

- A model host or a new agent runtime.
- An in-run DAG/orchestration framework (those become clients).
- A full chat stream, a wiki, or a voting/consensus engine.
- Hard distributed locks / a lock server (advisory + merge-queue cover it).
- Anything that routes around the human as merge/approval authority.

---

## 16. Phased rollout

| Phase | Ships | Why first |
|---|---|---|
| **P0 — Close the floor** | authn on all writes; deterministic serialization (done); REST shim parity with MCP | can't be a public substrate while writes are open |
| **P1 — Presence + dispatch** | `register_agent`/heartbeat/`list_active_agents`; `claim_next` (RVDQ) | makes it an *active* coordinator; finishes the "switch" |
| **P2 — Interrupts** | `signal` field; Claude Code hook bundle (IRQ); runner `kill` (NMI); state save/resume | the live stop/redirect the operator wants |
| **P3 — Oversight + cost** | approval gates; cost-per-outcome ledger; reliability scoring | the commercial wedge for serious/regulated buyers |
| **P4 — Subjects + scale** | explicit subject addressing + wildcards; kernel extraction (Go/NATS) behind the interface | scale + the pub/sub north star |
| **P5 — Protocol + ecosystem** | published spec; reference adapters; multi-tenant workspaces, RBAC, self-serve | turn the convention into the moat |

---

## 17. Risks & open questions

1. **Platform encroachment:** Anthropic/OpenAI/Linear ship native coordination. *Mitigation:*
   be narrowly excellent at the coordination + oversight + cost triangle and be the
   cross-vendor neutral party; publish the protocol.
2. **Cooperative-only ceiling:** we can't orchestrate an agent that won't call the tools.
   *Mitigation:* fidelity tiers + the NMI kill floor; make "load us first" frictionless.
3. **Codex enforcement surface unknown** — verify whether Codex supports a `PreToolUse`-style
   deny or only convention + kill. *Open.*
4. **Engine timing:** when exactly to extract the Go/NATS kernel — premature = wasted
   runway; late = scaling pain. *Trigger:* first real multi-tenant write load.
5. **Cost-attribution honesty:** gateway-metered vs agent-reported tokens diverge. *Open —
   see ADR-0002.*
6. **Standalone product vs strategic acquihire bait** — depends on how fast platforms move.
   The wedge is real today either way.

---

## 18. See also

- [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md) — positioning, competitive read, the three bets
- [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md) — the primitives, derived from session data
- [`MCP.md`](MCP.md) — MCP server design and tool reference
- [`AGENT_OPERATOR_FEATURES.md`](AGENT_OPERATOR_FEATURES.md) — single-agent operator layer
- `decisions/0001-…` — ADR on coordination-primitive build order
