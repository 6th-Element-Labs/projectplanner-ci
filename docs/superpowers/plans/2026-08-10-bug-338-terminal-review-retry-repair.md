# BUG-338 Terminal Review Retry Repair Plan

## Goal

Prevent Task Execution from launching work on an immutable terminal task. When an authenticated operator retries a terminal task with a current, unresolved review contract, route that authorization into one idempotent cross-task repair task and preserve the source task's terminal history.

## Design

1. Add a terminal-retry router to Task Execution.
   - Detect `Done`, `Cancelled`, or `Canceled` before launching.
   - Require a single current-head review verdict, its durable remediation row, and the complete open finding set.
   - Reuse an existing linked repair task for the same remediation or submit exactly one dedicated BUG repair task.
   - Return the repair task/execution identity instead of claiming that the source task started.
   - Return a typed `terminal_task_requires_repair` refusal when no exact repair contract exists.

2. Bind operator authority to escalated review repairs.
   - Generate the authorization receipt from the authenticated retry actor/principal; never trust a caller-supplied boolean.
   - Preserve that receipt in the repair task's `review_repair` state.
   - Keep ordinary automatic-finding repair behavior unchanged.

3. Extend cross-task repair proof conservatively.
   - Without operator authority, only the existing exact automatic finding set is repairable.
   - With a bound operator receipt, require the exact union of automatic and escalation findings.
   - Resolve those findings only after an exact repair PR/head has a passing review, passing merge gate, and canonical merge provenance.
   - Record that human authority was used and do not count the result as hands-off.

## Test order

1. Add a failing end-to-end regression that reproduces the live MXBT-1 shape: terminal merged source, escalated current finding, operator retry, dedicated repair task, idempotent replay, exact reviewed merge, source finding resolution.
2. Add negative assertions for terminal tasks without a current repair contract and unbound operator identity.
3. Run the existing BUG-111 terminal cleanup, BUG-171 cross-task repair, BUG-172 proof-model, COORD-20 remediation, and Task Execution adapter suites.
4. Run `scripts/switchboard_ci.sh` before completion.

## Live recovery after deploy

Reconcile BUG-8 against its exact PR (already completed through `reconcile_task_merge`), invoke the repaired terminal retry on MXBT-1 to create the dedicated repair execution, and audit REPORT-17 independently. No source task is reopened and no terminal runner is retained.
