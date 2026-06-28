# PRD — Switchboard: the model-agnostic agent coordination layer

- **Status:** Draft v0.1 (for review)
- **Date:** 2026-06-27
- **Author:** Steve (Taikun) · drafted from the multi-agent build sessions and the
  product-roadmap thesis
- **Product:** **Switchboard** (chosen 2026-06-27 — see §13)
- **Protocol:** **IXP — Instruction Exchange Protocol** (the open spec; `_XP` family —
  A/T/I/O = Agent/Task/Instruction/Outcome; IXP canonical)
- **Ledger:** **Tally** — the cost-per-outcome accounting feature (motif: 正)
- **One-line:** The narrow waist for agent coordination — any agent, behind any model,
  coordinates through one neutral substrate as long as it loads our tools first.
  *Switchboard speaks IXP and keeps a Tally.*

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

**Beachhead ICP — high-intelligence knowledge work (deliberate, for now).** The initial
target is *knowledge work*: software, consulting, investment-banking deliverables (pitch
decks, models, memos), sales proposals/RFPs, and other large orchestrated high-intelligence
motions. This work is **natively agentic** (judgment, not rule-execution), **decomposable
and parallelizable** (one big deliverable → many sub-pieces worked at once), and
**coordination-hungry** (the pieces share context, depend on each other, and must converge
to one voice). Switchboard fits this *without forcing it* — it is the same profile as the coding
case, generalized. Industrial / streaming / high-volume operational work (telco anomaly
draining, SRE) is a real **adjacent generalization** (see §19) but is explicitly **not the
wedge** — that work is mostly decidable, where a workflow engine wins, and the agentic tail
is thinner. Land knowledge work first; generalize down to operations later.

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
>
> **The wire-level contract for the core of these (presence · leases · messages/signals ·
> delta · handshake) is normatively specified in [`IXP-SPEC.md`](IXP-SPEC.md) — the
> `IXP-core` profile.** This section is the product-level ABI; the spec is the implementable
> protocol. Dispatch (FR-12/13) and cost (FR-20/21/22) are the `TXP` / `OXP` profiles, not
> yet specced (§17).

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
- **FR-21** **Cost-per-outcome ledger (Tally):** record tokens/$ per task, per agent, per epic;
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

## 13. Standardization & adoption — escaping SIP's grave

The durable advantage is the **convention**. But a coordination standard can die (SIP) or
win (MCP, Kubernetes) depending on *who pulls adoption*. Be precise about which kind we are.

**Two kinds of standard:**

- **Vertical** (model → tools): **MCP**. Spread like wildfire because it serves the
  *sponsor's own interest* — Anthropic wants Claude to use more tools. Adoption is **pulled
  by the beneficiary.**
- **Horizontal** (vendor ↔ vendor, rivals must interoperate): **SIP**. RCS-vs-iMessage.
  Federated everything. These **fight incentives** — the big players want walled gardens, so
  the open standard stalls. *Vendors don't want open coordination.*

Cross-vendor agent coordination is *shaped* like SIP. So **"vendors, please coordinate"
dies.** We escape it with four moves — the first is the key one:

1. **Single-player value first.** SIP was useless to the first adopter — you needed a
   network. Switchboard coordinates **one team's own fleet on day one, zero cooperation required**,
   then compounds. That's the property MCP had and SIP lacked: value *before* the network.
2. **Buyer-pull, not vendor-blessing.** The *customer* feels the LLM-islands pain and the
   runaway bill — **they** mandate the layer for their own fleets. This is how Kubernetes
   beat cloud lock-in, OpenTelemetry beat locked-in observability, USB-C went universal:
   the demand side had leverage where the vendors didn't.
3. **Cost is the adoption wedge.** Nobody adopts a standard for elegance (SIP was elegant).
   They adopt it to stop bleeding money and to get control. Lead with cost governance; the
   coordination protocol rides in *underneath*. **The trojan horse is the invoice.**
4. **Neutral governance, eventually.** A published spec under a neutral home (foundation) so
   buyers don't fear trading vendor lock-in for *ours*. That's what made K8s/OTel trusted.

**So: "can we be a standard like MCP?"** Not the way MCP became one (vertical,
vendor-pulled) — **the way Kubernetes became one** (horizontal, buyer-pulled, neutral,
useful single-player on day one). **Coordination is the *what*; cost control is the *why
they'll adopt it.*** The cost-per-outcome story (see §20) is what gives the buyer a reason
to pull.

**The spear (anti-tokenmax positioning — put it on the homepage):**

> *Your model vendor profits when your agents waste tokens. We're the neutral layer that
> manages across your LLM islands and shapes agents toward outcomes — not someone else's
> revenue.*

No model vendor can credibly say this (they're structurally incentivized toward tokenmax),
which is exactly why the neutral cross-island position is the defensible one.

**Execution checklist:** publish the spec (the §7–8 syscalls + session-start sequence +
lease/ack semantics); dead-simple onboarding (one-liner to load the toolset per runtime —
MCP config + `AGENTS.md`/`CLAUDE.md` protocol + REST shim); reference adapters (Claude Code
hooks, Agent SDK events, generic REST loop) so "load us first" is minutes; lead the story
with the six-agent session + the cost number — the two things competitors can't fake.

**Conformance over decoration — the only load-bearing use of the `_XP` family.** The IP is
the **wire semantics** of each primitive (§7–8) — the lease state machine, the delta cursor,
atomic `claim_next`, the ack/message model, the budget-governor IRQ/NMI, Tab÷Tally accounting
— *not* the names or the networking analogies (those are positioning/pedagogy, freely
copyable). The `_XP` layering earns its keep **only as conformance profiles**: an agent is
*IXP-core conformant* by implementing just the signaling core (presence + leases + messages +
interrupts), and adds **TXP** (work/dispatch) and **OXP** (outcome/Tally) later. This
**partial, layered adoptability** — minimal core in minutes, grow into the rest — is what let
MCP and Kubernetes spread, and is the property worth specifying and (eventually) certifying.

> **Naming — decided (2026-06-27).**
>
> **Product: Switchboard** — directed routing, operator-in-the-loop, telco/SIP heritage;
> names what it *does* (connect the right caller to the right line).
>
> **Protocol: IXP — Instruction Exchange Protocol.** Deliberately echoes an *Internet
> Exchange Point* (IXP) — the neutral interconnect where independent networks peer and
> exchange traffic with no middleman owning it — which is exactly the thesis: the neutral
> exchange where agent islands swap work. It **follows Switchboard**'s telephony/interconnect
> heritage. "Instruction" also fixes the unit at the **fine grain** — the agent's tool-call /
> IRQ boundary (§8.5). The overlap with *Internet Exchange Point* is intentional positioning,
> not confusion: same domain, same concept (neutral exchange), so it reinforces — unlike
> ISP/IDP/ICP, which point at the *wrong* domain.
>
> **Naming family — `_XP` (Exchange Protocol):** the lead letter selects the unit —
> **A**XP (Agent) · **T**XP (Task) · **I**XP (Instruction, canonical) · **O**XP (Outcome).
> IXP is the canonical name; the others are lenses for emphasizing a specific unit.
>
> **Feature: Tally** — the cost-per-outcome ledger (FR-21). Mark/motif: the kanji **正**
> (Japanese tally character, 5 strokes) which also means *correct* — encoding the *verified*
> outcome. Framing: tokens go on the **Tab** (spend), outcomes on the **Tally** (value);
> cost-per-outcome = Tab ÷ Tally.
>
> **Tagline:** *"Switchboard speaks IXP and keeps a Tally — one protocol, from epic to the
> instruction boundary (the agent's IRQ)."*
>
> *Rejected:* Mesh (crowded — service/data mesh), Conductor (Netflix Conductor = a workflow
> engine, the category we oppose), Fabric (Microsoft Fabric); and "Instruction-*" acronyms
> that point to the wrong domain — ISP (Internet Service Provider), IDP (Identity Provider),
> ICP (Insane Clown Posse / China ICP license).
>
> *To do:* protocol/product availability check (trademark · domain · GitHub · npm) before the
> public spec drops — **not yet verified.**

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

### Open items (tracked status)

- 🔴 **Auth gap — reference impl is NOT `IXP-core` conformant (today risk, not roadmap).**
  [`IXP-SPEC.md`](IXP-SPEC.md) §12 / R-2 and PRD NFR-5 require authenticated writes, but the
  reference implementation leaves writes **open by default** (`app.py` task CRUD has no auth;
  `mcp_server.py` writes gate only if `PM_MCP_TOKEN` is set). **If `plan.taikunai.com` is
  network-reachable, anyone can rewrite the board, impersonate agents, and trigger spend —
  this is a *today* security risk, not a future feature.** *Fix:* per-agent bearer auth on
  every write surface; record the authenticated identity as `actor`. **Owner/ETA: TBD —
  treat as P0.**
- 🟡 **`TXP` / `OXP` specs — deferred, intentionally.** Only the `IXP-core` signaling profile
  is specified. Work-dispatch (`TXP`: `claim_next`, dependency-aware routing) and
  outcome-settlement (`OXP`: the **Tally** ledger, budgets, verification) are **not** specced
  yet — and **should not be** until a real adopter is pulling for them. Writing them now is
  speculative surface area; the minimal core is what wins adoption. *Trigger: first adopter
  asking for dispatch or cost accounting.*
  - *Design note (carry into the OXP spec):* model **Tally/OXP as a read-side projection
    over the append-only activity log — a metering tap, not new mutating wire ops.** Cost/
    outcome accounting *observes* every layer (a management plane, not a stack layer), so the
    `actor`/timestamp/idempotency discipline `IXP-core` already mandates is what makes it
    possible for free. (`TXP`'s `claim_next` is the opposite — it builds *on* the lease
    primitive, a true layer above the core.)

### Strategic risks

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

## 18. Switchboard vs orchestration frameworks (LangGraph, workflow engines)

The recurring confusion is the word "orchestrate." Split the job into three layers and
the boundaries become exact:

1. **Plan / decompose** — turn a goal into work items + a dependency graph.
2. **Dispatch / schedule** — decide *which worker* gets *which item*, when, at what cost.
3. **Execute** — the *steps* to do one item (the "how").

| | Plan/decompose | Dispatch/schedule | Execute (the "how") |
|---|---|---|---|
| **LangGraph** | — | a little, within its *closed* authored node set (supervisor pattern) | ✅ **its main job** — control flow of one task's execution, one run, one process |
| **Workflow engine** | author draws it once | assign to a worker pool | ✅ for *decidable* work (fixed, knowable steps) |
| **Switchboard** | ✅ holds the plan + dependency graph (the board) | ✅ **across an open, durable, multi-vendor fleet, with cost-per-outcome as the objective** | ❌ **never** — delegated to the autonomous agent's judgment |

**The resolving statement:** *Switchboard orchestrates the **plan** and the **dispatch** — what
work exists, what depends on what, who does it, at what cost. It does NOT orchestrate the
**execution.** LangGraph and workflow engines orchestrate the execution (they dictate the
steps); Switchboard delegates that to the agents.* This is why Switchboard is simultaneously "coordination
not orchestration" (it never dictates the steps) and "it routes/orchestrates complex work"
(it owns the plan + dispatch) — the two are true at different layers.

**Altitude:** LangGraph routes *within one task's execution* (within-task). Switchboard routes
*work to workers across the fleet* (across-fleet). They **compose** — a LangGraph app (or a
workflow engine) is just one *worker* Switchboard dispatches to. LangGraph correction for the
record: it *can* route (conditional edges; an LLM supervisor routing to worker nodes). The
difference is **scope** (one authored run, one vendor, ephemeral vs. an open durable
multi-vendor fleet), **routing function** (coded state/logic vs. cost-per-verified-outcome +
reliability + load as a substrate primitive), and **model** (push to known nodes — a
flowchart — vs. pull-based `claim_next` to a self-selecting pool — a scheduler/market).

Analogy: **LangGraph is a program; Switchboard is the cost-aware cluster scheduler the programs run
under** — the K8s-scheduler / FinOps layer for a datacenter of agents.

---

## 19. The parallel work profile — knowledge work first

The coding template — *huge plan → decompose → run agents in parallel on independent
branches → they coordinate (leases, dedup, handoff, decisions) → converge* — is the general
shape. The value is **not parallelism per se**; it is **safe, cost-bounded parallelism over
shared, contended, dependency-linked state where the workers have judgment.** Switchboard is what
*unlocks* the parallel mode: without it, parallel agents collide, redo work, and blow the
budget; with it, a serial backlog becomes a coordinated swarm.

**The beachhead is high-intelligence knowledge work (see §3), where this profile is native —
not industrial/operational, where it is the exception.** Knowledge-work deliverables are
*one big artifact decomposed into interdependent pieces worked in parallel by agents with
judgment, that must converge to one coherent voice* — exactly the coding profile:

- **Software** — the canonical case: an epic → parallel agents on branches, leases + the
  decisions log + `claim_next`, converge to a merged feature.
- **Consulting engagements** — a diagnostic → parallel workstreams (market scan, ops review,
  financial model, recommendations) that depend on each other and must reconcile to one deck.
- **Investment-banking deliverables** — a pitch: parallel agents on the model, the comps,
  the industry section, the precedent transactions — shared assumptions (one WACC, one set of
  comps) enforced via the decisions log so the sections don't contradict; converge to one
  book.
- **Sales proposals / RFPs** — decompose the RFP into requirement-sections, parallel agents
  draft each, dedup boilerplate, enforce one win-theme and one price via shared state,
  converge to a single submission.

In every case the decisions log becomes **shared institutional memory** — one settled
assumption (the WACC, the win-theme, the architecture choice) propagates so the *N*-th piece
is cheaper and more consistent than the first. That's the **cost-per-outcome flywheel: the
fleet gets cheaper and more coherent as it learns** — something neither a workflow engine nor
a single-run LangGraph app can give you.

### Adjacent generalization — operational / streaming work (later, NOT the wedge)

The same profile *generalizes* to high-volume operational work (telco anomaly draining, SRE
incident response, large remediation/migration), but that is a **later** market, not the
beachhead: most of that work is rule-*decidable* (a workflow engine wins) and only a thin
*investigative* tail is agentic. In that world Switchboard sits **above** a workflow engine as the
cost-aware triage-and-dispatch layer, with the engine as its cheapest worker tier:

```
work / anomalies →
  Switchboard triage (cheap classifier: decidable vs investigative?)
    ├─ decidable      → workflow engine   (cheapest, deterministic, billions/day)
    └─ investigative  → agent fleet       (parallel, coordinated, cost-routed)
                           ↳ leases · dedup-to-root-cause · handoff · claim_next
  └─ cost-per-outcome ledger spanning BOTH tiers
```

Dumb (rule-decidable) work stays on the workflow engine — Switchboard only *routes* it there and
never agentifies it. The parallel-fleet value appears **only on the investigative tail**
(e.g. an incident/root-cause swarm where the cause isn't knowable in advance). Pursue this
only after knowledge work is landed.

**The honest boundary (scope guard):** dumb + independent → workflow engine, full
stop. High *volume* alone is a *scaling* problem (Kafka consumer groups + elastic workers),
NOT a coordination problem — do not justify Switchboard by queue size. Switchboard earns its place only
when the work is **investigative AND the parallel workers must coordinate** (dedup, deps,
shared resources, handoff). The signal that work has crossed the line: *you keep adding
branches to the workflow engine to handle cases it can't really decide* — that growing tail
is the Switchboard-shaped hole.

---

## 20. Cost-per-outcome *direction* (the six levers)

§8.7 *measures* cost; this section is how Switchboard *directs* it. **You don't ask agents to be
frugal — you change the economics they run in.** Tokenmax is Goodhart's law: optimize the
*numerator* (tokens) because it looks like progress, and the bill is the hangover. The fix
is to make the **unit of account the verified outcome** and **dollars the constraint**, then
push six levers — most of which only a coordination layer can pull, because it is the one
thing sitting in the middle of every agent's context *and* spend.

| Lever | Mechanism | Why only the layer can do it |
|---|---|---|
| **Budget governor** | Each task carries a token/$ cap; `remaining_budget` on every tool response; near the cap fire the **IRQ** ("wrap up"), at the cap the **NMI** (halt + escalate) | reuses the interrupt path (FR-14/16); nobody else has it cross-runtime |
| **Model right-sizing** | `claim_next` returns the task **and a recommended model tier** from `risk_level`/complexity — Haiku for mechanical, Opus only for the gnarly | the single biggest $ lever (5–10×); vendors won't (it cuts their bill) |
| **Context economy** | Hand the agent its *minimal optimal context* — delta-poll not full-board, cache-stable serialization, the Haiku pre-digest, a context-pack | uniquely the substrate's job — it controls what the agent sees |
| **Kill re-work** | Presence + leases + decisions log stop two agents doing the same thing or re-deriving a settled choice | only the layer sees the whole fleet |
| **Verified denominator** | "Done" doesn't count until exit_criteria are verified (cheap Haiku / CI check) — stop *paying for plausible-but-wrong* | outcome-per-dollar, not tokens-per-task |
| **Reliability-weighted dispatch** | route work to the agent/model with the best historical cost-per-verified-outcome on similar tasks | a market mechanism only a dispatcher can run |

**The alignment claim (the spear restated):** the **model vendor is structurally
incentivized toward tokenmax** — every wasted token is their revenue. **Only a neutral,
buyer-aligned layer is incentivized toward cost-per-outcome.** That is why this cannot come
from Anthropic or OpenAI, and why it is the wedge that makes the standards play (§13)
adoptable: cost is the *why they'll pull.*

---

## 21. See also

- [`IXP-SPEC.md`](IXP-SPEC.md) — **the protocol spec** (`IXP-core`): the wire contract (the IP)
- [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md) — positioning, competitive read, the three bets
- [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md) — the primitives, derived from session data
- [`MCP.md`](MCP.md) — MCP server design and tool reference
- [`AGENT_OPERATOR_FEATURES.md`](AGENT_OPERATOR_FEATURES.md) — single-agent operator layer
- `decisions/0001-…` — ADR on coordination-primitive build order
