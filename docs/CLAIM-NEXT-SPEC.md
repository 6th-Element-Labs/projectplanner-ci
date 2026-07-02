# `claim_next` Spec - TXP work dispatch

- **Status:** P0 implemented through DISPATCH-2
- **Date:** 2026-06-28
- **Product:** Switchboard
- **Protocol profile:** `+TXP` - task exchange / work dispatch, layered above `IXP-core`
- **Purpose:** turn Switchboard from a shared coordination board into an active scheduler
  that assigns the right unblocked work to the right agent at the right cost.

> Without `claim_next`, Switchboard is a place agents report to. With `claim_next`,
> Switchboard becomes the thing assigning work across the fleet.

---

## 1. Product thesis

The board already knows tasks, dependencies, lanes, status, activity, resource claims, and
agent presence. `claim_next` is the atomic operation that converts that knowledge into
dispatch:

- find work that is ready;
- match it to a live agent's lane/capabilities;
- avoid tasks already claimed;
- reserve the task in one transaction;
- return the minimum useful context and model/budget guidance.

This is the moment Switchboard becomes a scheduler instead of a passive ledger.

---

## 2. Relationship to `IXP-core`

`claim_next` is not part of `IXP-core`.

It builds on core primitives:

| `IXP-core` primitive | TXP use |
|---|---|
| presence | know which agents are available |
| leases | reserve the task and optional resources |
| messages/signals | dispatch, redirect, stop, approval |
| delta feed | notify peers of task claims/completions |
| idempotency | make retries safe |
| auth/actor | prevent spoofed assignment and spend |

An implementation may advertise:

- `IXP-core` only;
- `IXP-core + TXP`;
- `IXP-core + TXP + OXP`.

---

## 3. Core operation

### 3.1 `claim_next`

MCP:

```text
claim_next(project, agent_id, lanes?, capabilities?, max_risk?, max_budget_usd?, idem_key?)
```

REST:

```http
POST /txp/v1/claim_next
```

Request:

```json
{
  "project": "helm",
  "agent_id": "codex/CHART#b12e",
  "lanes": ["CHART", "ENGINE"],
  "capabilities": ["typescript", "cpp", "tests"],
  "model_capabilities": {
    "provider": "openai",
    "models": ["gpt-5", "gpt-5-mini"],
    "tiers": ["high_reasoning", "balanced"],
    "privacy": ["no_external_training"],
    "enforcement": ["advisory", "claim_gate", "runner_enforced"]
  },
  "max_risk": "medium",
  "max_budget_usd": 12.0,
  "idem_key": "codex-CHART-b12e-0001"
}
```

Response when work is claimed:

```json
{
  "claimed": true,
  "claim_id": "taskclaim_01J...",
  "task": {
    "id": "CHART-8",
    "title": "Expose chart query in client",
    "lane": "CHART",
    "status": "In Progress",
    "priority": 80,
    "risk": "medium",
    "exit_criteria": ["client can query rendered chart metadata", "tests pass"]
  },
  "lease": {
    "lease_id": "lease_01J...",
    "resource_type": "task",
    "names": ["CHART-8"],
    "expires_at": "2026-06-28T01:00:00Z"
  },
  "context": {
    "decisions": ["ADR-0001"],
    "blocking_notes": [],
    "recommended_files": ["web/chart.js", "app.py"],
    "delta_cursor": 4921
  },
  "budget": {
    "budget_usd": 12.0,
    "spent_usd": 1.35,
    "remaining_usd": 10.65,
    "status": "ok"
  },
  "dispatch_reason": {
    "policy": "score.v1",
    "score": 10680,
    "candidate_count": 3,
    "skipped": {
      "active_claim": 1,
      "status": 10,
      "lane": 2,
      "dependencies": 1,
      "capability_mismatch": 1,
      "risk": 0,
      "budget": 0
    },
    "required_capabilities": ["typescript", "tests"],
    "matched_capabilities": ["typescript", "tests"],
    "factors": {
      "blocking": 10000,
      "sort_order": 520,
      "lane_affinity": 250,
      "capability_fit": 200,
      "risk_fit": 80,
      "budget_fit": 100
    }
  },
  "recommendation": {
    "model_tier": "balanced",
    "allowed_models": ["gpt-5-mini", "claude-sonnet"],
    "enforcement": "claim_gate",
    "reason": "UI/backend integration with existing tests"
  }
}
```

Response when no work is available:

```json
{
  "claimed": false,
  "reason": "no_unblocked_work",
  "retry_after_seconds": 60,
  "cursor": 4921,
  "dispatch_reason": {
    "policy": "score.v1",
    "candidate_count": 0,
    "skipped": {
      "active_claim": 0,
      "status": 20,
      "lane": 1,
      "dependencies": 2,
      "capability_mismatch": 1,
      "risk": 0,
      "budget": 0
    }
  }
}
```

### 3.2 Idempotency

If the same `agent_id` repeats the same `idem_key` and request body, return the original
claim. If the same key is reused with a different body, return conflict.

This prevents double assignment when an adapter retries after a timeout.

---

## 4. Eligibility rules

A task is eligible when all are true:

1. It belongs to the requested project.
2. Its lane matches the requested lane set or the agent is lane-agnostic.
3. Its status is in the configured ready set.
4. All hard dependencies are complete or waived.
5. It is not blocked on human approval.
6. It has no active task claim.
7. Its required resources are not held by another active lease.
8. Its risk is within the agent's declared limit.
9. Its model policy is compatible with the agent's advertised model/provider capability.
10. Its remaining budget is compatible with the agent's cost profile.
11. The authenticated principal is allowed to claim work in that project.

Default ready statuses:

```text
Ready, Todo, Backlog
```

Default statuses excluded:

```text
Blocked, In Progress, Review, Done, Canceled
```

Existing deployments may map their local statuses into this set.

### 4.1 Human approval gates

Some tasks are intentionally not claimable even when their dependencies are complete. Bug intake
conversion is the first concrete case: a Bug Intake Agent may prepare implementation work, but the
task must not enter dispatch until a human operator or explicit coordinator policy approves it.

Human-gated work carries structured task state under `agent_state.human_gate`:

```json
{
  "required": true,
  "source_bug_task_id": "BUG-123",
  "target_workstream": "HARDEN",
  "severity": "high",
  "approval_reason": "Why this bug should become implementation work",
  "approved_by": "switchboard/operator",
  "approved_at": "2026-07-02T00:00:00Z"
}
```

Until approval is present:

- `claim_task` fails closed with `reason=human_approval_required`;
- `claim_next` skips the task and increments `dispatch_reason.skipped.human_approval`;
- task detail exposes `human_gate.status=human_approval_required`.

This keeps intake agents useful as sensors and triagers without making them a hidden
implementation-dispatch path.

---

## 5. Selection policy

Within the eligible set, Switchboard scores tasks rather than simply taking the top row.

Default score components:

| Component | Direction |
|---|---|
| explicit priority | higher first |
| dependency critical path | higher first |
| age/starvation boost | older ready work rises |
| lane affinity | stronger match rises |
| capability fit | stronger match rises |
| risk fit | avoid over-risking weak agents |
| model policy fit | prefer an agent/runtime that can run the required model tier within policy |
| budget fit | prefer agents likely to finish inside remaining budget |
| reliability history | prefer agent/runtime with lower cost per verified outcome |
| WIP limits | avoid overloading one agent/runtime |

DISPATCH-1 used deterministic first-ready selection. DISPATCH-2 implements `score.v1`:

- hard filters: ready status, lane, dependencies, active claim, declared required
  capabilities, `max_risk`, and `max_budget_usd`;
- scoring factors: blocking status, sort order, lane affinity, capability fit, risk fit,
  budget fit, and Tally outcome signals;
- response explanation: `dispatch_reason` records the selected score, factor breakdown,
  required/matched capabilities, candidate count, and skipped counts by constraint;
- guidance: `budget.status` and `recommendation.model_tier` are returned with the claim.

The first version is intentionally deterministic and explainable. Later versions can add
fleet reliability and cost-per-outcome history without changing the envelope.

---

## 5.1 Task model policy

`claim_next` must support explicit task model policy so Switchboard can route expensive
reasoning to work that justifies it and keep routine work on cheaper models.

Suggested task field:

```json
{
  "model_policy": {
    "tier": "high_reasoning",
    "allowed_models": ["gpt-5", "claude-opus-4.8"],
    "disallowed_models": ["cheap-fast"],
    "max_budget_usd": 40.0,
    "privacy": "no_external_training",
    "requires_tools": ["git", "github", "mcp"],
    "approval_required_for_upgrade": true,
    "enforcement": "claim_gate"
  }
}
```

Policy meanings:

| Field | Meaning |
|---|---|
| `tier` | Abstract model class such as `cheap_fast`, `balanced`, `high_reasoning`, `specialist` |
| `allowed_models` | Concrete models allowed by the task or org policy |
| `disallowed_models` | Concrete models blocked for quality, privacy, cost, or compliance reasons |
| `max_budget_usd` | Task-level spend ceiling used by Tally and dispatch |
| `privacy` | Required provider/data-handling posture |
| `requires_tools` | Runtime/tool capabilities required to do the work |
| `approval_required_for_upgrade` | Whether moving to a more expensive tier needs human approval |
| `enforcement` | `advisory`, `claim_gate`, or `runner_enforced` |

Enforcement tiers:

- `advisory`: return the recommendation and record drift if the actual model differs.
- `claim_gate`: refuse the claim when the agent advertises incompatible model/provider
  capability, unless an operator override is present.
- `runner_enforced`: the Agent Host or gateway launches/configures the requested model and
  blocks execution when it cannot.

For app-based runtimes where Switchboard cannot control the model directly, the adapter must
advertise `model_verification=reported` or `unknown`. For API-run agents, the gateway/host can
advertise `model_verification=enforced`.

---

## 6. Atomicity

`claim_next` must run inside one database transaction:

1. Read eligible tasks.
2. Select one deterministically.
3. Insert a task claim.
4. Insert or reuse a task lease.
5. Update task status/assignee if the local board uses those fields.
6. Append activity events.
7. Return the full claim result.

If two agents call concurrently, only one can receive a given task. The loser receives a
different task or `claimed=false`.

Suggested table:

```sql
CREATE TABLE IF NOT EXISTS task_claims (
  id             TEXT PRIMARY KEY,
  project        TEXT NOT NULL,
  task_id        TEXT NOT NULL,
  agent_id       TEXT NOT NULL,
  principal_id   TEXT NOT NULL,
  status         TEXT NOT NULL,       -- active | completed | abandoned | expired | revoked
  claimed_at     REAL NOT NULL,
  expires_at     REAL NOT NULL,
  completed_at   REAL,
  abandon_reason TEXT,
  idem_key       TEXT
);

CREATE INDEX IF NOT EXISTS ix_task_claims_active
ON task_claims(project, task_id, status, expires_at);
```

---

## 7. Claim lifecycle operations

### 7.1 `peek_next`

Returns the next likely task without claiming it. For UI/operator explanation only; agents
should use `claim_next`.

```text
peek_next(project, agent_id, lanes?, capabilities?) -> candidate[]
```

### 7.2 `renew_claim`

Extends a live claim TTL.

```text
renew_claim(project, claim_id, ttl_min=30) -> claim
```

Adapters should renew before half the TTL has elapsed.

### 7.3 `complete_claim`

Marks the claim complete and records evidence.

```text
complete_claim(project, claim_id, evidence, final_status?, outcome_id?) -> claim
```

Completion does not have to mean the task is verified: omit `final_status` to move the task to
`In Review`. Agents should not pass `final_status="Done"`; if they do, Switchboard records the
attempt and still keeps the task `In Review`. Code tasks should include branch/head SHA/PR evidence.
`Done` is reserved for GitHub/default-branch provenance after the work is merged, squash-merged, or
rebased into the intended branch.

### 7.4 `abandon_claim`

Releases work back to the queue with a reason.

```text
abandon_claim(project, claim_id, reason, partial_evidence?) -> claim
```

Reasons:

- `blocked_dependency`
- `missing_context`
- `budget_exhausted`
- `runtime_failed`
- `operator_redirect`
- `agent_unsuitable`
- `other`

### 7.5 `revoke_claim`

Operator/system revokes a claim, releases its task lease, and sends the displaced holder an
ack-required stop signal.

```text
revoke_claim(
  project,
  claim_id,
  reason,
  reassign_to?,
  sort_order?,
  partial_evidence?,
  notify=true,
  ack_deadline_minutes=5
) -> claim
```

Effects:

- `task_claims.status` moves from `active` to `revoked`;
- the task resource lease is released;
- the task returns to `Not Started`;
- `assignee` is set to `reassign_to` when provided, otherwise cleared;
- `sort_order` is updated when provided, so the scheduler can reprioritize the returned work;
- `partial_evidence` is preserved in the activity log and git-state evidence when branch/SHA/PR
  fields are present;
- the displaced agent receives an ack-required `claim_revoked` message;
- stale `complete_claim` / `abandon_claim` calls against the revoked claim fail with
  `claim is not active`.

---

## 8. Context returned with a claim

`claim_next` should return enough context to start without dumping the whole board.

Recommended context fields:

| Field | Meaning |
|---|---|
| `task` | the task object |
| `dependencies` | recently completed dependencies and evidence |
| `decisions` | relevant active decisions |
| `recommended_files` | files likely in scope |
| `leases_to_claim` | suggested resources |
| `blocking_notes` | known hazards |
| `delta_cursor` | cursor for future polling |
| `budget` | spent/remaining budget |
| `model_recommendation` | suggested model tier and reason |
| `acceptance_checks` | tests/verifications expected |

This is the context-economy lever: one dispatch call should hand the agent the minimum
optimal context, not force it to reread the entire project.

---

## 9. Model and cost guidance

`claim_next` should return a recommended model tier, not a vendor-specific command.

Example:

```json
{
  "model_recommendation": {
    "tier": "low_cost",
    "allowed_tiers": ["low_cost", "balanced"],
    "avoid_tiers": ["frontier"],
    "reason": "Mechanical docs edit with low blast radius"
  }
}
```

The adapter maps tiers to runtime-specific models.

Suggested tier inputs:

- task risk/blast radius;
- uncertainty;
- code vs docs vs review;
- remaining budget;
- agent/runtime historical reliability;
- whether the task is on the critical path.

This keeps Switchboard buyer-aligned: spend more only when the outcome justifies it.

---

## 10. Workspace and fairness guarantees

`claim_next` must fail closed on unknown projects and must never cross workspace boundaries.

Fairness requirements:

- no agent may hold more than its configured active claim limit;
- a task's score should receive an age boost to avoid starvation;
- lanes may have quotas or weights;
- operator-pinned tasks outrank normal scoring;
- abandoned tasks should cool down briefly before being reassigned to the same agent.

---

## 11. Activity events

Every lifecycle transition appends activity:

- `task.claimed`
- `task.claim.renewed`
- `task.claim.completed`
- `task.claim.abandoned`
- `task.claim.revoked`
- `task.claim.expired`
- `task.dispatch.skipped`

Events include:

```json
{
  "kind": "task.claimed",
  "project": "helm",
  "task_id": "CHART-8",
  "claim_id": "taskclaim_01J...",
  "agent_id": "codex/CHART#b12e",
  "actor": "principal/codex-chart",
  "score": 87.4,
  "reason": "highest_priority_unblocked"
}
```

These events feed delta polling, Tally, reliability scoring, and the operator UI.

---

## 12. Conformance tests

`+TXP` conformance requires:

1. A blocked task is never claimed.
2. A task with incomplete dependencies is never claimed.
3. Two concurrent agents cannot claim the same task.
4. A repeated `idem_key` returns the same claim.
5. Unknown project fails closed.
6. Claim TTL expiry returns work to the queue.
7. `complete_claim`, `abandon_claim`, and `revoke_claim` release the task lease.
8. `revoke_claim` notifies the displaced agent and prevents stale completion.
9. Activity events are emitted for every transition.
10. `claim_next` returns model/budget guidance when Tally data exists.
11. Selection is deterministic for equal inputs.

---

## 13. Exit criteria

`claim_next` is product-ready when:

- at least two live agents can pull work from the same lane without duplicate assignment;
- dependencies and active claims are enforced atomically;
- operator UI can explain why a task was selected or skipped;
- adapters can call `claim_next` at startup and after completion;
- task claims feed Tally/reliability metrics;
- the board visibly behaves like a scheduler, not only a reporting surface.

## 14. Implementation Notes

Capability requirements are optional. P0 supports either
`agent_state.dispatch.required_capabilities` or text declarations such as
`requires capabilities: docs, tests` in description/criteria fields. Tasks without declared
requirements remain claimable by lane-matched agents.

Budget enforcement is conservative: when `max_budget_usd` is supplied, a task whose existing
Tally spend already exceeds that ceiling is skipped. Selected tasks return `budget_usd`,
`spent_usd`, `remaining_usd`, and `status`.

Risk enforcement is driven by task `risk_level` and caller `max_risk` using the order
`low < medium < high < critical`. Unknown or unspecified risk remains eligible unless the task
declares a higher known risk than the caller accepts.
