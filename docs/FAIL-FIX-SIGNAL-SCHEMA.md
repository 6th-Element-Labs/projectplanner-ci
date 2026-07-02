# Fail-Fix Signal Schema

Status: `BUG-15` platform contract

Switchboard uses `fail_fix_signal.v1` whenever an agent, adapter, monitor, reconcile pass, test,
or operator surfaces a failure that must not be hidden behind placeholder values, silent defaults,
or optimistic status.

The signal is not a replacement for a BUG task. It is the common vocabulary carried by BUG intake,
reconcile findings, monitor events, visible task-comment fallbacks, and QA-9 negative tests.

## Required Fields

| Field | Meaning |
|---|---|
| `source` | Agent, adapter, monitor, test, reconcile, host, provider, or human. |
| `failure_class` | One of the canonical classes below. |
| `severity` | `low`, `medium`, `high`, or `critical`. |
| `affected_surface` | UI, MCP, REST, adapter, reconcile, CI, docs, scheduler, auth, etc. |
| `observed_behavior` | What actually happened. |
| `expected_behavior` | What should have happened. |
| `repro_steps` | Command, API call, transcript pointer, or replay pointer. |
| `evidence` | Logs, payload, URL, PR/check, screenshot, trace, or command output summary. |
| `task_id` | Source task when known; use `null` only for board-level findings. |

## Failure Classes

| Class | Default severity | Expected signal |
|---|---|---|
| `missing_data` | `medium` | Required data is present before workflow execution continues. |
| `broken_connection` | `medium` | The dependency returns a structured response or a loud connection error. |
| `invalid_input` | `medium` | The invalid value is rejected before downstream state changes. |
| `stale_branch` | `high` | The current branch, head SHA, and canonical main proof are reachable. |
| `absent_permission` | `high` | The action is denied with the missing authority named. |
| `malformed_payload` | `medium` | Payload shape is validated and malformed input fails closed. |
| `failed_gate` | `high` | The gate failure is visible and blocks release/dispatch until repaired. |
| `unreachable_agent` | `medium` | Delivery, mailbox, wakeability, and fallback state are explicit. |
| `unbound_identity` | `high` | The runtime identity is registered, bound, and visible to operators. |
| `hidden_fallback` | `critical` | Fallbacks are named and preserve a red/yellow auditable signal. |

## Consumption Rules

BUG intake:
- `submit_bug` validates `failure_class` against this taxonomy.
- Submitted BUG tasks store `bug_report.failure_class_detail` and a nested
  `bug_report.fail_fix_signal` when a failure class is supplied.
- Invalid classes fail closed and return the schema instead of creating noisy BUG work.

Reconcile:
- Every reconcile finding carries `failure_class` and `expected_signal`.
- Reconcile alert messages include the failure class beside each finding code.

Monitors:
- Ack timeouts are classified as `unreachable_agent`.
- Missing monitor targets are classified as `missing_data`.
- Monitor result payloads, timeout activity, and fired event summaries preserve the class.

Task comments:
- Visible fallback comments created for unreachable delivery carry `failure_class`.
- Shared-token writes without a bound active agent carry `unbound_identity`.

QA-9:
- Negative tests should assert each failure is loud, typed, audited, and does not silently create
  success state.
- If a negative test finds a real product bug, file it with `submit_bug` using this taxonomy.
