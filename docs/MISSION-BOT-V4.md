# Mission Bot v4 — durable pager for scoped LLM missions

- **Status:** Implementation target; not yet the deployed control path
- **Authority:** [ADR-0008 — three-plane separation](decisions/0008-three-plane-separation.md)
- **Also constrained by:** [ADR-0006 — subtraction rule](decisions/0006-control-plane-done-enough.md)
  [ADR-0020 — enforce at dispatch, observe at merge, stamp at Done](decisions/0020-merge-gates-observe-not-enforce.md),
  and
  [ADR-0024 — current trusted CI lanes](https://github.com/6th-Element-Labs/projectplanner/blob/master/docs/decisions/0024-merge-queue-admission-and-docs-lanes.md)
- **Replaces after cutover:** the current Mission Bot fact classifier, normalization law,
  generated dossier, completion-route retries, and factory-generated human escalation
- **Does not replace:** `start_task`, `autopilot_scopes`, `runner_sessions`, execution
  generations/fences, the C3 implementation handoff, GitHub required checks and merge queue,
  or canonical merge provenance

## Decision

Mission Bot v4 is a durable pager, not a GitHub classifier.

It stores one four-state mission item and an append-only sequence of material events. A
scoped worker has one work-driving operation: copy the next role already requested by the
workflow into `Task Execution.start_task`. The newly booted LLM registers through MCP, reads
the live mission context and persisted history, and decides what to do.

Mission Bot does not decide whether CI is fixable, whether a review is sufficient, whether
evidence belongs to the newest Work Session, whether a failure needs remediation, or whether
an operator is needed. Only an authenticated LLM execution may request a human.

The complete controller rule is:

```text
scope inactive?                     -> WAIT
verified terminal provenance stored? -> DONE
dependencies unmet?                  -> WAIT
agent requested a human?             -> WAIT_FOR_HUMAN
Capacity reports a live runner?      -> WAIT
mission has an unhandled event?      -> start_task(requested_role, event_pointer)
otherwise                            -> WAIT
```

No other state or decision is permitted.

## Desired outcome

An operator starts a task once. From then until canonical Done:

1. exactly one scoped coordinator owns drive authority;
2. exactly one physical execution generation may be live for the task;
3. each fresh LLM receives only mission identity and a history pointer;
4. the LLM reads current Switchboard and GitHub facts through MCP and provider APIs;
5. implementation, review, and remediation remain distinct fenced generations;
6. material external changes wake a fresh generation when one is needed;
7. only an LLM may ask the operator a real question; and
8. only verified terminal provenance — canonical GitHub merge provenance for code or
   verifier-stamped offline evidence for non-PR work — makes the task Done.

A green, reviewed, mergeable PR cannot remain unattended indefinitely. A GitHub event wakes
the scoped mission immediately; a bounded observation backstop wakes it if a webhook is lost.

## Non-goals

V4 does not:

- diagnose CI, code, review, merge, claim, callback, host, or deployment failures;
- infer that a failure needs a human;
- copy logs or a generated diagnosis into the startup prompt;
- use messages, claims, Work Sessions, or scope leases as runner liveness;
- let an implementation generation review or merge its own handoff;
- replace GitHub branch protection or the native merge queue;
- reduce GitHub's provider fields into a new authoritative `ready_to_merge` flag;
- add a general workflow language, distributed event platform, or AI router;
- add a second coordination-scope table or a second physical-runner registry; or
- use a stored retry counter to decide lifecycle.

## The four states

`mission_items.state` has exactly four values:

| State | Meaning | Who may produce it |
|---|---|---|
| `ACTIVE` | The mission has unhandled work and needs the requested LLM role when Capacity is free. | Operator Start, an authenticated LLM yield, the fenced scoped worker after observing a material event, or recovery of an attempt that ended without a valid yield |
| `WAITING` | The latest authenticated LLM inspected through a named event cursor and chose to wait for the external world. | The exact current execution through `yield_mission(outcome="waiting")` |
| `HUMAN` | An authenticated LLM asked one explicit operator question. | The exact current execution through `agent_requires_human` |
| `DONE` | Verified terminal provenance has been persisted. | Canonical GitHub webhook/reconciliation or the existing privileged offline verifier only |

There is deliberately no `RUNNING` state. `runner_sessions` is the sole liveness authority.

There are deliberately no `WaitingForChecks`, `ChangesRequested`, `Approved`,
`MergeBlocked`, or `Remediating` states. Those are facts an LLM interprets, not coordination
states.

An explicit operator Stop, task cancellation, or closed/superseded scope makes the scope
inactive. It does not fabricate `DONE`; the retained mission row simply becomes ineligible
for drive.

## Requested roles

ADR-0008 W4 requires fresh role generations. V4 therefore retains exactly these execution
roles:

```text
implementation
review_merge
remediation
```

Mission Bot does not derive a role from CI or merge-gate findings.

- Operator Start initializes `requested_role=implementation`.
- The C3 implementation finalizer records the review handoff and sets
  `requested_role=review_merge`.
- A review LLM may request `remediation`.
- A remediation LLM that publishes a new head may request `review_merge`.
- A review LLM waiting on checks or queue state keeps `requested_role=review_merge`.

The requested role is an authenticated LLM or hard-handoff command. The worker only copies
it into `start_task`; it never substitutes a role.

Exactly a current `review_merge` execution at the assigned exact head may arm auto-merge.

## Durable data model

Use the existing project-partitioned SQLite database and backup path. Do not introduce Kafka,
DBOS, Redis, or a separate event store.

### `mission_items`

This is Task Execution's v4 durable completion row. It replaces the current live
`completion_runs` decision row after cutover; it is not a second completion owner.

```sql
CREATE TABLE mission_items (
    project_id         TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    state              TEXT NOT NULL
                       CHECK (state IN ('ACTIVE','WAITING','HUMAN','DONE')),
    requested_role     TEXT NOT NULL
                       CHECK (requested_role IN
                              ('implementation','review_merge','remediation')),
    handled_through    INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    human_request_id   TEXT NOT NULL DEFAULT '',
    terminal_kind      TEXT NOT NULL DEFAULT ''
                       CHECK (terminal_kind IN ('','github_merge','offline')),
    terminal_ref       TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (project_id, task_id)
);
```

The row does not contain runner liveness, CI verdicts, review verdicts, retry counts, or a
cached repair diagnosis.

### `mission_events`

This is durable history and the wake inbox. It has no independent lifecycle authority.

```sql
CREATE TABLE mission_events (
    event_id           TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    sequence           INTEGER NOT NULL,
    event_type         TEXT NOT NULL,
    source_plane       TEXT NOT NULL,
    occurred_at        REAL NOT NULL,
    pr_number          INTEGER,
    head_sha           TEXT,
    generation         INTEGER,
    execution_id       TEXT,
    external_ref       TEXT NOT NULL DEFAULT '',
    payload_json       TEXT NOT NULL DEFAULT '{}',
    idempotency_key    TEXT NOT NULL,
    UNIQUE (project_id, task_id, sequence),
    UNIQUE (project_id, idempotency_key)
);

CREATE INDEX ix_mission_events_task_sequence
    ON mission_events(project_id, task_id, sequence);

CREATE INDEX ix_mission_events_task_head
    ON mission_events(project_id, task_id, head_sha, sequence);
```

Sequence allocation and event append happen in one database transaction. Provider delivery
GUIDs, execution IDs, GitHub event IDs, or deterministic observation fingerprints supply
idempotency keys.

### Material events only

V4 needs only these broad event families:

| Event | Meaning |
|---|---|
| `mission_started` | An operator created or resumed explicit scoped drive authority. |
| `task_changed` | Dependencies, task requirements, or an explicit coordination command changed. |
| `github_changed` | PR, exact head, required checks, review, comments, queue, or merge changed. |
| `runner_ended` | Capacity produced an exact terminal execution receipt. |
| `agent_yielded` | The current LLM persisted its event cursor and requested next role/outcome. |
| `human_answered` | An operator answered the explicit coordination question. |
| `observation_due` | The bounded webhook-loss backstop says a fresh LLM observation is due. |
| `terminal_provenance_persisted` | The authoritative provenance service persisted a canonical merge or verifier-stamped offline completion. |

The payload may identify a changed check or review URL, but Mission Bot never interprets the
payload. Repeated identical provider observations append no event.

## GitHub and CI normalization contract

### GitHub has no `ready_to_merge` flag

This section is exhaustive for GitHub fields, enums, webhook actions, and CI states that can
change this repository's implementation-to-merge lifecycle. Metadata such as reactions,
assignees, milestones, and labels remains visible to the LLM but is not a lifecycle flag
unless a future accepted policy explicitly makes it one.

GitHub exposes several independent facts. None is a complete merge instruction:

- `mergeable=MERGEABLE` means GitHub currently sees no merge conflict.
- `mergeStateStatus=CLEAN` means GitHub currently sees a mergeable PR with passing commit
  statuses.
- `autoMergeRequest != null` means someone already armed auto-merge.
- `mergeQueueEntry != null` means the native queue owns landing.
- `mergedAt` and `mergeCommit.oid` are provider observations that still require the
  canonical provenance verifier before the board becomes Done.

`CLEAN` is therefore not an authority, `BLOCKED` is not a diagnosis, and `MERGEABLE` is not
permission to bypass review or the queue. Mission Bot v4 stores provider changes and pages
the requested LLM role. The LLM reads the raw fields, Switchboard's exact-head evidence, and
the relevant URLs, then acts through the existing fenced commands.

### One ingestion rule

For an authenticated delivery mapped to a scoped task:

```text
provider delivery is a duplicate? -> acknowledge; append nothing
provider delivery changed a fact?  -> append one github_changed event
otherwise                          -> append nothing
```

The ingestor does not change `mission_items.state`, choose a role, call `start_task`, arm
merge, or request a human. The fenced W2 scoped worker compares
`mission_items.handled_through` with the latest persisted sequence and drives the event.

A merged observation is still appended as `github_changed`. A separate canonical provenance
projector consumes the durable observation, rereads GitHub, and may persist Done. This keeps
HTTP/event ingestion append-only while preserving webhook-driven canonical reconciliation.

The event fingerprint excludes volatile delivery metadata such as delivery time and
`updatedAt`. It includes the provider object identity plus the material value, for example:

```text
pull_request:<repo>:<number>:head:<head_sha>
status:<repo>:<sha>:<context>:<state>:<target_url>
review:<repo>:<number>:<review_id>:<state>:<commit_id>
queue:<repo>:<number>:<entry_id>:<state>:<head_sha>
```

### Pull-request lifecycle fields

These are the complete current GraphQL enum values. A nonterminal row never changes board
status by itself.

| GitHub field/value | Exact meaning | Persisted observation | What v4 does | What the LLM normally does |
|---|---|---|---|---|
| `state=OPEN` | PR remains open. | `github_changed` when the material snapshot changes. | Pages the already-requested role when the scoped worker observes the event. | Inspect the live head, checks, review, comments, auto-merge, and queue. |
| `state=CLOSED` with no merge | PR was closed without merging. | `github_changed`. | Never declares Done or Human. | Determine whether to reopen, replace, remediate, or explicitly cancel/supersede the mission. |
| `state=MERGED` | GitHub says the PR closed by merging. | Raw merge observation plus a provenance-verification request. | Waits for persisted terminal provenance. | May request `reconcile_task_merge`; never stamps Done. |
| `isDraft=true` | PR is a draft. | `github_changed`. | Does not call `gh pr ready`. | Current `review_merge` LLM decides whether the PR is actually ready and marks it ready. |
| `isDraft=false` | PR is not a draft. | `github_changed` on transition. | No special transition. | Continue review/repair/merge work from live facts. |
| `headRefOid` changed | The current PR head is a new exact commit. | `github_changed` keyed to the new SHA. | Pages the requested role; old-head facts remain history. | Reread the diff and use only CI/review/findings for the new SHA. |
| `baseRefOid` changed | The target branch advanced. | `github_changed` only when needed for the mapped PR/queue observation. | Does not order a rebase. | With this repository's merge queue, do not update solely because `master` moved; the merge group proves integration. |
| `baseRefName != master` | PR targets a noncanonical branch. | `github_changed`. | Does not infer Done eligibility. | Correct the base or explain the intentional noncanonical workflow. |
| PR identity unavailable or provider read failed | Current provider facts are incomplete. | Event references the failed read; context reports `context_complete=false`. | Does not invent green, red, closed, or merged. | Retry the live read, repair access, or yield waiting for another observation. |

### PR capability and repository-policy flags

These values describe what GitHub or the authenticated actor can do. They are not proof that
the action happened.

| Field/value | Meaning | V4/LLM treatment |
|---|---|---|
| `isInMergeQueue=true` | GitHub says the PR is in a merge queue. | Cross-check `mergeQueueEntry`; wait on the entry and current merge-group evidence. |
| `isMergeQueueEnabled=true` | The base branch has a merge queue. | Use the native queue path. This does not mean this PR is enqueued. |
| `viewerCanEnableAutoMerge=true` | The current GitHub identity may arm auto-merge. | A current `review_merge` LLM may arm once when the exact-head evidence is acceptable. |
| `viewerCanEnableAutoMerge=false` | The current identity or PR state cannot arm auto-merge. | Inspect repository policy and identity. Do not wait indefinitely or create Human automatically. |
| `viewerCanDisableAutoMerge` | The identity can disable an existing request. | Informational in normal flow; v4 never disables it automatically. |
| `viewerCanUpdateBranch` | The identity can update the PR branch from its base. | Capability only. Under the current merge queue, do not update solely because the base advanced. |
| `viewerCanMergeAsAdmin` | The identity can bypass protections. | Emergency audited repair capability only; never a normal v4 instruction or a Done signal. |
| `maintainerCanModify` | Maintainers may write the contributor branch. | Repository-write capability only; never evidence, liveness, or permission to merge. |
| `locked=true` | Conversation is locked. | Expose to the LLM. It does not mean the PR is merge-locked. |
| `potentialMergeCommit` | GitHub generated a test merge commit. | Supporting provider object only. The queue's current merge-group SHA owns landing evidence. |
| `repository.autoMergeAllowed=false` | Repository policy disables auto-merge. | Page the LLM on policy change; repair authorized configuration or ask a genuine question. |
| `repository.squashMergeAllowed=false` | The required squash method is unavailable. | Page the LLM; do not silently choose another landing method. |
| `repository.mergeCommitAllowed` / `rebaseMergeAllowed` | Other merge methods are enabled or disabled. | Informational here. V4's normal landing method remains squash. |

`PullRequestMergeMethod` has exactly `MERGE`, `SQUASH`, and `REBASE`. This repository's queue
rules select `SQUASH`; the other values are not fallback choices for Mission Bot.

### `mergeable`

| Value | GitHub meaning | V4 mapping | LLM guidance |
|---|---|---|---|
| `MERGEABLE` | GitHub can currently create the merge. | Evidence in `get_mission_context`; no controller transition. | Inspect remaining checks/review/queue facts. It is not sufficient by itself. |
| `CONFLICTING` | The PR cannot merge because of conflicts. | `github_changed`; page the requested role. | Request or perform remediation on a fresh generation/head. |
| `UNKNOWN` | GitHub is still calculating or cannot currently determine mergeability. | `github_changed` if newly observed; otherwise wait. | Reread after a bounded interval. Absence never becomes mergeable. |

### `mergeStateStatus`

`mergeStateStatus` is an aggregate explanation, not a command. The LLM must decompose it
using the underlying head, named required context, review, and queue fields.

| Value | GitHub meaning | V4 mapping | LLM guidance |
|---|---|---|---|
| `DIRTY` | The merge commit cannot be cleanly created. | `github_changed`. | Remediate the conflict. |
| `UNKNOWN` | GitHub cannot currently determine the state. | `github_changed` on transition. | Reread; do not infer pass or failure. |
| `BLOCKED` | Some GitHub merge requirement blocks the PR. | `github_changed`. | Inspect the underlying requirement. A required merge queue can make an otherwise acceptable PR appear blocked; this value must never prevent arming by itself. |
| `BEHIND` | The head is out of date with the base. | `github_changed`. | Do not update solely for this repository: required checks are loose and the native merge group owns current-base proof. |
| `UNSTABLE` | Mergeable, but a commit status is not passing. | `github_changed`. | Open the named required status and its run URL; wait or request remediation based on live evidence. |
| `HAS_HOOKS` | Mergeable with passing statuses and pre-receive hooks. | `github_changed`. | Arm through the normal provider path; GitHub remains the enforcement point. |
| `CLEAN` | Mergeable with passing commit statuses. | `github_changed`. | If exact-head review is acceptable and neither auto-merge nor queue ownership exists, arm auto-merge once. This is the key no-sit case. |

### GitHub reviews and conversations

GitHub review state and Switchboard's exact-head review verdict are distinct facts. This
repository currently has no required GitHub review rule, so `reviewDecision` may be `null`
even when Switchboard requires a fresh `review_merge` generation.

| Value/event | Meaning | V4 mapping | LLM guidance |
|---|---|---|---|
| `reviewDecision=APPROVED` | GitHub's applicable reviews approve. | `github_changed`. | Confirm the approval and Switchboard verdict both apply to the live head. |
| `reviewDecision=CHANGES_REQUESTED` | An applicable GitHub review requests changes. | `github_changed`. | Read findings/comments and request remediation. |
| `reviewDecision=REVIEW_REQUIRED` | GitHub branch policy still requires review. | `github_changed`. | Complete the required review through an authorized reviewer. |
| `reviewDecision=null` | No aggregate decision applies or reviews are not required. | Raw context only. | Never treat null as approval or rejection. Use Switchboard's exact-head verdict and policy. |
| Review submitted/edited/dismissed | Review evidence changed. | One `github_changed` per material review value. | Reread the review and open structured findings. |
| Review comment/thread or ordinary PR comment changed | Potential requirement/evidence changed. | One `github_changed` with comment/thread URL. | Read it. Requirements that must survive handoff also belong in structured Switchboard findings. |
| Head changed after a review | The old review may no longer apply. | New-head `github_changed`; old review remains history. | Review the new exact head. |

### Commit statuses and check runs

The current required context is a classic commit status named
`Switchboard CI / VM gate`. GitHub may also expose Check Runs from other integrations.
Mission Bot never collapses all checks into one home-grown green/red flag; the context view
names the exact required context and preserves every supporting URL.

#### Aggregate/commit status values

| Value | Meaning | V4 mapping | LLM guidance |
|---|---|---|---|
| `EXPECTED` | A required status is expected but has not reported. | `github_changed` if newly observed. | Inspect dispatch evidence; wait only when a real run is progressing. |
| `PENDING` | The context has started and has no terminal result. | `github_changed`. | Follow the target URL. Yield `WAITING` if it is genuinely active. |
| `SUCCESS` | The context reported success for that exact SHA. | `github_changed`. | Continue exact-head review/arming. This is verification evidence, never Done. |
| `FAILURE` | The context reported a failing verdict. | `github_changed`. | Read the run/logs and choose remediation or infrastructure repair. |
| `ERROR` | The context could not produce a normal verdict. | `github_changed`. | Diagnose the provider/dispatch/callback failure; Mission Bot does not classify it. |

#### Check Run status values

| Status | Meaning to expose | LLM guidance |
|---|---|---|
| `REQUESTED` | The provider has requested the check, but it has not completed. | Find the provider/dispatch reference; do not infer that a runner started. |
| `QUEUED` | The provider accepted the check and queued it. | Wait only while the provider still shows a live queued run. |
| `IN_PROGRESS` | The check is executing. | Follow the run URL and yield `WAITING`. |
| `WAITING` | The check is waiting on provider-side work or input. | Inspect the provider detail; distinguish legitimate wait from missing authority/input. |
| `PENDING` | The check is nonterminal without a more specific phase. | Treat as nonterminal and follow the provider reference. |
| `COMPLETED` | Execution ended. | Require a conclusion; a missing conclusion remains incomplete evidence. |

#### Check Run conclusion values

| Conclusion | Meaning to expose | LLM guidance |
|---|---|---|
| `SUCCESS` | Check completed successfully. | Use only when it is current-head and policy-relevant. |
| `FAILURE` | Check completed with failure. | Inspect logs and remediate. |
| `TIMED_OUT` | Check exceeded its provider timeout. | Diagnose the run/provider; retry only through the authorized exact-SHA path. |
| `CANCELLED` | Check was cancelled. | Determine whether superseded or unexpectedly aborted. |
| `STARTUP_FAILURE` | Check failed before normal execution. | Repair infrastructure/dispatch, not product code by assumption. |
| `ACTION_REQUIRED` | Provider requires an explicit action. | Perform the authorized action or ask a genuine question through the LLM-only human door. |
| `STALE` | GitHub marked the run stale. | Ignore it as current evidence; reread the exact head. |
| `NEUTRAL` | Check completed without pass/fail. | Never substitute it for the required success context. |
| `SKIPPED` | Check did not execute. | Never substitute it for required exact-SHA verification. |

### Auto-merge and native merge queue

| GitHub fact | Exact meaning | V4 mapping | LLM guidance |
|---|---|---|---|
| `autoMergeRequest=null`, `mergeQueueEntry=null` | Neither auto-merge nor queue ownership is visible. | A material PR/check/review change creates `github_changed`. | If the PR is acceptable, arm once. Never wait merely because `mergeStateStatus=BLOCKED` when the missing action is queue admission. |
| `autoMergeRequest != null` | Auto-merge has been armed, with an actor, timestamp, and merge method. | `github_changed`; not Done. | Confirm the request is for `SQUASH` and the exact PR/head, then yield `WAITING`. GitHub owns the next transition. |
| `mergeQueueEntry.state=QUEUED` | Entry is waiting in the queue. | `github_changed`; not Done. | Wait. Do not dequeue, rebase, or create a second enqueue effect. |
| `mergeQueueEntry.state=AWAITING_CHECKS` | Merge-group checks are pending/running. | `github_changed`; not Done. | Follow the merge-group SHA and required-status URL; active CI is normal. |
| `mergeQueueEntry.state=MERGEABLE` | Queue requirements currently permit landing. | `github_changed`; not Done. | Wait for canonical merge provenance. |
| `mergeQueueEntry.state=UNMERGEABLE` | The queue cannot land the entry. | `github_changed`. | Inspect the queue/timeline and merge-group checks; choose remediation or a deliberate ordinary re-arm after diagnosing the cause. No custom automatic requeue cycle exists. |
| `mergeQueueEntry.state=LOCKED` | GitHub reports the queue entry locked. | `github_changed`. | Inspect provider state; do not turn the enum into a human alert automatically. |
| Queue entry disappears while PR remains open | The PR was removed/ejected or auto-merge was disabled. | `github_changed`. | Read the PR timeline and checks. Repair/re-arm only after understanding the live cause. |
| Merge-group head changes | GitHub rebuilt the landing candidate against a new base/queue prefix. | `github_changed` keyed to the new merge-group SHA. | Use only the new merge-group status. Old group evidence remains history. |

### Merge, Done, offline work, and deployment

| External truth | Board result | Mission Bot sees |
|---|---|---|
| PR/check/review/head/draft/mergeability/queue changed | No terminal transition. | One persisted `github_changed` event; the scoped worker may make the mission actionable. |
| CI green/red/pending | Evidence only. | `github_changed`; the LLM interprets it. |
| Auto-merge armed or PR queued | Not Done. | `github_changed`; the LLM confirms and yields waiting. |
| Canonical repository PR merged into `master`, with `mergedAt` and `mergeCommit.oid` verified and persisted by the provenance service | Board `Done` with canonical `merged_sha`. | `DONE`. |
| Public `projectplanner-ci` workflow succeeds | Verification evidence only. | Never Done. |
| Verifier-stamped offline evidence | Board `Done`. | `DONE`. |
| No new material fact | No change. | `WAITING`. |
| Authenticated current LLM asks a question | Attention only. | `HUMAN`. |

GitHub deployment states are deployment evidence, not code-task Done:

| Deployment state | Meaning to expose |
|---|---|
| `QUEUED` | Deployment work is queued. |
| `IN_PROGRESS` | Deployment work is running. |
| `WAITING` | Deployment is waiting on an environment/protection condition. |
| `PENDING` | Deployment has not reached a terminal result. |
| `SUCCESS` | Deployment provider reported success. |
| `FAILURE` | Deployment provider reported a failed outcome. |
| `ERROR` | Deployment could not produce a normal outcome. |
| `INACTIVE` | The deployment is no longer active. |

A separate deployment/acceptance mission may use them through its own verified
terminal-provenance contract. Mission Bot must not silently extend a code task past
canonical merge or mark a deployment task complete merely because code merged.

### Compound scenario table

This table is the operational answer to "what happens next?" It deliberately assigns
interpretation to the LLM, not Mission Bot.

| Live compound state | Controller behavior | Expected LLM behavior |
|---|---|---|
| No PR; dependencies met; `requested_role=implementation` | Start one implementation generation. | Implement, test, publish PR, and complete the C3 handoff. |
| No PR; dependencies unmet | Wait without calling `start_task`. | None until a dependency event arrives. |
| Open draft PR | Page `review_merge` from the relevant event. | Inspect completeness and mark ready when justified. |
| Open PR; required status expected/pending; run genuinely active | Page once on the event. | Inspect the run URL, then yield `WAITING` at the latest cursor. |
| Open PR; required status missing and no run exists | Page `review_merge`. | Repair/ensure exact-SHA CI; do not wait on imaginary work. |
| Open PR; required status failure/error | Page the requested role. | Read logs and request remediation or repair infrastructure. |
| Open PR; required status success; current review missing/stale | Page `review_merge`. | Review the live exact head and record structured findings/verdict. |
| Open PR; required status success; review acceptable; unarmed and not queued | Page `review_merge`. | Arm squash auto-merge once. |
| Open PR; `mergeStateStatus=BLOCKED`; required status success; unarmed | Page `review_merge`. | Decompose the block. If the remaining requirement is queue admission, arm instead of waiting. |
| Open PR; `mergeStateStatus=BEHIND` under the current queue rules | Page on material change only. | Do not churn the branch solely to catch `master`; queue integration owns that proof. |
| Open PR; conflict/dirty | Page requested role. | Request a fresh remediation generation. |
| New PR head | Page requested role with the new exact-head fence. | Discard old current evidence, inspect/review/test the new head. |
| Auto-merge armed; no queue entry yet | Page on the arming/status event, then wait. | Confirm exact PR/head and yield `WAITING`. |
| Queue `QUEUED`/`AWAITING_CHECKS`/`MERGEABLE` | Page on each material transition, then wait. | Observe; do not dequeue or duplicate the merge request. |
| Queue ejected/unmergeable | Page requested role. | Inspect timeline and merge-group evidence, then remediate or deliberately re-arm. |
| GitHub says merged but provenance is not persisted | No Done transition. | The LLM may request reconcile; the provenance service verifies. |
| Verified terminal provenance persisted | Close the mission as `DONE`. | No agent action. |
| PR closed unmerged while mission active | Page requested role. | Reopen/replace/remediate/cancel based on live intent; never factory-generate Needs-you. |
| Provider unavailable or context incomplete | Page on the failure observation or backstop. | Repair access/retry the read or yield waiting; absence is never green. |
| `HUMAN` with an unanswered LLM question | Wait. | Resume only from an explicit operator answer event. |
| Live runner for the task | Wait regardless of board/claim/message projections. | Capacity owns liveness. |

### The no-sit merge invariant

The current `review_merge` LLM must not yield merely because the UI says “Ready to merge.”
For the live exact head `H`, this tuple is the ordinary arm condition:

```text
PR is OPEN
and isDraft is false
and required status "Switchboard CI / VM gate" is SUCCESS on H
and Switchboard exact-head review is acceptable on H
and no unresolved structured finding applies to H
and mergeable is not CONFLICTING
and autoMergeRequest is null
and mergeQueueEntry is null
```

When that tuple holds, the LLM arms squash auto-merge once with an exact-head fence. When
`autoMergeRequest` or `mergeQueueEntry` is present, it waits. Mission Bot does not calculate
this tuple; it guarantees that a material input change produces one durable event and one
fresh opportunity for the LLM to read it.

## Current projectplanner CI mapped to v4

### Live repository policy

Audited through the GitHub API on 2026-07-30:

- canonical repository: `6th-Element-Labs/projectplanner`;
- default branch: `master`;
- auto-merge enabled;
- exactly one required context: `Switchboard CI / VM gate`;
- required-status strict/up-to-date mode: disabled;
- no required GitHub PR reviews or conversation-resolution rule;
- native merge queue ruleset `master-merge-queue`, `ALLGREEN`, squash;
- queue build/merge concurrency: five;
- minimum merge wait: two minutes;
- required-check timeout: 60 minutes; and
- active webhook delivery currently subscribes only to `pull_request`, `merge_group`, and
  `push`.

The required context is not pinned to one App in branch protection (`app_id=null`), although
the trusted public workflow currently posts it with the dedicated GitHub App. This is a
repository-policy fact to retain in audit output; Mission Bot must not invent a second
signature gate.

### Live trusted workflow

The public `6th-Element-Labs/projectplanner-ci` default branch owns workflow authority.
Canonical code is mirrored to a disposable exact-SHA tag; agent-authored workflow files are
never executed. The live repository variable `SWITCHBOARD_CI_LANE_MODE=enforce` activates
ADR-0024's trusted lanes:

| Purpose | Selected lane | Work performed | Authority |
|---|---|---|---|
| `head` | `admission` | Exact checkout, Git whitespace/error check, Python compile, lane-selector direct test. | Required PR-head status for queue admission; never Done. |
| `merge_group` with a mechanically proven eligible Markdown-only diff | `docs` | Exact diff, whitespace, conflict-marker, and local-link validation. | Required merge-group landing status; never Done. |
| Other `merge_group` | `full` | `scripts/switchboard_ci.sh`, Node, Chromium, Playwright, artifacts. | Required merge-group landing status; never Done. |
| `ci_repair` | `full` | Same full contract on the fixed exact SHA. | Audited repair evidence; never Done by itself. |

All lanes use one workflow, one App callback identity, and one required context. Purpose/lane
changes test work but never changes provenance authority.

### End-to-end CI transport

```text
canonical PR/head or merge_group event
  -> verify_ci.ensure(exact SHA)
  -> external_ci_mirror
  -> disposable refs/tags/ci/**
  -> trusted projectplanner-ci:verify.yml
  -> announce posts required context=PENDING on canonical exact SHA
  -> mechanically selected admission/docs/full suite
  -> report posts SUCCESS or FAILURE and verifies readback
  -> GitHub branch protection/merge queue consumes that one context
  -> canonical merge provenance, separately, may stamp Done
```

`external_ci_runs` exposes these transport states:

| Stored state | Meaning | V4 context |
|---|---|---|
| `requested` | Exact-SHA run requested. | Run reference; not evidence that execution started. |
| `mirrored` | Source was copied to the public scratchpad. | Mirror reference; not a test verdict. |
| `triggered` | Trusted workflow dispatch accepted. | Workflow/run reference. |
| `running` | Provider run is executing. | Active run URL. |
| `success` | Workflow completed and intended to publish success. | Supporting evidence; GitHub's named required status is landing truth. |
| `failure` | Workflow completed with a red result. | Failure/log references for the LLM. |
| `cancelled` | Workflow aborted. | Incomplete/abort evidence; never green. |
| `error` | Mirror, dispatch, poll, or callback failed. | Infrastructure evidence; the LLM diagnoses. |

The public `verify(sha)` read model may summarize these as `pending`, `green`, or `red` and
attribute a stall to `dispatch`, `run`, or `callback`. V4 may expose that summary as a hint,
but it must also expose the raw exact-SHA run and required-context records. The summary never
selects a role or changes mission state.

### CI failure scenarios

| Exact-SHA evidence | What it means | V4/LLM response |
|---|---|---|
| No run row; no required status | Never dispatched or dispatch evidence lost. | Page the LLM; inspect/ensure the exact-SHA route. |
| Run `requested`/`mirrored`; no workflow URL | Dispatch is incomplete. | LLM repairs mirror/dispatch or waits only with live evidence. |
| Workflow running; required status pending | Normal active CI. | LLM yields `WAITING`; the terminal status event wakes a new observation. |
| Workflow success; required status still pending/missing | Callback/readback failure. | LLM repairs/reposts through the trusted path; do not rerun code by assumption. |
| Workflow failure with `failure_class=tests`; required status failure | Suite found a defect. | LLM requests remediation against that exact head. |
| Workflow error/cancel/startup failure | The suite may not have produced a verdict. | LLM repairs infrastructure/dispatch; Mission Bot never labels product code broken. |
| Required status success; external run row lacks `task_id` | GitHub has exact-SHA landing evidence, but the board lookup is incomplete. | Select evidence by exact SHA/context, not producer/task row. This is the BUG-239 class. |
| Evidence belongs to another Work Session but same task/branch/head and has valid canonical provenance | Producer generation differs; code object does not. | Do not invalidate solely because a newer session exists. This is the BUG-240 class. |
| Status belongs to old PR head | Stale evidence. | Keep history; never expose it as current. |
| Merge-group status belongs to a destroyed/rebuilt group | Stale landing candidate. | Keep history; use the current queue entry/group SHA only. |
| Multiple runs exist for one SHA | Retries/reposts occurred. | GitHub's current named required context controls landing; retain all run URLs for audit. |
| `ci_repair` succeeds | Identical full suite passed for the repair SHA. | A human administrator may use the audited repair lane; success alone is never Done. |
| Performance monitor fails | Non-required timing signal. | Record/alert separately; never block or remediate a PR automatically. |

### Required webhook/event coverage

The live hook/event gap explains why a PR can look ready and move only much later. The
public workflow posts a classic commit `status`, but the current repository hook does not
subscribe to `status`; reviews and comments are also absent. The legacy 30-second scoped
poll, another PR event, or reconciliation may eventually rediscover the change. That is
eventual luck, not a no-stuck guarantee.

| GitHub event | Current subscription | V4 requirement |
|---|---|---|
| `pull_request` | Yes, but the current handler interprets only opened/reopened/ready/synchronize/closed. | Subscribe and append one raw `github_changed` for every mapped material action, including draft, enqueue/dequeue, auto-merge, and head changes. |
| `status` | **No.** | **Required.** This is the immediate wake for `Switchboard CI / VM gate` pending/success/failure/error. |
| `check_run` | No. | Subscribe for provider checks and future required Check Runs. |
| `check_suite` | No. | Subscribe as supporting provider evidence; dedupe with check-run identity. |
| `pull_request_review` | No. | Required for submitted/edited/dismissed review changes. |
| `pull_request_review_comment` | No. | Required when diff comments may contain findings. |
| `pull_request_review_thread` | No. | Required when thread resolution changes actionable review evidence. |
| `issue_comment` | No. | Required for ordinary PR conversation comments. |
| `merge_group` | Yes. | Keep; append group/head changes and dispatch exact-SHA CI. |
| `push` | Yes. | Keep for canonical-base observation and provenance reconciliation; never use it as runner liveness. |
| `workflow_run` on `projectplanner-ci` | No. | Not required for lifecycle if the canonical `status` callback and external run store are working; it may remain audit-only. |
| `deployment_status` | No. | Only required for a separately scoped deployment/acceptance mission. |
| `repository_ruleset` / `branch_protection_rule` | No. | Subscribe for immediate changes to required checks or queue policy, or rely explicitly on the five-minute policy refresh. |
| `repository` | No. | Subscribe for material default-branch/merge-policy edits, or rely explicitly on the five-minute policy refresh. |

Every subscribed delivery first lands in the existing durable `webhook_inbox`. V4 adds event
projection after durable ingestion; it does not make the HTTP handler a coordinator.

The relevant `pull_request` actions normalize as follows:

| Action family | Actions | V4 observation |
|---|---|---|
| PR created or restored | `opened`, `reopened` | Append when mapped to the mission; the LLM reads the full live PR. |
| Exact code/base changed | `synchronize`, material `edited` | Append with the live head/base. Old-head evidence becomes history. |
| Draft state changed | `converted_to_draft`, `ready_for_review` | Append; the LLM decides the next review/ready action. |
| Merge ownership changed | `auto_merge_enabled`, `auto_merge_disabled`, `enqueued`, `dequeued` | Append; the LLM waits, diagnoses removal, or arms once as appropriate. |
| PR terminal observation | `closed` | Append. If merged, the provenance projector verifies; if unmerged, the LLM decides recovery/cancellation. |
| Review demand changed | `review_requested`, `review_request_removed` | Append; expose the requested reviewers and current review evidence. |
| Conversation changed | `locked`, `unlocked` | Append only when the lock is material to the current mission; never interpret it as mergeability. |
| Metadata only | `assigned`, `unassigned`, `labeled`, `unlabeled`, `milestoned`, `demilestoned` | Persist in the provider inbox for audit; do not create a mission event unless accepted workflow policy makes it material. |

The other lifecycle event actions are:

| Event | Actions/values | V4 observation |
|---|---|---|
| `status` | `pending`, `success`, `failure`, `error` in the webhook payload | Append on a material exact-SHA/context transition. `EXPECTED` is an aggregate GraphQL state when an expected context has not reported, not a status webhook value. |
| `check_run` | `created`, `rerequested`, `requested_action`, `completed` where available to the hook type | Append the raw status/conclusion and URL; never treat the action name as the verdict. |
| `check_suite` | `requested`, `rerequested`, `completed` where available to the hook type | Append only when it adds material evidence not already represented by its Check Runs. |
| `pull_request_review` | `submitted`, `edited`, `dismissed` | Append with review ID, state, commit ID, and URL. |
| `pull_request_review_comment` / `issue_comment` | `created`, `edited`, `deleted` | Append with the comment URL/ID; the LLM rereads live content. |
| `pull_request_review_thread` | `resolved`, `unresolved` | Append the thread identity and resolution state. |
| `merge_group` | `checks_requested` | Append the current merge-group head and dispatch exact-SHA CI. |
| `repository_ruleset` / `branch_protection_rule` | `created`, `edited`, `deleted` | Append only when the active default-branch landing policy materially changed. |
| `repository` | material `edited` fields | Append when default branch, auto-merge, or allowed merge methods changed. |

Repository/ruleset changes that alter the default branch, required context, merge queue,
auto-merge, or squash availability also append a material `github_changed` event for active
missions. A five-minute live-policy refresh is the backstop when no repository-policy
webhook is subscribed.

Not every GraphQL aggregate transition has its own webhook action. A delivery is a wake edge,
not the authoritative snapshot: after any material delivery, `get_mission_context` rereads
the live PR, required context, reviews, auto-merge request, and queue entry. The trusted CI
report callback may append the same exact-SHA/context event after verified readback; it and
the GitHub `status` delivery deduplicate to one material fingerprint.

## Persistent mission context

Agents do not receive a generated dossier. After MCP registration they call one read model:

```text
get_mission_context(project, task_id)
```

The response is a fixed-size current instrument panel plus recent history:

```json
{
  "schema": "switchboard.mission_context.v4",
  "project": "switchboard",
  "task_id": "QA-24",
  "mission": {
    "state": "ACTIVE",
    "requested_role": "review_merge",
    "handled_through": 41,
    "latest_sequence": 42
  },
  "current": {
    "task_status": "In Review",
    "dependencies_satisfied": true,
    "github": {
      "repo": "6th-Element-Labs/projectplanner",
      "pr": {
        "number": 1097,
        "url": "https://github.com/6th-Element-Labs/projectplanner/pull/1097",
        "state": "OPEN",
        "is_draft": false,
        "head_sha": "d76647d3b888af9fedd4ade1d70de9f6b4105e2f",
        "base_branch": "master",
        "base_sha": "base-example",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "review_decision": null,
        "auto_merge_armed": false,
        "queue": null
      },
      "required_context": {
        "name": "Switchboard CI / VM gate",
        "state": "SUCCESS",
        "target_url": "https://github.com/example/run"
      }
    },
    "external_ci": {
      "run_id": "ecir-example",
      "source_sha": "d76647d3b888af9fedd4ade1d70de9f6b4105e2f",
      "purpose": "head",
      "lane": "admission",
      "status": "success",
      "run_url": "https://github.com/example/run",
      "callback_verified": true
    },
    "switchboard_review": {
      "status": "passed",
      "head_sha": "d76647d3b888af9fedd4ade1d70de9f6b4105e2f",
      "ref": "reviewverdict-example"
    },
    "open_findings": [],
    "runner": {"live": true, "execution_id": "execlease-example"},
    "terminal_provenance": null
  },
  "recent_history": [
    {
      "sequence": 42,
      "event_type": "github_changed",
      "head_sha": "d76647d3b888af9fedd4ade1d70de9f6b4105e2f",
      "external_ref": "https://github.com/example/run"
    }
  ],
  "history_cursor": 42,
  "context_complete": true,
  "missing_sources": []
}
```

`get_mission_context` follows these rules:

1. GitHub's live PR head is the current head.
2. Capacity's `runner_sessions` projection is the current runner fact.
3. GitHub's named required context is the landing fact; `external_ci_runs` supplies
   transport/log references and never overrides it.
4. Current CI, review, findings, auto-merge, and queue facts are selected for the exact
   current PR or merge-group head.
5. The current GitHub PR fields are returned raw. `get_mission_context` does not manufacture
   `ready_to_merge`, `fixable`, or `needs_human`.
6. Terminal state comes from already-persisted verified provenance, not a PR-looking-merged
   inference in this read model.
7. Producer identity and Work Session are provenance metadata, not evidence validity by
   themselves.
8. Old-head and destroyed merge-group evidence remains in history but never appears as
   current.
9. Missing or unavailable sources are named in `missing_sources`; absence never becomes
   green.
10. The response contains references, not copied logs.
11. Older history is available through
    `list_mission_history(project, task_id, after_sequence, limit)`.

The context builder may reuse existing authoritative repositories, but v4 runtime behavior
must not read current completion routes, normalization outputs, generated dossiers, stored
retry counters, or legacy human classifications.

## Bare boot protocol

The immutable assignment contains only identity and a launch pointer:

```text
Mission: QA-24
Project: switchboard
Role: review_merge
Execution: execlease-example
Generation: 4
Trigger event: 42
PR: #1097
Expected head: d76647d3b888af9fedd4ade1d70de9f6b4105e2f

Register with Switchboard.
Call get_mission_context(project="switchboard", task_id="QA-24").
Inspect the referenced live Switchboard and GitHub evidence.
Move the mission toward canonical Done.
Only ask the operator when you have one genuine explicit question.
```

No task description, findings list, prior-attempt analysis, CI diagnosis, merge-gate
interpretation, or copied dossier is serialized into the assignment.

`prepare_agent_session` returns this first-call sequence for a task-bound worker:

```text
get_working_agreement
register_agent
get_mission_context
list_unacked_messages
```

Mailbox reads remain communication hygiene. They do not drive mission state.

## Agent completion commands

### Continue with a fresh role

```text
yield_mission(
    task_id="QA-24",
    observed_through=42,
    outcome="continue",
    requested_role="remediation"
)
```

This is used when the LLM has diagnosed the next required role. The server validates the
exact execution ID, generation, fence, task, project, and assigned head. It then records the
yield and requests surrender of that exact execution lease. After the Capacity terminal
receipt, the mission becomes `ACTIVE`.

### Wait for an external change

```text
yield_mission(
    task_id="QA-24",
    observed_through=42,
    outcome="waiting",
    requested_role="review_merge"
)
```

The server accepts `WAITING` only when `observed_through` equals the latest persisted event
sequence in the same transaction. If a newer event arrived while the agent worked, the yield
may be recorded for audit, but the mission remains actionable with that newer event
unhandled. After a valid current-cursor yield, the mission becomes `WAITING` only when the
exact execution terminalizes. A later event is appended; only the fenced scoped worker may
observe it and drive the mission again.

### Ask a human

The current `agent_requires_human` semantics remain:

```text
agent_requires_human(
    task_id="QA-24",
    question="Which of these two product behaviors is intended?",
    evidence_refs=[...]
)
```

Only a server-authenticated current LLM execution may create this state. Factory code,
classifiers, timeouts, workers, messages, and provider adapters cannot.

`HUMAN` creates an attention/question record. It must not project board `Blocked` as a
`start_task` admission gate. When the operator answers, the exact answer event makes the
mission eligible for a fresh agent generation even if a legacy board projection still says
Blocked. This prevents the old UI-72 deadlock from returning under a new name.

### Done

There is no agent-authored command for Done. An LLM may call
`reconcile_task_merge(task_id)` to request an immediate check, but that command rereads
GitHub and succeeds only when the canonical repository proves the PR merged. Webhook and
reconciliation therefore share the same server-side provenance verifier:

```text
LLM observes a merge -> request reconcile_task_merge
server rereads GitHub -> persist canonical merged_sha -> board Done
Mission Bot observes persisted Done -> close mission item
```

The LLM supplies timing, not truth. Its assertion, pasted SHA, task status, or successful
merge command is never sufficient evidence for Done.

For non-PR work, the existing privileged offline verifier performs the equivalent
evidence check and persists terminal offline provenance. Mission Bot treats either source as:

```text
verified terminal provenance already persisted -> DONE
```

### Stale writes

A yield or human request from a stale generation is audit-only and cannot change
`mission_items`. It cannot wake, stop, replace, or redirect the current generation.

### Terminal-runner precedence

A terminal Capacity receipt is not enough to choose the next role. The exact execution's
accepted handoff/yield wins:

1. C3 implementation completion or a valid `yield_mission` persists the intended
   outcome/next role before surrender.
2. The matching Capacity terminal receipt atomically finalizes that accepted handoff and
   marks both the handoff event and terminal receipt handled.
3. If the exact execution terminates without any accepted handoff/yield, append one abnormal
   `runner_ended` event and keep the same requested role eligible.
4. A duplicated or late generic `runner_ended` receipt cannot reboot the previous role after
   a valid handoff was finalized.

This prevents a clean implementation-to-review or review-to-remediation transition from
being overwritten by a lower-information process-exit observation.

## Scoped worker

The worker is deliberately boring:

```python
def tick(scope, mission):
    authority = acquire_and_validate_scope_lease(scope)
    if not authority.allowed:
        return WAIT

    if verified_terminal_provenance_is_persisted(mission.task_id):
        return DONE

    if dependencies_are_unmet(mission.task_id):
        return WAIT

    if mission.state == "HUMAN":
        return WAIT

    if runner_sessions.has_live_execution(mission.task_id):
        return WAIT

    event = oldest_unhandled_event(mission)
    if event is None:
        return WAIT

    return start_task(
        task_id=mission.task_id,
        role=mission.requested_role,
        source_sha=live_pr_head_if_required(mission.requested_role),
        mission_key=f"{scope.generation}:{mission.task_id}:{event.sequence}:"
                    f"{mission.requested_role}",
        instruction=minimal_launch_pointer(event),
    )
```

Properties:

- One tick performs at most one external mutation.
- Every work-driving call carries the current W2 scope tuple.
- Dependency admission is checked before `start_task`, so a deterministic refusal cannot
  loop every 30 seconds.
- `start_task` remains the sole capacity door and deduplicates replay.
- If start fails before admission, the event remains unhandled.
- If the worker crashes after admission, `start_task` replay attaches to or reads back the
  same generation.
- The worker never selects a host, process, claim, Work Session, CI repair, or human route.
- The worker never calls GitHub merge APIs itself.

One service process may poll many scopes, but it gains authority separately for each fenced
scope. The process itself has no project-wide work authority.

## External observation and the no-stuck guarantee

GitHub webhook ingestion appends an idempotent `github_changed` observation. Canonical
merge/offline verification may persist terminal provenance and append
`terminal_provenance_persisted`. Capacity terminal receipts append `runner_ended`. These
events contain facts only and have no work-driving authority.

The ingestor never changes `WAITING` to `ACTIVE`. The scoped worker observes unhandled events
under its W2 lease and either requests one runner or waits.

A five-minute observation backstop protects against a lost webhook:

1. When an LLM yields `WAITING`, persist the server timestamp.
2. If no later material event arrives within five minutes, append exactly one
   `observation_due` event for that wait timestamp.
3. The fenced scoped worker observes the unhandled event and requests a fresh generation.
4. The fresh LLM rereads live GitHub and Switchboard state.
5. If it is still legitimately waiting, it yields a new wait timestamp.

This bound is derived from persisted timestamps, never a retry counter.

Service objectives:

- An `ACTIVE` mission with no live execution receives a `start_task` request within one
  30-second worker poll.
- A missed provider webhook cannot leave a `WAITING` mission unobserved for more than five
  minutes plus one worker poll.
- A clean or abnormal agent exit without a valid yield produces `runner_ended` and makes the
  same requested role eligible within one worker poll.
- A green PR with no live runner and no human question therefore cannot sit indefinitely.

## ADR-0008 conformance

### Capacity: C1–C3

| Rule | V4 mapping |
|---|---|
| **C1: one physical registry** | V4 reads liveness only from `runner_sessions`. `mission_items`, events, claims, messages, and scope leases never mean live. |
| **C2: one automatic stop executor** | V4 never kills a process. The exact holder may surrender; Capacity's lease reaper owns stop. Operator Kill remains the audited exception. |
| **C3: surrender, reap, acknowledgement, review** | Implementation still completes through the existing exact C3 hard handoff. V4 does not expose review or boot `review_merge` until Capacity acknowledgement/finalization records the material event. |

### Communication: M1–M3

| Rule | V4 mapping |
|---|---|
| **M1: messages have zero lifecycle authority** | Mailbox storage, delivery, acknowledgement, and timeout never change `mission_items` or call `start_task`. |
| **M2: explicit delivery truth** | Communication receipts remain communication records. A human answer changes coordination only through an explicit authenticated answer command, not a message acknowledgement. |
| **M3: mailbox hygiene is observable** | Agents drain messages after registration. Stale mail may appear in context but never means dead, blocked, or ready. |

### Coordination: W1–W4

| Rule | V4 mapping |
|---|---|
| **W1: one door into capacity** | The only worker mutation that requests a runner is `Task Execution.start_task`. |
| **W2: fenced scope lease** | Every worker tick and work-driving write validates the exact `autopilot_scopes` lease tuple. `mission_items` is not a scope authority. |
| **W3: roaming daemon is janitor-only** | Global observation may ingest webhooks, verify terminal provenance, and append fact events. It never changes `WAITING` to `ACTIVE`. Only the scoped worker may observe an event and turn it into `start_task`. |
| **W4: one completion and merge owner** | `mission_items` is Task Execution's sole v4 completion row. Roles remain fresh generations. Only `review_merge` may arm merge. Only canonical GitHub or privileged offline provenance writes Done. |

## Enforcement stays at the boundary

Removing Mission Bot's classifier does not remove safety:

| Boundary | Enforcement |
|---|---|
| Initial work | Dependency admission and active W2 scope |
| Physical execution | One live generation, immutable assignment, generation/fence validation |
| Repository writes | Project/task/workspace and exact-head fences |
| Review handoff | C3 implementation surrender and Capacity acknowledgement |
| Merge request | Current `review_merge` execution, assigned PR/head, idempotent provider write |
| Landing | GitHub required checks and native merge queue |
| Done | Verified canonical merged SHA from webhook/reconciliation, or verifier-stamped offline evidence |

Advisory merge-gate facts remain available to the LLM. They do not select a role, create a
human request, or prevent a green PR from receiving an LLM.

## Current implementation mapping

As audited on 2026-07-30, production is still the v1 path: an eight-output classifier and
fact-normalization layer build dossiers, boot selected roles, mark drafts ready, and arm
merge directly. The tables below define what v4 reuses and what the single cutover must
disconnect. This document is not evidence that v4 is deployed.

### Keep as authoritative primitives

| Current surface | V4 use |
|---|---|
| `Task Execution.start_task` | Sole execution admission command |
| `autopilot_scopes` | Sole scoped coordination authority |
| `runner_sessions` and execution leases | Sole physical presence and automatic-stop clock |
| Agent Host / Connect | Capacity placement and exact assignment boot |
| C3 implementation completion finalizer | Hard implementation-to-review handoff |
| GitHub webhook, reconcile, and offline verifier | Provider observations and verified terminal provenance |
| Required CI context and native merge queue | Landing authority |
| MCP project binding and `register_agent` | Agent identity and project/task scope |
| Existing task, review, finding, CI, preflight, and execution stores | Referenced facts in `get_mission_context` |

### Replace in the live control path

After v4 acceptance, live behavior must no longer depend on:

- `src/switchboard/domain/mission_bot/reducer.py`;
- `src/switchboard/domain/mission_bot/facts.py`;
- `src/switchboard/domain/mission_bot/dossier.py`;
- the eight-output `MissionOutput` classifier;
- `src/switchboard/application/mission_bot/driver.py` fact reduction;
- `src/switchboard/application/completion_driver.py` route classification;
- `src/switchboard/domain/completion/state_machine.py`;
- `src/switchboard/domain/completion/normalization_law.py`;
- direct Mission Bot `gh pr ready` and `gh pr merge --auto` effects;
- `completion_runs.route`, `reason_code`, `attempt`, `state_version`,
  `next_retry_at`, or `board_status` as live authority;
- completion-effect retry ledgers as lifecycle authority;
- decision episodes as live controller input;
- `completion_routing` candidate selection;
- sticky human blockers and legacy `completion_wakes`;
- classifier-generated remediation or human decisions;
- `mission_dossier`, route, attempt, state-version, `acceptance_findings`, and
  prior-attempt analysis in immutable execution assignments;
- factory-generated Needs-you/attention for CI, review, merge, claim, callback, or evidence
  states;
- any UI/coordinator projection that treats `completion_runs` as task lifecycle truth; or
- convergence ladders and factory-state attention requests.

Historical rows may remain read-only for audit during the proof window. V4 never consults
them. After acceptance, delete the retired runtime code and compatibility projections under
ADR-0006 rather than maintaining two workflow engines.

The cutover must also remove the current split-brain where reconciliation may preserve
`In Review`/`Blocked` while a legacy completion projection says `Done`. V4 reads the
canonical task row plus verified provenance; it never reads `completion_runs.board_status`
to decide terminal state.

## Cutover without legacy dependence

### 1. Build the v4 stores and MCP reads

- Add `mission_items` and `mission_events`.
- Add `get_mission_context` and paginated `list_mission_history`.
- Add exact-execution `yield_mission`.
- Add first-call boot wiring after `register_agent`.

No live behavior changes yet.

### 2. Backfill one starting observation, not old decisions

For each active scope, build one initial v4 item from current authoritative sources only:

- scope state from `autopilot_scopes`;
- task and dependencies from the task store;
- live execution from `runner_sessions`;
- PR/head/check/queue/merge from live GitHub; and
- canonical merge provenance from the provenance store.

Do not import legacy routes, reason codes, retry counts, classifier decisions, or generated
dossiers.

Initial requested role:

- verified terminal provenance already persisted: initialize state `DONE`;
- dependencies unmet: initialize the appropriate role but do not call `start_task`;
- no PR and dependencies met: `implementation`; and
- any PR without persisted terminal provenance: `review_merge` so an LLM inspects the live
  mission, including a provider-merged PR that still needs canonical reconciliation.

An open red PR still starts `review_merge`; that LLM decides whether remediation is needed.

### 3. Shadow the worker

Run the v4 worker read-only against active scopes. For every historical and live event, prove
that it would either wait or call `start_task` with the persisted requested role. It must
never emit a GitHub mutation, remediation diagnosis, or human decision.

### 4. Single cutover

- Stop work-driving effects from the current Mission Bot/completion reducer.
- Enable the v4 scoped worker.
- Keep the global janitor observation-only.
- Do not dual-run work-driving paths.

Rollback is one service switch while v4 tables remain audit-only. Never allow both engines to
own effects simultaneously.

The production cutover test must inspect the loaded v4 service graph, not only source text.
It fails if the running worker imports or invokes the legacy reducer, `facts.py`, dossier,
completion routing, decision-episode, or completion-effect path. A second effect spy fails
if any legacy component writes `start_task`, GitHub ready/merge, Human, or Done during the
same proof. “Feature flag off” is not sufficient if both engines can still write.

### 5. Prove, then subtract

Run the acceptance suite below. Once it passes in production, remove the retired control
path, assignment dossier, compatibility route projections, and stale active documentation.

## Required acceptance tests

### Pure transition tests

1. Operator Start creates `ACTIVE/implementation` plus `mission_started`.
2. `ACTIVE` + no runner starts exactly one implementation generation.
3. Replaying the same event attaches/readbacks; it never creates a duplicate execution.
4. A live runner always produces WAIT without changing mission state.
5. A valid LLM wait records its cursor and becomes `WAITING` only after execution
   terminalization.
6. Provider ingestion appends a new event without changing `WAITING`; the fenced scoped
   worker observes it and makes exactly one `start_task` call.
7. A stale execution cannot yield, change role, or ask a human.
8. Only an authenticated current LLM can create `HUMAN`.
9. A valid `WAITING` yield is rejected as current when `observed_through != latest_sequence`;
   the newer event remains actionable.
10. Dependencies unmet produces WAIT and no refused `start_task` loop.
11. A human answer creates an event and the scoped worker resumes the requested role even
    when a legacy board projection says `Blocked`.
12. Canonical merge or privileged offline provenance produces `DONE`; no agent or worker can.

### ADR-0008 role and failure tests

1. Implementation completion follows C3 surrender, reaping, host acknowledgement, and only
   then makes `review_merge` eligible.
2. Red CI wakes `review_merge`; the LLM requests `remediation`; Mission Bot never diagnoses
   the failure.
3. Remediation publishes a new head and requests a fresh `review_merge`.
4. Old-head events remain in history and never become current evidence.
5. Review and remediation generations never reuse the implementation execution.
6. A dead runner produces a new event and one fresh same-role generation.
7. A terminal receipt after an accepted C3/yield finalizes that handoff and never restarts
   the prior role; an abnormal terminal receipt without a yield restarts the same role.
8. Communication timeout produces no mission transition or capacity mutation.
9. Coordinator takeover with a higher scope fence cannot replay a stale write.

### Crash and provider tests

1. Crash before `start_task`: event remains unhandled and is retried.
2. Crash after `start_task`: replay reads back the same generation.
3. Crash during LLM yield: transaction contains either the whole yield or none of it.
4. Lost GitHub webhook: `observation_due` wakes a fresh LLM within the bound.
5. Duplicate GitHub webhook: one event.
6. GitHub unavailable: no green/merged fact is invented; the event remains recoverable.
7. Agent Host unavailable: Capacity remains pending; no human request is fabricated.
8. Coordinator restart: persisted item, cursor, role, and history resume without memory loss.

### GitHub and CI matrix tests

1. Every documented PR, mergeability, merge-state, review, status, Check Run, queue, and
   deployment enum is accepted as raw context without falling through to a human or
   remediation classification.
2. Each material `pull_request` action, `status`, review/comment action, queue transition,
   merge-group change, and repository-policy change appends one deduplicated
   `github_changed` event.
3. `status=SUCCESS` on the current head, acceptable exact-head review, no findings,
   `autoMergeRequest=null`, and `mergeQueueEntry=null` wakes `review_merge`, which arms
   squash auto-merge once.
4. The same tuple with `mergeStateStatus=BLOCKED` still reaches the LLM; the aggregate flag
   cannot suppress arming when queue admission is the remaining action.
5. `autoMergeRequest != null` or any live queue state causes the LLM to wait, never enqueue
   twice or dequeue.
6. A new PR head invalidates all old-head current evidence and pages one fresh
   `review_merge` generation.
7. A destroyed/ejected merge group cannot reuse its old CI; its successor group requires
   exact-SHA evidence.
8. Head `admission`, eligible merge-group `docs`, ordinary merge-group `full`, and
   `ci_repair=full` all publish the same required context on the intended SHA and never
   produce Done.
9. Workflow success with a missing/pending callback remains incomplete and pages the LLM;
   it is never silently green.
10. Missing `status` webhook delivery is recovered by `observation_due` within the bound;
    with the subscription enabled, terminal CI produces an immediate durable event.
11. A provider-merged PR without persisted provenance stays nonterminal and can request
    reconciliation; only verified persisted provenance produces Done.
12. The production v4 process imports none of the retired work-driving modules and the
    legacy effect spy records zero writes.

### Five production canaries

The release proof is five unattended tasks:

1. clean implementation → review → merge → Done;
2. controlled red CI → LLM-selected remediation → new head → review → merge → Done;
3. implementation runner loss → fresh implementation recovery → Done;
4. review runner loss while waiting → fresh review recovery → Done; and
5. dropped GitHub webhook → observation backstop → merge → Done.

For every canary, retain:

- ordered mission events;
- every execution ID, generation, role, and terminal receipt;
- the exact PR head used by each review/merge action;
- the required PR and merge-group CI URLs;
- the merge-queue receipt;
- the canonical merged SHA; and
- proof that no Needs-you state existed unless the LLM explicitly called
  `agent_requires_human`.

Acceptance requires all five to reach canonical Done without manual runner starts, CI reruns,
queue nudges, merges, state-table surgery, or service restarts.

## Operational invariants

1. **One row:** one `mission_items` row per project/task.
2. **One inbox:** ordered, idempotent `mission_events`.
3. **One scope:** `autopilot_scopes` owns drive authority.
4. **One runner truth:** `runner_sessions`.
5. **One capacity door:** `start_task`.
6. **One current head:** live GitHub.
7. **One mind:** the registered LLM diagnoses the mission.
8. **One human door:** authenticated `agent_requires_human`.
9. **One landing authority:** GitHub required checks and merge queue.
10. **One Done authority:** verified terminal provenance—canonical merge or privileged
    offline verification.

If an implementation proposal adds another state, classifier, retry ledger, lifecycle timer,
human escalation path, or work-driving daemon, it is not Mission Bot v4.

## Subtraction ledger

V4 adds:

- one four-state Task Execution mission row;
- one typed append-only mission event journal;
- one context read, one history read, and one exact-execution yield command; and
- one scoped pager loop.

V4 removes:

- the live CI/review/merge fact classifier;
- the normalization/reinterpretation layer;
- generated mission dossiers;
- factory-selected remediation;
- factory-selected human escalation;
- completion-route retry/convergence state;
- evidence selection by newest Work Session;
- multiple representations of “what should happen next”; and
- the possibility that a green PR waits because Mission Bot misunderstood its evidence.

The net result is one persisted workflow row, one persisted inbox, one scoped worker, and an
LLM that reads the mission after it wakes.

## Provider references

- [GitHub GraphQL enums](https://docs.github.com/en/graphql/reference/enums)
- [GitHub webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
- [GitHub merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [GitHub auto-merge](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-auto-merge-for-pull-requests-in-your-repository)
