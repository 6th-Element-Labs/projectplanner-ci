# ADR-0008: Three-plane separation for capacity, communication, and coordination

- Status: **Accepted** by the operator
- Date: 2026-07-23
- Board authority: **decision-849**, which supersedes decision-845
- Amends: decision-779 (DHCP-style execution leases)
- Companions: [ADR-0006](0006-control-plane-done-enough.md) (subtraction rule), [ADR-0007](0007-application-shell-cleanup.md) (application shell)

## Decision

Switchboard has three independent control planes. Each plane has one job and no
authority over either of the other two:

1. **Capacity** owns physical execution presence and process lifecycle.
2. **Communication** owns message storage, delivery, acknowledgement, and
   delivery truth.
3. **Coordination** owns scoped work outcomes from explicit Start through
   verified Done.

An execution lease is not a coordination scope lease. A coordination scope
lease is not execution presence. A message or acknowledgement is neither.
No row, timeout, or inference in one plane may impersonate authority from
another plane.

## Why this is necessary

Desktop-only operation worked because one human supplied all three functions.
When communication and coordination services were added, their authorities were
not separated:

- message acknowledgement timeout could lead to runner lifecycle action;
- process cleanup interpreted claims and task state;
- schedulers used private liveness lists that could not see desktop work;
- raw wake and worker-pull paths bypassed the canonical start transaction;
- a project-wide coordinator could drive work outside an explicit task or
  deliverable Start;
- multiple components could initiate review, remediation, or merge.

The recurring failure was not insufficient coordination. It was overlapping
authority.

## Plane 1: capacity

### Responsibility

The capacity plane answers one question: **which physical execution generation
is alive?**

Every managed runtime, whether desktop, Mac Agent Host, Plan VM, AWS host, or
provider-native execution, registers in the canonical execution-lease registry.
Renewal is a heartbeat. Lack of renewal expires the lease.

### Binding rules

#### C1. One physical execution registry

`runner_sessions` is the canonical execution-lease registry. Claims, Work
Sessions, messages, wake intents, agent registrations, and coordination scopes
are not execution liveness and may not be unioned into it.

Every managed execution has a server-owned identity before wake visibility:

- execution ID;
- transactional monotonic generation;
- role and optional head SHA;
- owning host and assignment;
- fence epoch;
- heartbeat interval, TTL, and expiry;
- lifecycle state.

Desktop work obtains and renews the same real execution lease through
`start_task`; it is not represented by a fabricated advisory runner.

#### C2. One automatic stop executor

Only execution-lease state may cause an automatic managed-process stop. A lease
becomes due in exactly two ways:

1. heartbeat renewal stops and TTL expires; or
2. the exact execution holder explicitly surrenders the lease at a role
   boundary, which fences the generation and makes the lease due immediately.

Both cases are consumed by the same capacity-plane lease reaper. No task status,
claim status, message timeout, credential watcher, coordinator, steward, or UI
projection may kill a process directly.

The surviving exceptions are explicit, audited operator Kill authority and
fail-closed cleanup of a process whose spawn or registration never completed.
All other stop paths are deleted or translated into an execution-lease
transition.

#### C3. Completion is surrender, reap, acknowledgement, then review

For a managed implementation generation, `complete_claim` does not kill the
process and does not immediately expose In Review. It performs the following
durable protocol:

1. authenticate and resolve one exact implementation execution identity;
2. preserve completion evidence in the canonical Task Execution run;
3. mark `review_handoff=pending` while retaining exclusive task ownership;
4. increment the execution fence, revoke the exact session authority, and make
   the execution lease due now;
5. return an idempotent Stopping receipt.

The capacity reaper then stops only that supervised generation. The owning
enrolled host persists a stop receipt before process action and acknowledges the
exact runner, generation, and fence epoch. The host acknowledgement and the
existing canonical completion finalizer commit atomically. Only that transaction
may complete the claim and Work Session and expose In Review.

Host outage, kill failure, network loss, or acknowledgement loss remains visibly
Stopping. A late heartbeat or token from the fenced generation is refused.

`switchboard.connect.assignment.v1` remains byte-compatible during rollout.
Server-owned lifecycle identity is carried in a sibling policy object until all
eligible hosts advertise the required capability.

## Plane 2: communication

### Responsibility

The communication plane stores messages, reports honest delivery state, carries
acknowledgements, and surfaces communication failures.

### Binding rules

#### M1. A message has zero lifecycle authority

A message timeout may:

- record an audit fact;
- notify the sender;
- place a finding on the operator surface;
- leave a required handoff visibly pending or blocked with a reason.

It may not wake, start, retry, restart, supersede, fence, revoke, surrender, or
stop any execution or coordination lease. It may not dispatch remediation or
merge work.

If a recipient is offline, the receipt says stored and unreachable. A scoped
coordinator may separately decide to request capacity through `start_task`.
The communication implementation itself never performs that action.

#### M2. Delivery state is explicit and terminal

Mailbox storage is not delivery, and delivery is not handling. Durable states
and receipts distinguish at least stored, runtime-online, delivered, acked,
timed-out, cancelled, and superseded outcomes.

Timeout monitors terminate. They do not create recursive acknowledgement-required
notices or immortal fired rows. Duplicate timeout and acknowledgement handling is
idempotent. A late acknowledgement from a stale recipient generation or stale
coordination-scope fence is audit-only and cannot resume current work.

Deadlines must account for advertised polling cadence and startup margin, but a
bad deadline can only produce a bad delivery expectation, never a lifecycle
effect.

#### M3. Mailbox hygiene is observable

Runners drain their mailbox as part of their work loop. The operator UI shows
unacknowledged count and oldest age. A stale mailbox is an honesty signal and
never a dead/offline inference.

## Plane 3: coordination

### Responsibility

The coordination plane drives an explicitly started task or deliverable from
Not Started through review, remediation, merge, reconciliation, and proven Done.
It reads capacity state and uses communication, but owns neither.

### Binding rules

#### W1. One door into capacity

UI, desktop MCP, scheduler, remediation, review, and coordinator starts all use
`start_task`. A coordinator requests capacity; it does not construct wakes,
select a process, or boot a runner directly.

#### W2. Drive authority requires a fenced scope lease

The existing `autopilot_scopes` substrate becomes the one coordination-scope
lease authority. No second scope table or daemon is introduced.

A live scope lease contains an exact tuple:

- scope type and durable scope ID;
- current lease ID;
- task or deliverable target;
- holder coordinator identity;
- generation and fence epoch;
- heartbeat, expiry, and lifecycle status;
- explicit operator Start provenance.

Every coordinator-originated work-driving write cites this tuple. The server
atomically validates the holder, current fence, status, expiry, and exact target
membership before side effects. Missing, expired, paused, stopped, superseded,
closed, stale-generation, wrong-owner, and out-of-scope writes fail closed.

Reads may be global. Writes may not. Exactly one current holder exists for a
scope. Takeover increments the fence epoch, so a stale coordinator cannot write.
Coordinator process loss permits fenced takeover of that scope and never changes
or kills task-runner execution leases.

Task or deliverable Start acquires the scope. Closure, explicit Stop,
supersession, or expiry releases it. Concurrent scopes cannot cross-write.

#### W3. The roaming daemon is a janitor, not a coordinator

The global daemon has a narrow bookkeeping allowlist:

- sweep expired wakes and leases;
- reconcile already-authoritative GitHub/provider provenance;
- regenerate briefs;
- publish honesty findings.

It may not call `start_task`, inject or send work instructions, dispatch review
or remediation, retry work, queue or merge a PR, advance completion phases or
Done, surrender an execution lease, or control a runner.

A janitor finding can lead to an operator Start or a scoped coordinator decision.
It is never itself work authority.

#### W4. One completion and merge owner

Task Execution's durable completion run is the sole owner of review,
remediation, exact-head validation, merge queueing, reconciliation, and Done.
Implementation, `review_merge`, and remediation use fresh execution generations
through `start_task`. Exactly one `review_merge` generation may convert passing
evidence into a queued merge.

Outcomes are `merged(provenance)` or `blocked(reason)`. A blocked result remains
first-class task and operator state and may create a fresh remediation round on
the original task through the same door.

## Authoritative roadmap

Board decision-849 and the following dependency graph implement this ADR:

1. **BUG-155**: exact execution identity, completion surrender, Stopping, owning
   host acknowledgement, and atomic canonical finalization.
2. **SIMPLIFY-20**: enable the one lease clock on capable hosts and delete every
   rival automatic stop path.
3. **SIMPLIFY-18**: make `runner_sessions` the one physical execution registry
   and migrate every liveness reader.
4. **SIMPLIFY-15**: make Task Execution the one completion owner, enforce fenced
   coordinator scope leases, and demote the global daemon to janitor.
5. **SIMPLIFY-16**: prove four tasks hands-off across host and coordinator
   restarts, concurrent scope isolation, and exact merge provenance.
6. **SIMPLIFY-11**: delete all compatibility paths, side doors, private readers,
   old stewards, and superseded documentation after proof passes.

**SIMPLIFY-21** implements the communication-only boundary in parallel and is a
hard dependency of SIMPLIFY-16.

The serial execution chain is:

`BUG-155 -> SIMPLIFY-20 -> SIMPLIFY-18 -> SIMPLIFY-15 -> SIMPLIFY-16 -> SIMPLIFY-11`

## Acceptance

SIMPLIFY-16 is the release criterion. The architecture is not accepted because
a stop was requested, a PR merged, or a dashboard looks correct. It is accepted
only when recorded evidence proves:

- four tasks reach canonical Done without manual instructions, merge nudges, or
  runner kills;
- every implementation process, PTY, token, execution lease, and Fleet-live
  projection is terminal before In Review;
- review and remediation use fresh role generations;
- exact PR-head and merge-group checks bind to the correct SHAs;
- at least two concurrent coordinator scopes cannot cross-write;
- coordinator takeover is fenced and restart-safe;
- communication timeout, duplicate timeout, late acknowledgement, offline
  recipient, and monitor restart produce no execution or scope-lease mutation;
- every timeout monitor reaches a terminal state;
- the janitor action census contains zero work-driving actions;
- canonical merged SHA and Done provenance reconcile exactly once.

## Subtraction ledger

No new coordination mechanism ships without naming what it deletes:

- BUG-155 replaces immediate In Review and terminal-task inference with the
  exact hard handoff.
- SIMPLIFY-20 deletes rival clocks and direct automatic killers.
- SIMPLIFY-18 deletes private liveness checks and advisory/fabricated runners.
- SIMPLIFY-21 deletes message-timeout wake and lifecycle recovery actions.
- SIMPLIFY-15 deletes project-wide work-driving loops and competing completion
  and merge owners.
- SIMPLIFY-11 deletes rollout flags, legacy side doors, compatibility paths,
  obsolete tests, and stale documentation after the hands-off proof.

## Non-goals

- This ADR adds no daemon, liveness-union table, or competing timer.
- It does not make communication responsible for waking an offline recipient.
- It does not let a coordination scope lease represent process liveness.
- It does not let an execution lease authorize task or deliverable writes.
- It does not change remediation from a new round on the original task.
- It does not declare current draft implementations compliant without the full
  acceptance evidence above.
