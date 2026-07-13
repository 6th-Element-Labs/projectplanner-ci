# Exception-only human escalation channel

COORD-6 implements the coordinator's **human delivery layer**. Normal progress
(claim, wake, nudge, In-Review monitoring) stays agent-to-agent. Humans are
interrupted only for actionable exception classes from
[Coordinator Operating Contract §5](COORDINATOR-CONTRACT.md).

Implementation: `coordinator_escalation.py`, wired from
`mission_coordinator.run_coordinator_tick`.

## When a human is paged

| Escalation class | Typical trigger |
|---|---|
| `human_gate_required` | `human_gate`, bug-intake conversion, SME/security review |
| `budget_breach` | IRQ/NMI budget envelope |
| `failed_gate` | Required CI/review red after bounded retries; broken task links |
| `stale_branch_conflict` | PR conflicted / merge_gate branch_stale |
| `missing_provenance` | Done without merged_sha / reconcile drift |
| `absent_permission` | Missing scope/tier for an intended action |
| `unreachable_agent_no_host` | `request_wake` finds no eligible host |
| `unbound_identity` | Takeover / identity risk on the target |
| `ambiguous_requirements` | Breakdown / unclear acceptance needs a decision |
| `security_secrets_boundary` | Secrets or security boundary |
| `policy_violation` | Action would violate project policy |
| `repeated_failures` | Recurring monitor / retry ceiling |
| `red_ci_product_judgment` | Red CI that needs product judgment, not another silent retry |

Agent-lane actions (`claim_task`, `resume_or_claim`, `verify_merge_provenance`)
**never** call this channel.

## Notification shape (`switchboard.coordinator_escalation.v1`)

Every human notification includes:

1. **exact task** (`task_id`, optional `deliverable_id`)
2. **failed condition** (why the coordinator cannot proceed)
3. **recommended choices** (id / label / effect)
4. **minimum decision needed** (one-line ask)

Delivery path (mirrors `run_reconcile_alerts`):

1. `send_agent_message` → `switchboard/operator` with `signal=coordinator_escalation`,
   `requires_ack=true`, high priority
2. Optional Slack/email via `notify.send` (dry-run when unconfigured)
3. Activity kind `coordinator.escalation`
4. Idempotent under `coord-esc:{project}:{class}:{task}:{signature}:{alert}:{window}`

## Tick integration

On `human_required`, the mission coordinator classifies each escalation action and
delivers. On `dispatch_blocked`, it escalates only for exception reasons (no host,
absent permission, unbound identity) — a busy/empty claim queue stays silent to
humans.

COORD-3 continues to record `decision_kind=human_escalation` with the delivery
receipt under `result.human_notifications`.
