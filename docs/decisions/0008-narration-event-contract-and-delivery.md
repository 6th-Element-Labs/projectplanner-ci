# ADR-0008 — Narration event contract and delivery semantics

- **Status:** Accepted contract; implementation staged in NARRATE-8 through NARRATE-14
- **Date:** 2026-07-11
- **Owner:** Narrative / Control Plane
- **Task:** NARRATE-7
- **Depends on:** NARRATE-2, BUG-44
- **Executable contract:** [`narration_events.py`](../../narration_events.py)

## Context

The current narrator is useful but its trigger is not transactional or truly event driven.
NARRATE-2 added `pending_narrations`, keyed by `task_id`, and `create_task` / `update_task`
enqueue a marker only after the task transaction commits. That keeps LLM work outside the request
path and collapses a burst, but a process crash between the domain commit and the second queue
transaction can lose the intent. The marker also retains only the latest task status, not the exact
source revision, cause, authorization decision, trace, or attempt history.

NARRATE-3 refreshes deliverable headers by computing every deliverable's mission status and then
comparing its brief fingerprint. In production this full scan ran on every nominally idle 45-second
timer cycle and saturated a CPU, starving web reads. BUG-44 repaired the immediate failure by:

- using `pending_narrations` as a cheap project-level dirty signal before scanning deliverables;
- scheduling with `OnUnitInactiveSec=45s`, so an overlong drain is not immediately due again;
- bounding the one-shot service with low CPU/I/O priority, a 50% CPU quota, and a five-minute
  timeout.

Those protections remain hard constraints. The event-driven replacement must not restore an idle
full-board scan, a continuously due timer, a synchronous provider call, or background work with the
same priority as interactive web/MCP traffic.

## Decision

We will use a per-project **transactional outbox** containing strict
`switchboard.narration_requested.v1` envelopes. A meaningful task or deliverable mutation and its
narration intent commit in the same SQLite transaction. Commit means both exist; rollback means
neither exists. A post-commit wake is only an acceleration signal. Durable outbox state, not the
wake and not a timer tick, is the source of pending work.

Delivery is **at least once**. Effects are idempotent and publication is compare-and-swap guarded by
the entity revision and source hash. The provider may be called more than once across a crash, but
one request revision cannot publish more than one visible current narration, and an older result
can never overwrite a newer entity revision.

### Event envelope

The executable shape is enforced by `validate_narration_requested()`:

| Field | Contract |
|---|---|
| `schema` | Exactly `switchboard.narration_requested.v1`. Unknown fields fail closed; evolution uses a new schema version. |
| `event_type` | Exactly `narration_requested`. |
| `event_id` | Globally unique immutable request-event identifier. |
| `project` | Owning project/SQLite partition. It must match both the caller's selected project and the authorization receipt. |
| `entity_type`, `entity_id` | `task` or `deliverable`, plus the project-local entity id. |
| `source_revision` | Monotonic integer for material narration source state, starting at 1. |
| `source_hash` | `sha256:` hash of the canonical source snapshot used for generation. |
| `causal_event` | Immutable domain event id, kind, occurrence time, and optional actor id that caused this request. |
| `priority` | `low`, `normal`, `high`, or `urgent`; priority changes scheduling, never authorization or freshness checks. |
| `requested_at` | Positive Unix timestamp at outbox insertion, not provider start time. |
| `dedupe_key` | `nrq:` SHA-256 derived from project, entity, revision, source hash, and causal event id. Caller-supplied mismatches are rejected. |
| `supersedes` | Null, or the older event id and older source revision this event replaces. It cannot point sideways or backwards. |
| `attempt` | Durable state, count, next availability, lease, and last error. Attempt metadata is mutable delivery state; immutable event fields never change. |
| `authorization` | Principal, authorization-decision id, `narration:request` scope, and project captured at the domain mutation boundary. |
| `trace_id` | Correlates mutation, outbox row, wake, lease attempts, provider receipt, publication, and operator surface. |

The canonical source snapshot is not arbitrary row JSON. It is a versioned projection containing
only inputs that can materially change the narration:

- task: title/description/deliverable/exit criteria, status, relevant dependency state, terminal
  provenance, and the selected activity cursor;
- deliverable: the structured mission-brief inputs and linked-task state used by
  `mission_narrative.build_mission_brief()`.

NARRATE-8 must centralize this projection and revision bump beside meaningful-change detection.
Cosmetic/read-only operations do not bump the revision or emit an event. A projection change
without a revision bump is a contract violation; an equal revision with a different hash is a
`revision_collision` and fails closed.

### Transactional boundaries

There are three boundaries, and no provider/network work occurs inside any SQLite transaction:

1. **Emit transaction.** Authorize the domain mutation, update the entity and activity, increment
   its narration-source revision, compute the canonical source hash, and insert the outbox row.
   The unique dedupe key makes a retried domain write idempotent. Commit both or neither.
2. **Claim transaction.** Select available work in project/entity/revision order, atomically change
   `pending` or expired/retryable work to `claimed`, assign a worker and bounded lease, increment
   the attempt count, and commit. Provider generation happens after this transaction closes.
3. **Publish transaction.** Persist a generation receipt first, then reread the current entity
   revision/hash. Publish only when both match the request and the visible-effect key has not
   already been applied. Otherwise mark the attempt `superseded`; preserve the receipt and do not
   replace current prose.

The wake signal is sent after boundary 1 commits. Wake failure cannot roll back the committed
mutation and does not lose work. The recovery sweep later finds it.

### Attempt state machine

```text
pending -> claimed -> delivered
              |  \
              |   -> superseded
              -> retry_wait -> claimed
                     |
                     -> dead_letter
```

- A worker crash leaves `claimed` work recoverable only after its lease expires.
- Retry delay uses bounded exponential backoff with jitter and retains the original error.
- Malformed, unauthorized, cross-project, or repeatedly failing poison work becomes queryable
  `dead_letter`; it is never silently dropped or presented as success.
- A stale revision becomes `superseded` before provider work when possible, and at the publish
  compare-and-swap when the entity changes during generation.
- Manual retry and narrate-now create or reactivate work through authorized, audited APIs; they do
  not mutate immutable event fields or bypass dedupe, freshness, rate, token, or cost policy.

### Ordering, coalescing, and idempotency

There is no global ordering promise. Within `(project, entity_type, entity_id)`, source revision is
the order. Workers may process different entities concurrently, but only one live lease may own a
specific request. If revision 12 is available while revision 11 is pending, revision 11 may be
marked superseded without a provider call. A lower revision received after a higher current entity
revision is a stale event, not executable work.

At-least-once duplicates are recognized by `event_id` and `dedupe_key`. Visible publication uses a
separate unique effect key over project/entity/revision/source hash. Therefore:

- duplicate emit or wake: one outbox intent;
- duplicate claim after lease recovery: possibly another provider attempt, one visible effect;
- crash after provider success but before receipt: a repeat charge is possible and must be visible
  in receipts/cost accounting; it still cannot duplicate or regress the visible narration;
- current entity changes during the call: receipt retained, result suppressed as superseded.

### Authorization and project isolation

The authenticated mutation path, not the event body, is the authority. It derives `project` from
the selected database and records a trusted authorization decision id. External callers never
insert outbox rows directly. Every write and consume boundary passes an explicit expected project;
the envelope project, DB partition, authorization project, entity lookup, and output target must
all agree. A mismatch is `cross_project`, is not retried as ordinary provider failure, and emits a
security/audit signal.

System-generated events use a registered system principal with the same `narration:request` scope.
Operator narrate-now/retry checks current permission again and records a new decision; possession
of an old event is not ongoing authority.

### Timer and recovery policy

Normal flow is commit -> wake -> claim. The timer is **recovery only** and runs a cheap indexed
query for pending, retry-ready, or expired-lease rows. It must not enumerate every project entity,
hydrate mission status, or call the provider when there is no durable work. Recovery cadence may
be slower than the current 45-second primary poll (target five minutes) because the wake path owns
freshness. BUG-44's `OnUnitInactiveSec`, service priority/quota, and finite timeout remain until
equivalent worker-level isolation is proven.

### Retention

Cleanup is bounded, indexed, batched, and lower priority than interactive traffic:

- delivered requests and generation receipts: 30 days online;
- superseded requests with no provider call: 7 days online;
- dead letters and their original errors: 90 days online;
- aggregate cost/SLO/audit records: 180 days or the project's longer governance policy;
- pending, claimed, retryable, or unacknowledged failed work: never age-deleted.

Deletion never removes the current visible narration or its source revision/hash. Legal hold or a
project retention policy can extend these periods. Cleanup itself is audited.

## Invariants

1. A committed meaningful mutation has exactly one durable request for its causal event/revision;
   a rolled-back mutation has none.
2. Narration remains derived/advisory. Status, dependencies, provenance, and progress are truth.
3. No stale or cross-project request reaches provider work or visible publication.
4. A visible narration identifies the exact project/entity/revision/source hash and generation
   receipt that produced it.
5. No provider call occurs on an interactive mutation path or while holding a SQLite transaction.
6. Delivery may repeat; visible effects and cost receipts do not silently double-count.
7. Errors, fallback text, retries, dead letters, and budget refusal remain explicit. No fallback
   overwrites the failed receipt or turns the state green.
8. Idle operation performs no project-wide entity scan and consumes near-zero CPU.

## Failure modes and required outcomes

| Failure | Required outcome |
|---|---|
| Crash before emit transaction commit | Neither mutation nor request is visible. |
| Crash after commit, before wake | Request remains pending; recovery sweep wakes/claims it. |
| Duplicate mutation retry | Unique dedupe key returns the existing request/effect. |
| SQLite busy/locked | Bounded retry/backoff; interactive traffic keeps priority; failure remains visible after budget. |
| Worker crash while claimed | Lease expiry makes work claimable; attempt history remains. |
| Provider timeout/outage/malformed response | Receipt/error retained; bounded retry, then explicit fallback or dead letter per policy. |
| Budget/rate limit exhausted | No hidden provider call; visible policy fallback/refusal with receipt. |
| Entity advances before provider call | Mark older request superseded; zero LLM charge. |
| Entity advances during provider call | Persist receipt, suppress publication at compare-and-swap. |
| Cross-project or unauthorized event | Reject/dead-letter with security audit; never retry into another project. |
| Recovery timer overlaps active worker | Atomic lease/dedupe prevents a second visible effect. |
| Backlog or poison item | Other entities continue; oldest age/dead-letter state is queryable and alertable. |

## Migration and rollout

Migration is additive and reversible:

1. **NARRATE-8:** add entity source revisions, the outbox/attempt schema, indexes, validator, and
   atomic emitters. Continue writing the legacy `pending_narrations` marker after commit during
   shadowing. Backfill current revisions/hashes without emitting historical provider work.
2. **NARRATE-9/10:** run the wakeable worker in shadow mode. Compare legacy and event-driven impact
   sets, ordering, coalescing, and stale suppression; do not publish from both paths.
3. **NARRATE-11/12:** make task/deliverable generation consume immutable snapshots, attach receipts,
   and enforce deterministic/LLM selection plus project budgets.
4. **NARRATE-13:** expose queue age, attempts, leases, errors, dead letters, receipts, cost, and
   authorized controls.
5. **NARRATE-14:** canary publication by project, switch primary triggering to outbox wake, retain
   legacy dual-write for a soak, and reduce the systemd timer to recovery-only duty.

Rollback stops the event worker and flips the per-project primary-path flag back to the legacy
queue/timer. During soak, legacy markers remain available. Outbox rows and receipts are preserved
for audit and later replay; rollback never drops the additive tables or decrements entity revisions.
After the soak, removal of legacy polling is a separate reviewed migration, not part of this ADR.

## SLOs and alerts

- **Durability:** zero committed meaningful mutations without a matching outbox intent.
- **Freshness:** p95 current narration published within 60 seconds of `requested_at` under the
  agreed production load; queue oldest-pending age alerts at 60 seconds.
- **Wake:** p95 committed request visible to an eligible worker within 5 seconds when a worker is
  healthy; wake loss is recovered within one recovery sweep.
- **Correctness:** zero stale, revision-colliding, unauthorized, or cross-project visible writes;
  zero duplicate visible effects for one effect key.
- **Interactive isolation:** mutation paths perform no LLM/network call; narration does not regress
  web/MCP latency or SQLite lock/error rate from the pre-rollout baseline.
- **Idle cost:** zero provider calls and near-zero worker CPU when the outbox has no actionable row;
  no timer-led project-wide deliverable scan.
- **Auditability:** 100% of provider attempts have a trace-linked receipt recording outcome,
  latency, model/prompt version, tokens/cost when known, and fallback/error reason.

## Contract fixtures

`fixtures/narration_events/` contains one accepted task event and negative fixtures for the three
exit-criteria failures: malformed shape, cross-project routing, and revision regression.
`test_narration_event_contract.py` also proves deterministic hashing/dedupe, authorization project
binding, equal-revision hash collision rejection, supersedes ordering, lease consistency, strict
unknown-field rejection, and stale classification. The test is part of `scripts/switchboard_ci.sh`.

## Alternatives rejected

- **Keep the 45-second timer as primary.** It cannot meet the idle-resource requirement and repeats
  the causal shape of BUG-44.
- **Best-effort post-commit enqueue.** Simpler, but the crash gap can lose narration intent.
- **Exactly-once provider delivery.** Not available across SQLite, process crashes, and external
  providers. At-least-once plus idempotent publication is honest and sufficient.
- **Global event ordering.** Unnecessary contention. Narration only requires monotonic per-entity
  ordering and stale suppression.
- **Trust event-supplied project/authorization.** Violates the Project authority boundary. The DB
  partition and authenticated authorization decision must agree independently.
