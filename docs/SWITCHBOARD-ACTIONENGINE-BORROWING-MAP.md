# Switchboard / ActionEngine borrowing map

- **Status:** Strategic implementation spec
- **Board anchor:** DOGFOOD-11
- **Related strategy:** [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md)
- **Source reference:** ActionEngine local checkout, especially the DBOS runtime,
  durability service, approvals, retry helper, workflow run history, operational memory,
  and outcome-ledger PRDs.

---

## 1. Purpose

ActionEngine already contains useful durable-workflow ideas: DBOS-backed execution,
workflow run receipts, approval gates, exact-once side effects, retry discipline,
operational memory, and outcome ledgers.

Switchboard should borrow those ideas, but not become ActionEngine. ActionEngine runs
domain workflows. Switchboard coordinates humans, agents, runtimes, hosts, tasks, claims,
messages, leases, approvals, cost, and evidence across many workflows.

This map separates three decisions:

| Decision | Meaning |
|---|---|
| **Direct lift** | Copy or extract a small dependency-light helper into Switchboard with tests and attribution. |
| **Pattern copy** | Reimplement the idea in Switchboard's data model, APIs, activity log, and task lifecycle. |
| **Do not import** | Keep the ActionEngine artifact out of Switchboard; use it only as architecture reference. |

The bias should be conservative: direct-lift only where the code is small, isolated, and
not coupled to ActionEngine's workflow engine, schemas, or OT/business semantics.

---

## 2. What is good in ActionEngine

ActionEngine has several product-quality instincts worth preserving:

1. **DBOS is invisible infrastructure.** The DBOS adapter supplies checkpoint/restart
   behavior while Taikun keeps the workflow semantics. That is the right posture if
   Switchboard ever uses DBOS: infrastructure under our product contract, not the product
   contract itself.
2. **One canonical run id joins the world.** Workflow runs, LLM calls, documents,
   approvals, decision records, and outcomes all hang from `run_id` /
   `workflow_execution_id`. Switchboard needs the same discipline around task, claim,
   session, PR, and outcome ids.
3. **Operational memory is a receipt, not a duplicate app.** The ActionEngine
   `decision_record` concept indexes the truth of a run without trying to replace every
   source table. Switchboard should project receipts from the append-only activity graph,
   not create a second planner.
4. **Correctness failures are loud.** Durability and effect code distinguishes
   correctness violations from telemetry gaps. Missing asset, missing target, unknown
   point, expired approval, and unconfirmed effects should stay red rather than turning
   green through a fallback.
5. **Side effects are claimed, read back, and confirmed.** The OT/API effect path has
   the right skeleton: deterministic effect key, lock where needed, claim before doing,
   verify/read back, confirm only after proof.
6. **Human gates default deny.** Approval lookup treats expiry as a non-approval.
   Switchboard should do the same for dispatch, runner control, merge, spend, hosted
   execution, and sensitive integration actions.
7. **Cost and governance are visible in the work surface.** Runs and governance panels
   make cost, model/policy caps, and per-run detail visible instead of leaving them as
   backend-only events.
8. **Outcome memory is the real moat.** The outcome-ledger framing is exactly the
   Switchboard thesis: work gets defensible when actions connect to predicted, realized,
   measured, and attributed outcomes.

---

## 3. Code-borrowing decisions

| ActionEngine artifact | What to borrow | Decision | Switchboard owner |
|---|---|---|---|
| `actionengine/engine/services/retry.py` | Transient classifier shape, exponential backoff, jitter, max-attempt handling. | **Direct lift candidate.** Keep the helper small and dependency-free; extend error classes for MCP, REST, GitHub, provider-cost APIs, and Agent Host calls. | `BUG-16` |
| Deterministic effect-key helpers in `durability_service.py` / `api_effect.py` | Stable key from action type, target, resource, payload hash, and idempotency window. | **Pattern copy, possible tiny helper lift.** Rebuild around Switchboard project/task/claim/session ids. Do not import ActionEngine tables. | `HARDEN-21` |
| `workflow_run_service.py` | Lifecycle receipt fields, waiting/terminal states, cost/call rollups. | **Pattern copy.** Switchboard receipts are projections from task activity, claims, messages, approvals, PR evidence, and Tally, not ActionEngine workflow rows. | `RECON-9` |
| `approval_service.py` | Request/decide schema, expiry-as-deny, evidence on decisions. | **Pattern copy.** Reuse the default-deny behavior and evidence contract; map to dispatch, SME review, runner control, merge, spend, and hosted execution. | `DISPATCH-3`, `ACCESS-10`, future approval-gate hardening |
| `durability_service.py` step journal | Resume map, cancellations, dead letters, drain events. | **Pattern copy.** Useful for replay/reconcile jobs and hosted dispatch, but too workflow-engine-specific to copy. | `RECON-8`, `RECON-10`, `HARDEN-21` |
| `api_effect.py` and `ot_effect.py` | Claim effect, lock resource, verify/read back, confirm effect. | **Pattern copy.** Use for external software side effects only; do not import OT semantics or compensation code. | `HARDEN-21` |
| `sensing_emit.py` | Universal queue/inbox with dedupe key and visible failed emits. | **Pattern copy.** Switchboard should use a failure/signal intake path for fail-fix-early events and agent-as-sensor reports. | `BUG-15`, `BUG-8`, `QA-9` |
| `dbos_runtime.py` | DBOS as resumable infrastructure under Taikun-owned semantics. | **Do not import now.** Evaluate for slow background jobs only. Do not move hot coordination primitives onto DBOS. | `RECON-10` |
| `RunsView.tsx` / `GovernancePanel.tsx` | Per-run replay surface, cost receipt, policy binding display. | **Pattern copy.** Build Switchboard cockpit panels for task/session receipts, live side effects, approvals, and policy. | `HARDEN-6`, `RECON-9`, `HARDEN-13` |
| ActionEngine workflow DAG engine | Node execution, JSON DAG orchestration, workflow-specific node runner. | **Do not import.** Switchboard is not an in-run DAG engine. A workflow engine is a worker under Switchboard, not the coordination kernel. | Product boundary |

Direct-lift rule: if the code knows about ActionEngine workflows, OT points, documents,
tenant workflow tables, or DBOS APIs, pattern-copy the design instead of copying the code.

---

## 4. Switchboard product spec

### 4.1 Coordination receipts

Switchboard needs a receipt layer that answers: "What happened to this piece of work,
who controlled it, what did it cost, what evidence proved it, and what outcome changed?"

This should be a projection over the existing activity graph, not a second task table.

Minimum receipt fields:

| Field | Meaning |
|---|---|
| `receipt_id` | Stable id for the projected receipt. |
| `project` | Project namespace. |
| `task_id` | Work item. |
| `claim_id` | Claim that moved the task, if any. |
| `agent_id` | Agent or human actor. |
| `runtime` / `host_id` | Runtime and host context when known. |
| `status` | `open`, `claimed`, `running`, `awaiting_approval`, `blocked`, `in_review`, `done`, `void`, or `superseded`. |
| `started_at` / `terminal_at` | Lifecycle timestamps. |
| `evidence_refs` | Branch, head SHA, PR, merged SHA, offline evidence, CI, artifact, or audit export pointers. |
| `approval_refs` | Approval gates and decisions attached to the work. |
| `policy_refs` | Dispatch policy version, interrupt policy, WIP/resource rules. |
| `cost_refs` | Tally usage, provider reconciliation, and confidence grade pointers. |
| `failure_refs` | Fail-fix signals, reconcile findings, dead letters, rejected payloads. |
| `outcome_refs` | Verified outcome, KPI, merge/default-branch proof, accepted review, or task conversion. |

Rules:

- The append-only activity log remains source of truth.
- Corrections are appended and projected; historical receipt rows are not silently edited.
- Unknown required fields are red/yellow state, not empty success.
- Receipt APIs should explain which source events produced the projection.
- Receipt IDs should be stable enough for audit exports and replay.

Task anchor: `RECON-9`.

### 4.2 External side-effect ledger

Switchboard already has internal idempotency for many MCP writes. It also needs an explicit
ledger for external effects that can cause real-world duplication or spend.

Covered effects:

- GitHub writes: PR comments, status updates, merge/reconcile mutations.
- Agent Host actions: wake, stop, kill, restart, attach, resume.
- Human notifications: Slack, email, Teams, SMS, pager.
- Hosted dispatch: starting a managed runner or spending provider credits.
- Provider reconciliation pulls and audit exports.
- Any future integration that changes state outside the Switchboard database.

Proposed schema shape:

| Field | Meaning |
|---|---|
| `effect_key` | Deterministic key over project, effect type, target, resource, payload hash, and idempotency window. |
| `project` | Project namespace. |
| `effect_type` | `github_write`, `wake`, `runner_control`, `notify`, `provider_pull`, `audit_export`, etc. |
| `target` | External service or host. |
| `resource` | PR, thread, host, runner, user, provider account, or export id. |
| `task_id` / `claim_id` / `agent_id` | Work context that authorized the effect. |
| `status` | `requested`, `claimed`, `issued`, `verified`, `failed`, `dead_letter`, or `void`. |
| `payload_hash` | Hash of the normalized outbound payload. |
| `idem_key` | External idempotency key when the provider supports one. |
| `readback_json` | Provider response or verification evidence. |
| `retry_count` / `last_error` | Visible retry state. |
| `requested_at` / `issued_at` / `verified_at` | Effect lifecycle timestamps. |

Execution rules:

1. Compute `effect_key` before touching the external system.
2. Claim the effect atomically. If already `verified`, return the recorded proof.
3. If status is pending or uncertain, read back first before issuing again.
4. Use the retry helper only for transient transport/provider failures.
5. Confirm only after provider read-back or a clearly named weaker verification.
6. Dead-letter terminal failures with task/claim context and a failure-signal link.
7. Do not use visible fallback success for missing provider, missing token, invalid target,
   unauthorized actor, or malformed payload.

Task anchor: `HARDEN-21`. Retry helper anchor: `BUG-16`.

### 4.3 Fail-fix signal intake

ActionEngine's sensing queue pattern maps neatly to Switchboard's fail-fix-early policy.
When an agent, adapter, monitor, reconcile pass, or test sees missing/broken/invalid state,
it should emit a typed signal.

Minimum signal fields:

| Field | Meaning |
|---|---|
| `signal_id` | Stable signal id. |
| `project` | Project namespace. |
| `source` | Agent, adapter, monitor, test, reconcile, host, provider, or human. |
| `failure_class` | Missing data, stale branch, broken connection, invalid payload, auth failure, permission drift, provider mismatch, etc. |
| `severity` | Informational, warning, blocking, critical. |
| `task_id` / `claim_id` / `agent_id` | Context when known. |
| `expected_signal` | What should have been true. |
| `actual_signal` | What was observed. |
| `repro` | Command, API call, transcript pointer, or replay pointer. |
| `dedupe_key` | Stable key for grouping repeats. |
| `status` | `new`, `deduped`, `converted_to_task`, `fixed`, `accepted_risk`, or `void`. |

Task anchors: `BUG-15`, `QA-9`, `BUG-8`.

### 4.4 DBOS evaluation boundary

DBOS may be valuable for slow, resumable background work. It should not be placed in the
hot Switchboard kernel unless a later load test proves the latency and semantics fit.

Good DBOS candidates:

- replay simulation over long activity intervals;
- provider cost reconciliation;
- audit export generation;
- large import/normalization jobs;
- dispatch policy scorecards;
- managed-runner provisioning workflows that already leave visible effects.

Bad DBOS candidates for now:

- `claim_next` hot path;
- exact task claim;
- message delivery and ack;
- task/resource leases;
- heartbeat/presence;
- activity append and core provenance writes.

Task anchor: `RECON-10`.

---

## 5. Explicit non-borrows

Switchboard should not import:

- the ActionEngine app or workflow editor as a dependency;
- the JSON DAG execution engine for agent coordination;
- DBOS as the source of truth for task claims, leases, messages, or activity;
- OT/industrial control semantics, point writes, or compensation code;
- a duplicate planner database beside the Switchboard activity log;
- silent fail-open behavior for correctness paths;
- domain-specific ActionEngine tables where a Switchboard graph projection is enough.

The right mental model: ActionEngine is a strong reference implementation of durable
workflow operations. Switchboard is the cross-runtime coordination kernel that can dispatch,
observe, approve, and score ActionEngine-like workers.

---

## 6. Phased implementation

| Phase | Work | Task anchors |
|---|---|---|
| P0 | Document borrowing boundaries and task map. | `DOGFOOD-11` |
| P1 | Implement failure taxonomy, visible signal intake, and retry helper. | `BUG-15`, `BUG-16`, `QA-9` |
| P1/P2 | Add exactly-once external side-effect ledger for wakes, runner controls, GitHub/provider writes, and notifications. | `HARDEN-21` |
| P2 | Add coordination receipt projection and receipt API. | `RECON-9` |
| P2 | Use receipts in replay/simulation and dispatch policy scorecards. | `RECON-8`, `DISPATCH-7` |
| P2 | Evaluate DBOS for replay/reconcile/audit/provider jobs only. | `RECON-10`, `TALLY-4`, `HARDEN-13` |
| P2/P3 | Surface receipts, approvals, side effects, failures, and policy in the operator cockpit. | `HARDEN-6`, `ACCESS-10`, `HARDEN-13` |

---

## 7. Acceptance tests for future work

Future implementations should prove:

1. Retrying a transient outbound error never creates duplicate external effects.
2. Retrying after an uncertain external effect reads back before issuing again.
3. Missing token, missing target, malformed payload, expired approval, and unknown project
   all fail visibly.
4. A task/claim receipt can be rebuilt from append-only events after process restart.
5. Receipt projection explains the source events behind each derived field.
6. Side-effect dead letters include task, claim, agent, project, payload hash, and error.
7. DBOS-backed jobs can crash and resume without changing Switchboard's public protocol.
8. `claim_next`, exact claims, messages, leases, and activity append remain independent of
   DBOS unless a later architecture decision explicitly changes the hot-path boundary.
