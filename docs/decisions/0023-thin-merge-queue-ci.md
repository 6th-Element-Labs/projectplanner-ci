# ADR-0023 — Thin merge queue with one exact-SHA CI contract

- **Status:** Accepted historical decision; Decisions 2 and the audit-only clause
  of Decision 3 superseded by
  [ADR-0024](0024-merge-queue-admission-and-docs-lanes.md) on 2026-07-29
- **Date:** 2026-07-27
- **Author:** CI simplification
- **Supersedes:** ADR-0022 Decisions 2 and 5, and clarifies its manual recovery path
- **Relates to:** [ADR-0022](0022-one-fail-closed-ci-verdict.md) ·
  [CI strategy](../CI-STRATEGY.md)

## Context

The public mirror and App callback are required, and the native merge queue remains valuable
because parallel coding agents otherwise fight over a moving base. The failure was not the queue
shape itself. It was a fast/full workflow fork, process-state status fan-out, PR-blocking
wall-clock ratchets, and a custom eject/requeue lifecycle layered around the queue.

That combination made a technically green change depend on several independent callbacks and
recovery owners. Repairing the CI path also depended on the queue being healthy, creating a
circular outage.

## Decision

1. `projectplanner-ci:verify.yml` is the only required verification workflow.
2. PR heads, merge-group heads, and CI-repair heads execute the identical full
   `scripts/switchboard_ci.sh` contract, including Playwright.
3. All three purposes publish exactly one required context:
   **`Switchboard CI / VM gate`**. Purpose is audit metadata, never suite selection.
4. Wall-clock concurrent-load and cross-process timing ratchets run in scheduled,
   non-required monitoring. Their reports remain visible but cannot eject PRs or merge groups.
5. Claim, Work Session, exact-head review, remediation, and merge authorization remain
   Switchboard preconditions for arming auto-merge. They are not GitHub statuses.
6. Autopilot updates the PR, waits for the single required context, enables squash auto-merge
   once, and waits. GitHub's native queue owns admission and land order. Autopilot does not own
   a requeue effect; a persistent failure becomes an explicit operator decision.
7. The repair lane uses the same App mirror, workflow, script, context, and exact SHA. After
   one green run, a human administrator may squash-merge outside the queue only after recording
   the PR head, base SHA, run URL, operator, reason, and `ci-repair` designation. The repair
   command fails closed if the head or base moves or any exact-run evidence is missing.
8. Legacy public `backend-tests` and PAT-backed `pull-proof` workflows and the Plan VM
   claim-status timer are retired.

## Consequences

GitHub necessarily evaluates the required context on a PR head before queue admission and on
the distinct merge-group SHA before landing. Those are two executions against two different
objects, but they are one implementation and one contract—there is no alternate fast suite,
second workflow, or second status lifecycle.

The native queue remains the only landing serializer. Switchboard remains the authority for
whether an agent may arm auto-merge. Canonical merge provenance remains the only code path to
Done.

The emergency path bypasses queue health, not verification. It can repair the CI or queue
machinery without weakening the exact-SHA evidence required to merge.
