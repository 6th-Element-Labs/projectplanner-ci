# Runner lease orphan recovery

- **Status:** Scoped on board (ADR-8 minimal cut); capacity half landed, coordination
  handoff remains tracked
- **Date:** 2026-07-26 (rescoped same day after ADR-0008 review)
- **Board:** `project=switchboard`
  - Deliverable: `runner-lease-orphan-recovery`
  - **Capacity:** `ADAPTER-35` — Done via PR #932
  - **Coordination↔capacity handoff:** `COORD-73` — tracked on PR #935
  - Archived over-scope: `COORD-72`, `COORD-74`, `COORD-75` (ops/verify/tip-split were plan theater)
- **Evidence:** `docs/AUTOPILOT-BREAKDOWNS.md` BREAKDOWN 21; COORD-57 board investigate note
- **ADRs:** [ADR-0008](../../decisions/0008-three-plane-separation.md), [ADR-0006](../../decisions/0006-control-plane-done-enough.md) Decision-2

## Name

**Runner lease orphan recovery** (short: **lease-orphan**).

## ADR / DHCP fit (why this is not a new plane)

DHCP boot stays:

```text
armed scope → start_task → runner (lease + heartbeat)
```

| Plane | This work |
|---|---|
| **Capacity** | Fair renew: connect TTL=180; never silently omit a failed heartbeat. Kill clock unchanged. |
| **Communication** | Untouched |
| **Coordination** | Stop treating orphan claim as tip coverage; finish lease-death ownership unwind so dead gen cannot claim-cover a tip |

Rejected as out of scope / over-scope:

- Pid-attested grace (deferred until TTL+retry proven insufficient)
- Separate ops-playbook task (operator one-shot / lands with COORD-73)
- Separate tip-predicate task (same seam as unwind)
- Separate dogfood-verify task (acceptance on the two product tasks)
- Soft / advisory leases (Approach C)

## Problem

COORD-57 (2026-07-26): connect fanout + `heartbeat_ttl_s=60` + missed/failed renew → capacity correctly killed the runner; coordination left active claim + WS and Autopilot tip sat covered (`waiting` / 0 candidates).

That is an **ADR-0008 violation in tip selection** (claim impersonating capacity liveness), plus an incomplete lease-death handoff (WS archive only when `metadata.work_session_id` is set).

## Decision

Two product tasks only:

1. **ADAPTER-35 (capacity hygiene)** — connect register `heartbeat_ttl_s=180`; on `heartbeat_runner_session` failure retry once and always emit `runner_heartbeats` entry (`renewed: false` + error); never silently omit. Host bundle cut after merge.
2. **COORD-73 (coordination reads capacity)** — on `runner_lease_expiry` terminal ack: revoke that generation's claim; archive WS even when `metadata.work_session_id` is null; Autopilot tip covered iff `task_has_live_execution` or start/wake in-flight; reason_code `orphan_claim_after_runner_lease_expiry`. Dogfood closes the COORD-57 investigate note.

## Goals

- Dead runner generation ⇒ no active claim covering the tip.
- Tip covered only by live execution (or in-flight start).
- Connect multi-wake fanout survives brief IXP blips at least as well as direct_task renewals.
- No new coordination mechanism, steward, soft lease, or Autopilot.

## Non-goals

- BREAKDOWN 26 Dropbox cwd
- BREAKDOWN 21 progress watchdog for silent live PTY
- Soft leases / host-attested liveness redesign
- Automated ADR scoping gate (separate product gap — see below)

## Testing / acceptance

**ADAPTER-35**

- Unit: connect register advertises 180.
- Host test: failed POST still listed + retried.

**COORD-73**

- Server: lease-expiry with `work_session_id=null` + active claim/WS clears both.
- Server: does not revoke a claim owned by a different live generation.
- Autopilot: expired runner + active claim → next tip tick clears/redispatches, not wait/0.
- Dogfood: COORD-57 note closed with evidence after host bundle cut.

## Rollout

1. ADAPTER-35 has landed; cut/install its Agent Host bundle.
2. Land COORD-73 (server + tip predicate).
3. One-shot revoke any pre-fix stranded tips if still wedged; prefer natural clear via COORD-73 once live.
4. Close the COORD-57 investigate note.

## Success criteria

- Replaying COORD-57 shape cannot leave In Progress + active claim with no live runner.
- Autopilot redispatches after lease death without human claim archaeology.
- Board scope stays two plane-aligned tasks (capacity + handoff), not a five-ticket program.

---

## Appendix — Why MCP did not force this cut at scoping time

Observed gap (2026-07-26): agents can `create_deliverable` + `create_task` × N + `link_tasks_to_deliverable` with no check against ADR-0008 plane mapping, ADR-0006 subtraction, DHCP layering, or task-count budgets.

What exists today:

| Surface | What it enforces | What it does not |
|---|---|---|
| `create_task` | Bound actor, workstream, UI-impact classification | Plane ownership, ADR citation, subtraction, tip count |
| `create_deliverable` | Field shape / optional intake for in_progress | Architecture policy on linked task fan-out |
| `propose_deliverable_breakdown` | Milestone/task JSON shape, `classify_task` UI impact | ADR-0008 / DHCP / “is this a new mechanism?” |
| `policy_constraints` on deliverable | Stored text | Runtime ratchet on create/link |
| `list_decisions` / `get_decision` | Readable ADR-lite corpus | Not consulted by create/link tools |
| ADR-0006 Decision-2 | Explicitly **review policy at SESSION-12 PR chokepoint**; “deliberately not automated” | Scoping-time MCP writes |

So over-scoping is currently a **human/agent judgment failure**, not a tool refusal. SESSION-12 may catch “new mechanism” at PR time; it does not prevent five Ready tasks from landing on the board first.

If we want MCP to force architecture at scoping, that is a **separate** subtraction-friendly product change (e.g. breakdown/create_task requires `plane=` ∈ {capacity, communication, coordination} + refuse ops/verify-only tasks as deliverable blockers, or require an ADR id with a one-line “what this subtracts”). Do **not** fold that into lease-orphan — it would recreate the over-scope problem this appendix documents.
