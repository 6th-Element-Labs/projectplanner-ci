# ADR-0017 — Boundary delivery of ordinary messages (mid-turn inbox drain)

- **Status:** Proposed
- **Date:** 2026-07-21
- **Author:** codor-comparison session (Claude Code), from the inbox-concept analysis thread
- **Relates to:** [`IXP-SPEC.md`](../IXP-SPEC.md) §7.4 (delivery & the instruction boundary) ·
  [ADR-0004](0004-adoption-and-enforcement.md) (shared adapter core, boundary enforcement) ·
  [ADR-0006](0006-control-plane-done-enough.md) (subtraction rule — satisfied below) ·
  [`AGENT-HOST-SPEC.md`](../AGENT-HOST-SPEC.md) §10 (delivery guarantees) ·
  [`MULTI_AGENT_COORDINATION.md`](../MULTI_AGENT_COORDINATION.md) §3.2 (event subscriptions —
  the mechanism this ADR subtracts) · FR-14 interrupt-consume:
  [`adapters/switchboard_core.py`](../../adapters/switchboard_core.py)

---

## Context

**The gap, precisely:** IXP §7.4 says an agent SHOULD drain its inbox at each tool-call
boundary. The FR-14 guard already does this — `_consume_interrupt` GETs the full unacked
inbox at every guarded `PreToolUse` boundary — but it consumes only stop-class signals
(`stop` / `redirect` / `claim_revoked`) and **discards every ordinary message it just
fetched**. A peer's non-signal message (`heads_up`, plain coordination text) dead-letters
until the recipient's *next session start*, which is unbounded. The spec's intent is
boundary-latency delivery; the behavior is session-start delivery. This ADR closes that gap.

**The economics:** the messages are already on the wire at every boundary and thrown away.
Delivering them costs zero additional round-trips — this is a routing decision on data in
hand, plus one timestamp column, not a new fetch path.

**External evidence the pattern works:** codor (`live_inbox: true`) injects unread channel
messages into Claude Code after tool calls via a hook, consuming them to keep context
bounded. We adopt the injection mechanic and reject its consumption semantics (see
Alternatives) — our receipt model distinguishes stored / delivered / handled and must keep
doing so.

**Why now, under ADR-0006 Decision 4** (coordination work is by exception during the
product-first horizon): this qualifies as the exception on three grounds. It is small (the
data is already fetched; the delta is routing plus a `delivered_at` stamp). It removes a
live H2 pain — during the Helm sprint, a peer's non-signal message currently waits for the
recipient's next session, which is exactly the coordination failure mode the sprint will
hit. And it **deletes a planned mechanism rather than adding one** (the subtraction, below).

## Decision

### 1 — Split channels by message class

- **Stop-class signals stay on `PreToolUse` deny**, unchanged. Pre-action is the point:
  halt *before* the side effect, with the message as the deny reason (FR-14 as shipped).
- **Ordinary messages are delivered via a `PostToolUse` hook returning
  `additionalContext`** — the channel that is model-visible *by contract*. A thin shim over
  the shared core (ADR-0004 discipline: Claude Code and Codex run the same core function;
  each runtime gets its own I/O shim only).

We deliberately do **not** piggyback mail on the existing "allow + soft reminder"
`permissionDecisionReason` path: it conflates mail with permission semantics, and an
allow-reason's visibility to the model is a harness implementation detail, not a contract.

### 2 — Delivery is evidence, not handling (the receipt grows a middle fact)

- The inbox fetch grows a `mark_delivered=true` mode: the server stamps `delivered_at`
  when a boundary drain picks a message up. A message is injected **at most once** (the
  dedupe codor gets from `--consume`, without destroying the record).
- Ordinary messages are **not auto-acked on injection**. (FR-14's auto-ack of stop signals
  stands — the deny proves the runtime acted.) A `requires_ack` message is injected with
  the instruction to call `ack_message`; **handling proof remains the ack**.
- `switchboard.message_delivery_receipt.v1` gains an additive `runtime_injected` fact:
  `mailbox.stored → runtime_injected → acked`. No version break. The receipt gets *more*
  honest, not less: injection proves the runtime was handed the message at a boundary,
  and still does not prove handling.

### 3 — Context budget and envelope

- Per boundary: at most **3 messages**, each body capped (a few hundred chars), ordered by
  priority then age, with an overflow line ("N more pending — call `inbox`").
- Every injected message is wrapped in a provenance envelope — sender, task, sent-at —
  framed as **data from a peer, not instructions from the operator**. Mid-turn injection
  is a prompt-injection surface: the scoped inbox already limits senders to authenticated
  project principals, and the envelope framing is the second layer. When session-graded
  identity lands, its verification grade is displayed in this envelope.

### 4 — Capability honesty

- `register_agent`'s `control_json` grows a `boundary_delivery` capability. Only adapters
  that actually run the boundary drain advertise it.
- Delivery-receipt wording (AGENT-HOST-SPEC §10 table) then distinguishes "active session,
  delivery at next **guarded tool boundary** (seconds)" from "active session, delivery at
  next session start (unbounded)". Today's wording papers over that difference.
- "Guarded boundary" is the honest term: the `PreToolUse`/`PostToolUse` matcher covers
  side-effecting tools (`update_task|Bash|Edit|Write|NotebookEdit`), not every read. A
  read-heavy agent gets mail slightly later; widening the matcher would tax every search
  call for no coordination win. Spec language that implies "every tool call" is corrected
  to "guarded boundary".

### 5 — Fail-open, always

Injection must never block or delay a tool call. The shared core already returns allow on
any transport failure; the mail path inherits that. A failed injection leaves the message
undelivered (no `delivered_at`), so it is retried at the next boundary — no loss.

## The subtraction (ADR-0006 Decision 2, answered at the chokepoint)

**Killed:** the planned event-subscriptions push channel for *agent* recipients —
`MULTI_AGENT_COORDINATION.md` §3.2's scheduler+notify (Slack/Gmail) delivery of
`message.received`. Boundary injection **is** the push, with better latency and no external
channel dependency. §3.2 survives only for *human* notification. This is the best kind of
subtraction — roadmap, not code: the mechanism dies before it is built.

**Narrowed:** the task-comment visible fallback for directed messages is limited to
absent-recipient cases (no active session and no wakeable host); an actively-draining
session no longer needs a comment shadow.

Net mechanism count: flat to negative.

## Documentation placement (what changes where)

| Doc | Change |
|---|---|
| `IXP-SPEC.md` §7.4 | Normative: ordinary-message boundary drain, `delivered_at` fact, and a MUST-NOT rule — implementations must not claim delivery finer than the guarded boundary, parallel to the existing boundary-latency honesty rule |
| `AGENT-HOST-SPEC.md` §10 | New wording rows keyed on the `boundary_delivery` capability |
| `MULTI_AGENT_COORDINATION.md` §3.2 | Superseded-for-agents banner pointing here |
| `MCP.md` | Tool reference, when the MCP `inbox` tool lands (open question below) |
| ADR-0006 | **No amendment.** 0006 is the stop-condition charter, not a mechanism registry; this ADR is the citation the SESSION-12 reviewer needs |

## Build plan (small slices, in order)

1. **Server:** `delivered_at` column + `mark_delivered` param on `/ixp/v1/inbox`; additive
   `runtime_injected` receipt fact.
2. **Shared core:** split the existing FR-14 fetch — stop-class → deny (unchanged);
   ordinary → budgeted, enveloped injection payload.
3. **Claude Code adapter:** `PostToolUse` shim + `settings.json` hook entry.
4. **Capability:** `boundary_delivery` in `control_json`; receipt wording keyed on it.
5. **Docs:** the placement table above; strike §3.2's agent push from the roadmap.
6. **Codex adapter parity** when its boundary surface supports it (same core, own shim).

## Alternatives rejected

- **Piggyback on `PreToolUse` allow + `permissionDecisionReason`.** Plumbing exists (the
  guard's soft reminders use it) but it welds mail onto permission semantics, and
  model-visibility of allow-reasons is not contractual. Mail gets its own channel.
- **Auto-ack on injection (codor's `--consume`).** Collapses delivered-and-handled into one
  step. Our receipt vocabulary exists precisely to keep those separate; injection becomes a
  middle fact, not a terminal one.
- **Widen the matcher to all tools.** A board round-trip on every `Grep` to shave minutes
  off mail latency for read-heavy agents — cost out of proportion to the win.
- **Build §3.2's push channel instead.** External delivery dependency (Slack/Gmail), worse
  latency than the boundary the agent already crosses, and a net-new mechanism where the
  subtraction rule demands a deletion.

## Open questions

- **MCP `inbox` tool conformance** (P0-SPEC names `inbox` with `signal`/`unacked` params;
  the MCP surface has only `list_unacked_messages` without them). Related, separate slice —
  this ADR's drain rides REST and doesn't wait for it.
- **Redelivery policy:** a message injected but never acked is currently done (delivered,
  unhandled, visible in the receipt). Should high-priority `requires_ack` messages re-inject
  after N boundaries or a TTL, or is the existing ack-deadline monitor the only escalation?
  Start with monitors only; revisit with usage data.
- **Context compaction:** an injected-but-compacted-away message is delivered by our
  definition. The receipt's `runtime_injected` wording must not overclaim ("handed to the
  runtime at a boundary"), and the ack remains the only proof that survives compaction.
- **Codex boundary surface:** which Codex hook (if any) is the `PostToolUse` equivalent;
  until one exists, Codex honestly advertises no `boundary_delivery` and keeps
  session-start semantics.
