# Cross-Project Completion Handoff Design

**Status:** Approved by the operator

**Task:** BUG-337

## Problem

An Agent Host enrolled in one project can be granted execution authority in another project. The authentication and token-issuance paths already recognize that grant through `check_agent_host_identity(host_id, principal_id, project=target_project)`. Runner persistence still reclassifies host authority by querying the target project's local `principals` and `agent_host_enrollments` tables.

For a shared host enrolled in Atlas and executing Maxwell work, that stale target-local test evaluates false. A valid terminal completion receipt then fails the `narrow_host` check in `terminal_ack_claim_completion_in`. The caller interprets the missing completion result as an orphaned terminal runner, archives the Work Session, releases the claim, and leaves the task without canonical PR/head evidence. Autopilot subsequently and correctly starts implementation again.

## Requirements

1. A granted Agent Host must not require a duplicate principal or enrollment row in the target project.
2. Terminal completion authorization must use `check_agent_host_identity` for the exact host, principal, and target project.
3. Exact runner, host, generation, role, lease epoch, and stopping-lease checks remain enforced.
4. A terminal runner with a pending `completion_handoff` must never enter yielded, terminal-task, Work Session archival, or orphan-ownership cleanup when acknowledgment cannot finalize.
5. A failed acknowledgment remains visibly pending with a typed reason and can be retried idempotently.
6. Autopilot, merge authority, and Done provenance remain unchanged.
7. Agent Host must retain the durable stop receipt until a response has neither `error` nor `error_code`.
8. Verified-kill cleanup must preserve an unacknowledged completion handoff and leave it discoverable for Host replay.

## Design

### Host authority

`_upsert_runner_session_in` resolves the submitted host through `check_agent_host_identity(submitted_host, principal_id, project=project)`. A result with `required=true` and `allowed=true` is the authoritative Agent Host classification, including cross-project grants. A required but denied identity fails before runner mutation. Operator and non-host callers continue through their existing paths.

The target-local `principals` lookup and duplicate target-project enrollment requirement are removed. The target project's `agent_hosts` registration and any existing runner ownership tuple still have to match the exact principal.

### Terminal completion

`terminal_ack_claim_completion_in` no longer accepts a caller-computed `narrow_host` boolean. It resolves the authoritative host identity itself from the persisted runner host, supplied principal, and target project. All existing execution identity and stopping-lease checks remain.

### Pending-handoff protection

If a terminal receipt contains a pending `completion_handoff` but canonical finalization cannot run, runner persistence returns a typed `completion_handoff_pending` result and skips every generic terminal cleanup path. The claim and Work Session remain owned and retryable. A later valid receipt can finalize the same handoff idempotently.

The typed result is also a top-level retry signal. Agent Host retains its persisted stop receipt on either `error` or `error_code`, and the bounded pending-completion query continues returning terminal runners whose handoff has not been acknowledged.

The verified-kill control path applies the same fence. It may record physical process death, but it does not release claim ownership, terminalize the execution lease, or rewrite the wake as failed while a completion handoff is unacknowledged.

## Verification

The regression test creates an Agent Host principal and enrollment only in a source project, grants it the target project, starts and claims a managed target-project execution, records PR/head/test evidence through `complete_claim`, and reports the terminal runner receipt. It proves the task reaches `In Review`, canonical git state contains the submitted PR/head, the claim and Work Session complete, and no orphan-release activity is emitted.

A failure-path assertion temporarily invalidates the stopping lease, proves the terminal receipt remains visibly pending without cleanup, restores the lease, and proves replay completes normally.

Adapter-level coverage proves a pending acknowledgment retains its on-disk stop receipt across initial delivery and replay. A verified-kill race proves the claim, Work Session, stopping lease, and wake remain intact and that pending discovery leads to successful terminal replay.
