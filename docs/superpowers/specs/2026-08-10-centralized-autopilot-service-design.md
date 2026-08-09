# Centralized Autopilot Service

- **Status:** Approved design
- **Date:** 2026-08-10
- **Board task:** `ARCH-MS-128` on `project=switchboard`
- **Architecture:** [ADR-0008/three-plane](../../decisions/0008-three-plane-separation.md),
  [ADR-0025/service-extraction](../../decisions/0025-bounded-context-service-extraction.md)
- **Runtime contract:** Mission Bot V4 and Task Execution

## Problem

Autopilot has repeatedly worked, then regressed when a local policy or technical condition
quietly acquired lifecycle authority. The July 2026 remediation-round budget was one example:
valid review findings reached a fourth automatic remediation round, and a factory rule converted
that ordinary work into Human state. Scattered unit tests allowed a later change to preserve its
local behavior while breaking the end-to-end Mission Bot contract.

The durable correction is not another retry daemon, policy engine, or scheduler. Switchboard must
centralize the existing Task Execution and Mission Bot V4 completion owner behind one bounded,
event-driven service and make every other component submit facts rather than write Autopilot
lifecycle state.

## Decision

Extract the existing Task Execution / Mission Bot V4 completion owner into one centralized
Autopilot service. The service remains the ADR-0008 W4 owner; process extraction changes deployment
topology, not authority.

The service exclusively decides and records:

- the next execution role for an explicitly started scope;
- implementation, review, remediation, merge, and reconciliation continuation;
- exact-head admission and replacement;
- automatic remediation of CI, security, permission, conflict, and review findings;
- Human parking from an authenticated `agent_requires_human` receipt only; and
- terminal Done from canonical merge provenance and reconciliation only.

GitHub adapters, UI, MCP, project coordinators, hosts, and runners may publish facts or request an
existing typed action. They may not independently retry, escalate, merge, mark Done, or write
Autopilot lifecycle state.

## Non-goals

- No fourth ADR-0008 plane or second completion owner.
- No new project-wide scheduler, retry daemon, or host selector.
- No removal or weakening of authentication, authorization, tenant isolation, CI, review,
  Work Session, exact-head, or merge gates.
- No provider-specific Codex, Claude Code, or Cursor lifecycle branches.
- No project flags, remediation budgets, attempt exhaustion, or default tenant/policy fallbacks.
- No database-backend migration authorized by this design.
- No customer-facing UI redesign.

## ADR-0008 authority map

| Plane | Existing authority retained | Service relationship |
|---|---|---|
| Capacity | `runner_sessions`, execution leases, owning host, lease reaper | Reads canonical liveness and requests work only through `start_task`; never kills a process directly. |
| Communication | durable messages, delivery, acknowledgement | Consumes communication facts; delivery state never changes mission lifecycle. |
| Coordination | explicit Start and fenced `autopilot_scopes` lease | Validates the exact scope fence before every work-driving effect. |
| W4 completion | Task Execution durable completion run | Becomes the centralized service and remains the one lifecycle writer. |

This is ADR-0008 compliant because it consolidates the existing W4 owner instead of inventing a
new authority. Capacity and Communication remain separate. The global janitor remains unable to
start, retry, remediate, merge, or park missions.

## Architecture

```text
GitHub / runners / UI / scoped coordinator
                    |
                    v
           append durable facts
                    |
                    v
      Task Execution / Mission Bot V4 service
          one reducer; one lifecycle writer
                    |
          +---------+----------+
          |         |          |
          v         v          v
       Capacity   GitHub    Task projection
       start_task merge     typed Task API
```

### Components

1. **Fact intake** validates a versioned envelope, exact project/task identity, source authority,
   sequence, idempotency key, and optional execution/scope fence.
2. **Mission journal** appends accepted facts. Replay is ordered by the server-owned sequence.
3. **Deterministic reducer** consumes one task's journal and returns one typed result: continue,
   wait, Human, or Done, with at most one effect intent.
4. **Transactional outbox** stores the effect and cursor advance atomically. Effect delivery may
   repeat; the receiving boundary and effect key make it idempotent.
5. **Effect adapters** invoke only existing authority doors: `start_task`, GitHub merge-queue
   admission, reconciliation, and the authenticated Human command.
6. **Projection query** exposes the durable current mission state without allowing callers to set
   it.

The service does not expose a generic `set_status`, `retry`, `mark_human`, or `mark_done` API.

### Operational reason for a process cut

The candidate process is justified under ADR-0025 by failure isolation and a distinct promotion
contract: a bad web/UI/board release must not replace the running Autopilot reducer, and a candidate
Autopilot release must prove shadow parity, separate-VM conformance, and a production canary before
receiving lifecycle write authority. If independence or writer safety cannot be proven, the package
remains in-process and is still the only owner.

## Data ownership

The bounded context has exclusive write ownership of:

- `mission_items` and `mission_events`;
- `completion_runs`;
- `review_verdicts` and `review_findings`; and
- `review_remediations`.

It reads, but does not own, the exact coordination scope from `autopilot_scopes` and physical
liveness from `runner_sessions`. It updates the user-facing task status through the typed Tasks
application API, never through cross-service SQL.

The mission journal and completion run remain authoritative if the Tasks projection is delayed.
Projection delivery is an idempotent outbox effect and is never read back as a lifecycle decision.

The first implementation step establishes this package/repository ownership in-process. A live
process cut is permitted only after ADR-0025 independence proof shows declared ports, safe writers,
outage behavior, parity, rollback, and no undeclared imports. A No-Go leaves the same package as
the authoritative in-process owner; it does not restore scattered writers.

## Mission Bot V4 reducer contract

For each unhandled fact sequence, the reducer emits exactly one of:

| Result | Meaning |
|---|---|
| `continue` | Request one exact implementation, `review_merge`, or remediation generation through `start_task`. |
| `wait` | A wake, runner, external check, merge queue, reconciliation, or retry timer has a durable pending state. |
| `human` | One authenticated, execution-bound `agent_requires_human` receipt names a concrete human-only decision or permission. |
| `done` | Canonical default-branch merge provenance has reconciled the task. |

The reducer has no attempt-limit input and no exhaustion output. Round number remains audit
telemetry only.

### Security and policy boundary

Security, authentication, authorization, tenant-isolation, permission, CI, review, Work Session,
and merge checks remain enabled. They may prevent an unsafe merge. They do not own Autopilot
lifecycle and therefore cannot create Human state, terminate the mission, or exhaust remediation.

An actionable failing check becomes an exact-head remediation input. A temporarily unavailable
dependency becomes `wait` plus durable automatic retry. A real decision that cannot be derived
from the assignment or persisted project policy requires the active authenticated runner to call
`agent_requires_human`.

There is no generic `Blocked -> Human` conversion.

## Data flow

1. Explicit operator Start creates or renews the fenced coordination scope.
2. GitHub, Task Execution, Capacity, and authenticated runners append facts.
3. The service locks one task cursor, validates the current scope and execution fences, and reduces
   all facts through the next unhandled sequence.
4. It commits the result, cursor, and optional effect intent in one transaction.
5. The outbox invokes the existing authority boundary.
6. The resulting capacity, GitHub, or reconciliation fact re-enters the journal.
7. Processing continues until an authenticated Human request or canonical Done.

Concurrent delivery cannot start two generations: task cursor serialization, execution generation,
scope fence, and effect idempotency all identify the one current action.

## Failure behavior

| Condition | Required behavior |
|---|---|
| Service unavailable | Facts remain queued. Processing resumes from the durable cursor. No Human request is created. |
| Capacity full or host unavailable | Remain `wait`; retry with capped exponential delay and no attempt limit. |
| CI, security, permission, conflict, or review failure | Preserve the exact finding and create automatic remediation when actionable. Never factory-create Human. |
| PR head moves | Fence the stale generation and request one fresh exact-head generation. |
| Duplicate or out-of-order delivery | Deduplicate or wait for the missing sequence; never repeat the effect. |
| Malformed event | Quarantine the event as a platform-health incident. Do not turn it into project approval or mutate the mission optimistically. |
| Service restart during effect delivery | Replay the outbox effect by stable key; receiving boundary returns the existing result. |
| Explicit authenticated Human request | Park the exact mission generation and surface the concrete request. |
| Merge provenance observed | Reconcile exactly once and project Done. |

Automatic retry uses exponential backoff only to prevent a hot loop. Backoff never becomes a retry
budget or terminal result.

## Non-bypassable enforcement

Centralization is incomplete until all of these are mechanically enforced:

1. Only the bounded repositories may write the owned tables.
2. REST, MCP, webhook, UI, coordinator, and janitor adapters call typed application ports.
3. CI rejects new direct imports or SQL writes that bypass the service boundary.
4. Only the authenticated `agent_requires_human` command can append `human_requested`.
5. Only reconciliation can append canonical Done provenance.
6. All execution starts use `start_task`.
7. The old lifecycle writers are deleted after cutover; rollback never activates a second
   implementation.

## Proof strategy

### Tier 1: every pull request

The release-blocking deterministic contract suite runs on the exact PR head and proves:

- remediation rounds 4, 10, and 100 continue automatically;
- CI, security, permission, conflict, and review findings produce remediation, not Human;
- capacity unavailable produces `wait`, then continuation;
- only authenticated `agent_requires_human` produces Human;
- stale-head replacement creates one generation;
- duplicate facts and service restarts do not duplicate starts or merges; and
- only canonical provenance produces Done.

The full canonical repository gate also runs on an ephemeral CI VM. No unit test may hand-build a
finding shape that production code would never emit; fixtures use the real contract producers.

### Tier 2: shadow parity

The candidate service consumes real production facts but performs no effects. For every current
path decision it records the candidate result, reason, and effect digest. Any mismatch blocks
cutover. Shadow mode has read-only production credentials and no lifecycle write authority.

### Tier 3: separate conformance VM

A separate VM runs the existing `conformance` project and sandbox repository with its own database,
host identity, credentials, and harmless PRs. It has no authority over `switchboard`, `atlas`, or
customer repositories. Curated full-loop scenarios prove:

- clean implementation through merge and Done;
- security finding through multiple remediation rounds and Done;
- service and host restart recovery;
- capacity exhaustion followed by launch;
- exact-head movement during review; and
- idempotent merge-queue and reconciliation behavior.

### Production promotion canary

After deployment, one harmless production canary must reach canonical Done before the release is
accepted. A failed canary blocks or rolls back the platform release. It never parks customer
missions or asks a project owner to repair the platform.

## Branch and workspace policy

The design branch was created in an isolated worktree from freshly fetched canonical
`origin/master` at `6cc080445e4548c8457a07c1fabf773e3fddf234`. The dirty shared checkout and the
completed BUG-336 branch are not inputs.

Each implementation task starts from a newly fetched canonical default branch in its own isolated
worktree and uses `codex/<TASK-ID>-<slug>`. Implementation does not accumulate on the design branch.
Exact PR-head CI, merge-group CI, and canonical default-branch provenance remain required.

## Rollout and rollback

1. Establish the bounded in-process owner and delete internal bypasses.
2. Add the Tier 1 release contract.
3. Package the same owner as a candidate service behind typed ports.
4. Prove ADR-0025 independence and safe data ownership; record Go or No-Go.
5. On Go, run shadow parity with zero effects.
6. Cut one internal route so the service becomes the sole lifecycle writer.
7. Run conformance VM and production promotion canaries.
8. Soak, then delete the old process route and compatibility writers.

Rollback changes the route back to the same bounded in-process package and replays from the durable
cursor. It does not restore deleted legacy decision code, enable dual writers, waive a gate, or
reset mission state.

## Release contract

Every ProjectPlanner pull request must pass the deterministic Autopilot contract. Every service
release must pass shadow parity and the conformance VM. Every production deployment must pass the
promotion canary.

Failures block the ProjectPlanner release, not the customer mission. Security findings stay visible
and block unsafe merges, while Mission Bot continues automatic remediation without an attempt cap.

## Success criteria

1. One bounded owner and one lifecycle writer exist in source and production.
2. No machine-generated condition can append `human_requested`.
3. No remediation attempt count affects routing.
4. All security and release gates remain enabled and feed automatic remediation.
5. A service or host restart resumes from durable state without duplicate execution or merge.
6. Separate-VM conformance proves clean, remediation, capacity, stale-head, and restart scenarios.
7. Production promotion requires a successful harmless canary.
8. Rollback returns to the same bounded package without reintroducing a second authority.

## Rejected alternatives

### New decision-only policy service

Rejected because it would split decision authority from the writer, add network races, and risk a
fourth control plane.

### Shared helper library

Rejected as the final boundary because adapters and repositories could bypass it. The reducer may
be a library inside the bounded context, but the lifecycle writer must remain exclusive.

### Disable security until Autopilot is stable

Rejected because it would permit known tenant or authorization defects to merge. The conformance
project supplies a safe policy-light sandbox for lifecycle proof without weakening production
repositories.

### Big-bang process cut

Rejected by ADR-0025. Package ownership, deterministic tests, shadow parity, a reversible route,
and deletion of old writers precede physical extraction.
