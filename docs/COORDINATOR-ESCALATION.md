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
| `budget_breach` | IRQ/NMI budget envelope |
| `ambiguous_requirements` | Breakdown / unclear acceptance needs a decision |
| `security_secrets_boundary` | Secrets or security boundary |
| `repeated_failures` | Recurring monitor / retry ceiling |

Everything else is a machine-handled signal, not a notification: failed CI,
conflicts, stale branches, missing provenance, capacity/no-host, identity repair,
and legacy policy labels remain in the coordinator/agent loop. They can still be
recorded as typed failure classes, but `should_notify_human` returns false.

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

On `human_required`, the mission coordinator classifies each action and delivers
only the four irreducible decision classes above. `dispatch_blocked` remains
visible and repairable but does not page for capacity, host, permission, identity,
or an empty claim queue.

COORD-3 continues to record `decision_kind=human_escalation` with the delivery
receipt under `result.human_notifications`.
