# ADR-0024 — Fast admission and Markdown merge-queue lanes

- **Status:** Accepted (operator decision, 2026-07-29)
- **Date:** 2026-07-29
- **Author:** CI simplification
- **Supersedes:** [ADR-0023](0023-thin-merge-queue-ci.md) Decisions 2 and the
  audit-only clause of Decision 3
- **Relates to:** [CI strategy](../CI-STRATEGY.md) ·
  [merge-queue runbook](../SWITCHBOARD-RUNBOOK.md)

## Context

The native merge queue solved the original parallel-agent traffic jam: agents no longer
need to rebase merely because another PR lands. GitHub constructs a temporary merge-group
commit from the current base and queued changes, then asks CI to verify that exact landing
candidate.

ADR-0023 deliberately made PR heads and merge groups run one identical full suite. That
removed a fragile collection of workflows, statuses, timers, and custom eject/requeue
effects. It also made every change pay for the full Python, Node, dependency, Chromium,
application, and Playwright setup twice.

PR #1063 exposed the pure-duplication case. Its PR head and merge-group commit had different
commit SHAs but the same Git tree (`11957c56dc693043759b2f1f5fb13ec2796cc656`), so two full
runs tested identical files. The merge-group run remains necessary when the base or earlier
queue entries change the candidate tree. The PR-head full run is not landing proof.

GitHub still requires the configured status before queue admission and again on the
merge-group SHA. The optimization therefore removes the duplicate **full suite**, not either
exact-SHA status decision.

## Decision

1. Keep one trusted default-branch `verify.yml`, one App-authenticated status context
   (`Switchboard CI / VM gate`), one exact-SHA mirror route, and the native merge queue.
2. A PR head runs the bounded **admission lane**:
   - verify the mirrored checkout equals the requested source SHA;
   - run Git's whitespace/error check on the head commit;
   - compile the Python tree without importing application dependencies; and
   - execute the CI lane selector's direct test.
3. A merge-group head runs the **full lane** by default, including
   `scripts/switchboard_ci.sh` and Playwright.
4. A merge group may run the **Markdown lane** only when all of these mechanically verified
   conditions hold:
   - GitHub supplied a full base SHA;
   - the base and source commits both exist in the mirrored checkout;
   - the base is an ancestor of the source;
   - the exact base-to-source diff is non-empty; and
   - every changed path has a case-insensitive `.md` suffix.
5. Missing, malformed, unreachable, non-ancestor, empty, or mixed-path evidence selects the
   full lane. Classification errors never select a faster lane.
6. The Markdown lane runs exact-diff whitespace checks, unresolved-conflict checks, and
   local-link target validation on changed Markdown files. Rename detection is disabled so a
   code-to-Markdown rename still exposes the removed code path and forces full CI. Markdown
   symlinks are ineligible. The lane does not install application dependencies, Node, or
   Chromium.
7. `ci_repair` always selects the full lane.
8. Every lane emits `switchboard.ci_lane_result.v1` with the requested SHAs, selected lane,
   reason, changed paths, and executed checks. Purpose now selects a trusted verification
   contract; callers still cannot choose arbitrary commands or status contexts.
9. The trusted workflow reads repository variable `SWITCHBOARD_CI_LANE_MODE`:
   - missing, unknown, or `full` forces every candidate to full CI;
   - `shadow` records the candidate lane but still runs full CI; and
   - `enforce` executes the mechanically selected lane.
10. The native queue remains the only landing serializer. Agents do not update or rebase a
   PR solely because the default branch advanced; the merge group owns current-base
   integration proof.

## Why this does not restore the old failure mode

The retired fast/full design coupled multiple workflows, status lifecycles, process-state
projections, wall-clock gates, and custom queue ejection/requeue behavior. This decision
changes none of those ownership boundaries:

- one workflow;
- one required context;
- one callback identity;
- one enqueue action;
- no custom requeue;
- no agent-driven base refresh loop; and
- canonical merge provenance still owns Done.

Only the work performed inside the trusted workflow differs by mechanically selected lane.

## Safety argument

The merge-group SHA is the only candidate GitHub can land. Code and mixed changes always run
the full suite on that SHA. A false Markdown classification would require every changed path
in the exact base-to-source Git diff to end in `.md`; any executable, workflow, fixture,
configuration, lockfile, or asset path forces full CI.

The PR-head admission lane can allow a deeper application failure to reach the queue. That is
an intentional throughput tradeoff, not a false-green landing: the full merge-group lane
must still pass before code merges. Queue ejection rate and time-to-terminal-status should be
observed. Evidence of repeated avoidable queue pollution is grounds to add a bounded impacted
test to admission, not to restore duplicate full suites by default.

## Rollout and rollback

1. Land the dispatcher, selector, tests, workflow source, and this ADR while the currently
   installed public workflow still runs the old full contract.
2. Reconcile `.github/workflows/verify.yml` onto
   `6th-Element-Labs/projectplanner-ci`'s protected default branch.
3. Set `SWITCHBOARD_CI_LANE_MODE=shadow` and verify candidate-lane receipts while every run
   still executes full CI.
4. Set `SWITCHBOARD_CI_LANE_MODE=enforce` and prove three canaries:
   - PR head reports `admission`;
   - Markdown-only merge group reports `docs`; and
   - code or mixed merge group reports `full`.
5. For every canary, verify the required status target URL, exact source SHA, base SHA where
   required, lane receipt, and canonical merge provenance.

Immediate rollback is setting `SWITCHBOARD_CI_LANE_MODE=full`; the trusted workflow then
forces every purpose to full CI while preserving the dispatcher input and receipt schema.
No branch-protection, ruleset, status-context, queue, credential, or reconciliation change
is required.

## Consequences

Ordinary code changes pay for one short admission run plus one authoritative full
merge-group run. Markdown-only changes pay for two short validations and no application
suite. The queue still protects against stale bases and interacting agent changes without
requiring agents to race each other through rebases.
