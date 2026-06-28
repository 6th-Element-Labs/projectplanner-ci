# IXP — Instruction Exchange Protocol · Core Spec (`IXP-core`)

- **Status:** Draft v0.1 (for review)
- **Date:** 2026-06-27
- **Layer:** signaling core (presence · leases · messages/signals · delta · handshake)
- **Profiles:** this document specifies **`IXP-core`**. Work-dispatch (`TXP`) and
  outcome-settlement (`OXP`) are separate profiles layered on this core (out of scope here;
  see [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md), [`TALLY-SPEC.md`](TALLY-SPEC.md), and
  PRD §8.4, §8.7).
- **One-line:** the model-agnostic wire contract any agent — behind any LLM, in any runtime
  — speaks to coordinate with other agents over a shared, durable substrate.

> This is the IP artifact. Names, analogies, and positioning live in the PRD
> ([`PRD-AGENT-COORDINATION-LAYER.md`](PRD-AGENT-COORDINATION-LAYER.md)); the moat is *this*
> contract becoming a convention. The reference implementation primitives already exist in
> `store.py` (tables `file_leases`, `agent_messages`, `activity`) and `mcp_server.py`.

---

## 1. Scope & conformance

`IXP-core` defines the minimal contract for **cooperative coordination among independent
agent sessions** sharing one workspace: who is present, who holds which resource, directed
messages and interrupt signals between agents, and an efficient change feed. It is the
"signaling layer" — fine-grained coordination at the agent's tool-call / instruction
boundary.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY** are to be
interpreted as in RFC 2119.

`IXP-core` is **advisory**: the substrate surfaces information so agents make better
decisions; it does **NOT** enforce mutual exclusion at the write layer (no hard locks — see
§6.5). Hard, unmaskable stop (process kill) is an out-of-band *deployment* concern, **not**
part of the wire (§7.5).

An implementation is **`IXP-core` conformant** iff it satisfies every **MUST** in §4–§9 and
the conformance checklist in §11.

---

## 2. Model & terminology

- **Workspace** (`project`): an isolation boundary. Every operation carries a workspace id.
  Reads and writes **MUST NOT** cross workspaces. An unknown workspace id **MUST** fail
  closed (reject; never default-route).
- **Agent** / **agent_id**: a stable identifier for one agent *session*. Format
  **SHOULD** be `"<runtime>/<scope>"`, optionally with a uniqueness suffix —
  e.g. `claude/ENGINE-11`, `codex/CHART-8`, `cursor/REVIEW#a1b2`. An agent_id **MUST** be
  stable for the lifetime of the session that owns it.
- **Resource**: a non-shareable thing agents contend for, addressed by
  `(resource_type, name)`. `resource_type` ∈ `file` · `port` · `build_dir` · `worktree` ·
  `binary` · `branch`. `name` is opaque to the protocol (e.g. a repo-relative path, a port
  number as string).
- **Lease**: a time-bounded advisory claim on one or more resources by an agent.
- **Message**: a directed communication from one agent to another, optionally carrying a
  **signal** (§7).
- **Cursor**: a monotonic, gap-free sequence number over the workspace **activity log**.
  Used for efficient delta polling (§9). The reference cursor is the activity row id.
- **Activity log**: an append-only record of every mutation; the single source of truth for
  "what happened." Every mutation **MUST** append an entry carrying `actor` (agent_id or a
  reserved system actor) and an ISO-8601 UTC timestamp.

---

## 3. Transport bindings

`IXP-core` is transport-agnostic and **MUST** be offered over at least one of:

1. **MCP** (Streamable HTTP) — each operation is one tool. This is the primary binding for
   MCP-native runtimes (Claude Code, Cursor, Claude Desktop, Codex-MCP).
2. **REST + JSON** — the thin shim for non-MCP runtimes (LangGraph, raw tool-calling loops).
   Operation ⇒ `POST /ixp/v1/<operation>` (reads **MAY** use `GET`). Same field names, same
   semantics, same activity-log effects as the MCP binding.

Binding requirements:

- **R-1 (deterministic serialization).** Responses **MUST** be serialized with sorted keys
  and **MUST NOT** include volatile fields that change between otherwise-identical calls
  (e.g. human-relative times, `generated_at`). This is required for LLM prompt-cache hits
  across sessions. (Ref: `mcp_server._dumps`.)
- **R-2 (auth).** Every **write** operation **MUST** be authenticated when the substrate is
  network-reachable. The binding **MUST** support a per-agent bearer credential; the
  authenticated identity **MUST** be recorded as `actor`. Reads **MAY** be open within a
  workspace. *(Reference impl gap: writes are currently open by default — see PRD NFR-5 and
  [`P0-SPEC.md`](P0-SPEC.md).)*
- **R-3 (idempotency).** Mutating operations that a client may retry (`claim`, `send`)
  **MUST** accept a client-supplied `idem_key` and **MUST** deduplicate on it within the
  workspace: a repeated `idem_key` returns the original result and performs no second effect.

---

## 4. Identity & presence

Presence answers "who is live, on what, in which runtime" so a sender can address a peer.

### 4.1 Operations

- `register_agent(project, agent_id, runtime, model?, lane?, task?, ttl_s=120) → presence`
  — announce a live session; establishes a heartbeat TTL.
- `register_agent(..., protocol?)` — agents **SHOULD** advertise the `protocol` envelope they
  support. The response includes `protocol_compatibility`; adapters **MUST** stop or downgrade
  when it is incompatible.
- `heartbeat(project, agent_id) → presence` — renew. An agent **SHOULD** heartbeat at an
  interval ≤ `ttl_s/2`.
- `list_active_agents(project, lane?) → [presence]` — agents whose presence is unexpired.

### 4.2 Semantics

- An agent **MUST** `register_agent` before sending messages or claiming resources.
- A presence record whose last heartbeat is older than `ttl_s` is **stale**; stale records
  **MUST NOT** appear in `list_active_agents` and **MUST** be treated as "not present."
- `presence` object:
  ```json
  {"agent_id":"claude/ENGINE-11","runtime":"claude-code","model":"claude-opus-4-8",
   "lane":"ENGINE","task":"ENGINE-11","registered_at":"...Z","expires_at":"...Z",
   "protocol_compatibility":{"compatible":true,"mode":"exact","version":"ixp.v1"}}
  ```

---

## 4.3 Protocol Envelope

`get_working_agreement(project)` **MUST** include:

```json
{
  "protocol": {
    "name": "switchboard",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profile_version": "2026-06-28",
    "profiles": {"ixp_core":"1.0","txp_dispatch":"0.1"},
    "compatible_versions": ["ixp.v1"],
    "field_aliases": {
      "send_agent_message.ack_timeout_seconds": "ack_deadline_minutes"
    }
  }
}
```

Adapters **MUST** fail closed on a known-incompatible `version`. Missing protocol metadata is
treated as legacy only by the server; adapter packs may require it once their profile says so.

---

## 5. (reserved)

> Numbering preserved for alignment with the PRD; resource leases follow.

---

## 6. Resource leases (CSMA/CA)

A lease is an advisory reservation: an agent claims `(resource_type, name)` *before* acting,
so peers can avoid collisions ("reserve, then act" — collision *avoidance*, not detection).

### 6.1 Operations

- `claim(project, agent_id, resource_type, names[], task?, ttl_min=30, idem_key?) → grant | conflict`
- `check(project, resource_type, names[]) → [held]` — which of `names` are currently held.
- `release(project, lease_id) → {released: bool}` — idempotent.
- `list_active_leases(project) → [lease]` — all unexpired, unreleased leases.

### 6.2 Grant / conflict

On success (all `names` free):
```json
{"lease_id":"<uuid>","agent_id":"claude/ENGINE-11","resource_type":"file",
 "names":["web/collision.js"],"task":"ENGINE-11","claimed_at":"...Z","expires_at":"...Z"}
```
On conflict (≥1 name held by a *different* live lease):
```json
{"conflict":true,"resource_type":"file","name":"web/collision.js",
 "held_by":"claude/CHART-8","holder_task":"CHART-8","expires_at":"...Z",
 "retry_after_seconds":60}
```
- `claim` **MUST** be all-or-nothing across `names`: if any is held, **no** lease is created.
- On conflict the server **MUST** return `retry_after_seconds`, computed as
  `max(30, floor(remaining_ttl_seconds / 2))`. The client **SHOULD** wait at least that long
  before retrying (e.g. `Bash(sleep N)`); this is advisory backoff, not enforcement.

### 6.3 Expiry & release

- A lease **MUST** auto-expire at `expires_at` (= `claimed_at + ttl_min`) without an explicit
  `release`. Expired leases **MUST NOT** cause conflicts and **MUST NOT** appear in
  `check`/`list_active_leases`.
- `release` **MUST** be idempotent: releasing an unknown/already-released lease returns
  `{"released": false}` and **MUST NOT** error or corrupt state.

### 6.4 State machine

```
(none) --claim--> HELD --release--> RELEASED
                   |
                   '--(ttl elapsed)--> EXPIRED
```
`HELD` is the only state that produces conflicts. `RELEASED` and `EXPIRED` are terminal.

### 6.5 Enforcement model (normative)

Leases are **advisory**. The substrate **MUST NOT** block a write because a resource is
leased. An agent that claims and receives a conflict decides what to do (wait, renegotiate
scope, message the holder). An agent that proceeds despite a conflict is **non-conforming**
but **MUST NOT** be blocked at the wire — it is surfaced, not prevented. (Hard serialization
is a `TXP` merge-queue concern, not `IXP-core`.)

### 6.6 Forward: agent-scaffolded capabilities (planned `resource_type`)

As agents increasingly **scaffold their own tools/environments at runtime**, a tool an agent
invents becomes a *shared resource the swarm must discover, not silently re-invent*. A planned
`resource_type` — **`capability`** (a.k.a. `tool`) — extends the lease/registry model so a
runtime-created tool is **published into presence, leased while being built, and discovered**
by peers via `check`/`list_active_leases`, with a version. This turns self-scaffolding from
sprawl (every agent re-builds and re-pays for the same tool) into a **compounding shared
library the fleet builds for itself.** Not in `IXP-core`; reserved here so the resource model
anticipates it. *(The matching oversight — provenance + approval gates for what a swarm builds
— is an `OXP`/control-plane concern: see [`TALLY-SPEC.md`](TALLY-SPEC.md) and PRD §8.6–§8.7.)*

---

## 7. Directed messages & signals

Messages are point-to-point with an explicit acknowledgement — distinct from a broadcast
comment, which is fire-and-forget. A message **MAY** carry a **signal** that requests the
recipient change its execution.

### 7.1 Operations

- `send(project, from_agent, to_agent, message, signal?, priority=0, requires_ack=false, ack_deadline_min?, task?, idem_key?) → msg`
- `inbox(project, to_agent, unacked=true, signal?) → [msg]` — the recipient's queue.
- `ack(project, message_id, response?) → msg` — recipient confirms receipt/handling.
- `status(project, message_id) → msg` — sender checks for ack.
- `list_pending_acks(project, agent_id?) → [msg+monitor]` — outstanding ack-required messages.
- `list_monitors(project, status?, kind?) → [monitor]` — durable monitor state.
- `sweep_monitors(project) → {checked,resolved,fired,events}` — evaluate monitors now.
- `resolve_monitor(project, monitor_id, reason?) → monitor` — operator/manual cleanup.
- `cancel_monitor(project, monitor_id, reason?) → monitor` — stop a monitor that should not fire.

If `requires_ack=true`, `send` **MUST** create a durable `ack_deadline` monitor and return
`monitor_id`. `requires_ack` is not just metadata: if no agent acks before `ack_deadline`, the
monitor sweep writes a `monitor.timeout` activity event and sends an `ack_timeout` notice to the
sender. A production Switchboard deployment SHOULD run monitor sweep on a durable host timer.
Waking an absent runtime is outside `IXP-core`; see
[`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md) for host registration and wake intents.

### 7.2 Signals

`signal` ∈ `{heads_up, redirect, stop}` (absent ⇒ a plain message):

| signal | recipient obligation |
|---|---|
| `heads_up` | note it; **MUST NOT** be required to halt |
| `redirect` | **SHOULD** adopt the new instruction and `ack`; continue on the new path |
| `stop` | **SHOULD** halt the current line of work at the next boundary (§7.4) and `ack` |

`priority` is an integer (higher = more urgent); recipients **SHOULD** drain higher priority
first. `stop`/`redirect` with high priority is the intended "interrupt."

### 7.3 `msg` object
```json
{"id":4821,"from_agent":"codex/PLAN","to_agent":"claude/ENGINE-11","task":"ENGINE-11",
 "message":"Halt — schema froze; rebase before touching collision.js","signal":"stop",
 "priority":10,"requires_ack":true,"ack_deadline":"...Z","sent_at":"...Z",
 "acked_at":null,"ack_response":null}
```

### 7.4 Delivery & the instruction boundary (normative)

The substrate is **pull-based**: it **MUST NOT** be assumed able to push into a running agent
(MCP clients are request-response and do not subscribe to server push). Therefore:

- An `IXP-core` agent **MUST** call `inbox` at session start, and **SHOULD** call it at each
  **tool-call boundary** (the agent's instruction boundary) or via a runtime adapter that
  does so on its behalf (e.g. a Claude Code `PreToolUse` hook that fetches and, on a `stop`,
  denies the pending tool with the message as the reason).
- The tightest guaranteed latency of a signal is **one tool-call boundary** — nothing reaches
  the model mid-token. Implementations **MUST NOT** claim finer-than-boundary delivery.
- A recipient that acts on a `stop`/`redirect` **MUST** `ack` (optionally with a one-line
  `response`) so the sender can confirm the signal landed before proceeding.
- If the recipient is absent, the message is only durably stored. Delivery requires an agent
  session to start or resume and poll the inbox. The optional Agent Host layer can create that
  session, but the bus itself cannot.

### 7.5 Escalation to hard stop (informative)

If a `stop` is not honored (agent not polling, or non-cooperative), the only guaranteed halt
is **out-of-band**: the runtime/runner terminates the process. This "NMI" is a deployment
mechanism, **not** an `IXP-core` wire operation, and **MUST NOT** be relied on as in-band
delivery.

---

## 8. The session-start sequence (handshake)

Every `IXP-core` agent **MUST** perform this sequence at session start, before doing work:

1. `get_working_agreement(project)` — fetch the canonical rules-of-the-repo policy
   (definition of done, branch convention, `merge_strategy`, `canonical_main_sha`, port map,
   BYO-data) and conform to it for the session. This makes every agent — behind any model —
   play the same game instead of each inventing its own flow. See PRD §8.8 / ADR-0003.
2. `register_agent(...)` — announce presence.
3. `inbox(to_agent=self, unacked=true)` — drain pending messages/signals; `ack` as handled.
4. *(if it will edit resources)* `check(...)` then `claim(...)` before the first write.

An agent **SHOULD** repeat step 3 (and a `heartbeat`) at each tool-call boundary, and
**MUST** `release` its leases on completion. Runtimes **SHOULD** automate the whole sequence
via an adapter (hook / SDK lifecycle) so the model need not be relied upon to remember it —
this is how the handshake becomes a guarantee rather than a suggestion. The adapter contract
and the three-tier adoption model are specified in
[ADR-0004](decisions/0004-adoption-and-enforcement.md); the Claude Code reference adapter
lives in [`adapters/claude-code/`](../adapters/claude-code/).

An agent **MUST NOT** set a task to `Done` itself: it reports progress only up to `In Review`
via `complete(evidence={branch, head_sha, pr?})`; the merge webhook is the sole writer of
`Done` (it stamps the `merged_sha`). This keeps task status git-derived, not self-reported —
see ADR-0003 (work provenance & reconciliation).

---

## 9. Change feed (delta polling)

To avoid re-reading full state, agents poll a cursor-scoped delta.

- `get_delta(project, lane?, since_cursor=0) → {cursor, updates}`
- The response **MUST** return a new `cursor` (the highest activity id observed) and an
  `updates` array of changes with `id > since_cursor`, filtered to `lane` when given.
- When nothing changed, `updates` **MUST** be empty (a near-zero-cost response). Clients
  **MUST** persist `cursor` between calls and pass it back.
- Cursors **MUST** be monotonic within a workspace. Timestamps **MUST NOT** be used as the
  cursor (clock skew across sessions).

```json
{"cursor":4847,"updates":[
  {"id":4847,"kind":"lease.released","resource":"file:web/collision.js","agent_id":"claude/CHART-8"},
  {"id":4846,"kind":"message.sent","to_agent":"claude/ENGINE-11","signal":"stop"}]}
```

---

## 10. Idempotency, ordering, errors

- **Idempotency** (§3 R-3): `claim` and `send` are deduplicated on `idem_key`. Agents retry
  on network error; the substrate **MUST NOT** double-apply.
- **Ordering**: the activity log is the total order within a workspace. Cross-agent ordering
  is defined only by activity ids, never by wall-clock.
- **Error shape**: errors **MUST** be machine-readable: `{"error":"<code>","detail":"..."}`.
  Reserved codes: `unknown_workspace`, `unknown_agent`, `unknown_lease`, `unknown_message`,
  `unauthorized`, `conflict` (leases use the §6.2 conflict object, not this error).

---

## 11. Conformance checklist — `IXP-core`

An implementation is `IXP-core` conformant iff:

- [ ] Workspace scoping is fail-closed; writes never cross workspaces. (§2)
- [ ] Deterministic serialization; no volatile fields. (R-1)
- [ ] All writes authenticatable; authenticated identity recorded as `actor`. (R-2)
- [ ] `claim`/`send` honor `idem_key` dedup. (R-3)
- [ ] Presence: `register_agent` + `heartbeat` + `list_active_agents`; TTL staleness. (§4)
- [ ] Leases: `claim` (all-or-nothing) / `check` / `release` (idempotent) /
      `list_active_leases`; `retry_after_seconds = max(30, remaining/2)`; TTL auto-expiry;
      advisory (never blocks writes). (§6)
- [ ] Messages: `send` / `inbox` / `ack` / `status`; signals `heads_up|redirect|stop`;
      `priority`; pull delivery; boundary-latency honesty. (§7)
- [ ] Session-start handshake supported. (§8)
- [ ] `get_delta` cursor feed; empty when unchanged; monotonic id cursor. (§9)
- [ ] Append-only activity log with `actor` + UTC timestamp on every mutation. (§2)

**Out of scope (other profiles):** `claim_next` / dependency-aware dispatch (`TXP`, see
[`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md)); cost-per-outcome ledger / verification /
budgets (`OXP`, the **Tally** feature, see [`TALLY-SPEC.md`](TALLY-SPEC.md)). An
implementation **MAY** advertise `IXP-core`, `+TXP`, `+OXP` independently.

---

## 12. Security considerations

- Open writes on a network-reachable substrate allow any caller to rewrite the board,
  impersonate agents, and trigger spend. Production deployments **MUST** require per-agent
  credentials on writes (R-2). The reference hardening plan is [`P0-SPEC.md`](P0-SPEC.md).
- `agent_id` is self-asserted unless bound to a credential; an authenticated binding
  **SHOULD** be enforced so an agent cannot spoof another's `actor`, `ack`, or leases.
- Message bodies are untrusted input to the receiving agent's context; recipients **SHOULD**
  treat `redirect`/`stop` instructions as advice to evaluate, not commands to obey blindly
  (prompt-injection surface).

---

## 13. Appendix — example flows

**A. Claim → conflict → backoff → acquire**
```
A: claim(file, ["collision.js"], agent="claude/ENGINE-11", idem_key="k1")
   → {conflict, held_by:"claude/CHART-8", retry_after_seconds:60}
A: (Bash sleep 60); inbox(self)  # maybe CHART-8 messaged me
A: claim(file, ["collision.js"], ..., idem_key="k1")  # same key; dedup-safe
   → {lease_id:"...", expires_at:"...Z"}
A: ...edit... ; release(lease_id)
```

**B. Live stop via a PreToolUse adapter (boundary-latency interrupt)**
```
PLAN: send(to="claude/ENGINE-11", signal="stop", priority=10, requires_ack=true,
           message="schema froze — stop before editing collision.js")
ENGINE-11 hook (PreToolUse): inbox(self, signal="stop") → [msg]
   → deny the pending Edit with reason = msg.message
ENGINE-11: ack(msg.id, response="halted; awaiting rebase")
PLAN: status(msg.id) → {acked_at:"...Z", ack_response:"halted; awaiting rebase"}
```
