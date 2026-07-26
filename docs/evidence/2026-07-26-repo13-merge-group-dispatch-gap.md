# REPO-13 merge-group CI dispatch gap

**Captured:** 2026-07-26
**Affected PR:** [#940](https://github.com/6th-Element-Labs/projectplanner/pull/940)
**Related task:** `REPO-13`
**Classification:** merge-queue automation and observability breakdown

## Outcome

PR #940 passed every required branch-head gate but remained in GitHub's merge queue because
the temporary merge-group SHA received no CI dispatch from Switchboard. The queue was waiting
for checks that had never been started.

This was not a documentation-content failure, a test failure, or a missing review. It was a
handoff failure between GitHub creating the merge group and Switchboard mirroring that exact SHA
to the CI repository.

## Evidence

The PR head was `2d2797ba6751e19b2a142e81b2bb680bfffeb7cd`.

| Time (UTC) | Evidence |
|---|---|
| 02:57:38 | `Switchboard / claim gate` passed |
| 03:04:17 | `Switchboard CI / VM gate` passed |
| 03:04:18 | `Switchboard UI / Playwright` passed with no skips |
| 03:05:05 | GitHub enqueued PR #940 |
| 03:05:06 | `Switchboard / merge authorization` passed |
| 03:21 onward | GitHub held the PR in `AWAITING_CHECKS` |

GitHub created:

- merge-group SHA: `7c3c123c03d63a57fe80ab95c80620815b3d4f62`
- merge-group ref:
  `refs/heads/gh-readonly-queue/master/pr-940-ca21520690d221347faecc5ef9b7a0af6445dac1`

Switchboard's durable reads returned:

- no external CI run for the merge-group SHA;
- no decision episode for `REPO-13`;
- no merge-group dispatch or failure receipt in `REPO-13` activity;
- task status still `In Review`, with no canonical merge provenance.

The missing receipt means the evidence cannot distinguish whether GitHub's `merge_group`
webhook was not delivered, was not consumed, or was consumed without recording the result.
That ambiguity is itself part of the breakdown.

## Recovery attempts

1. Direct use of `dispatch_scratchpad_ref` failed before a provider write because the worker
   had no `SWITCHBOARD_CI_DISPATCH_TOKEN`, `SWITCHBOARD_CI_GITHUB_TOKEN`,
   `PM_GITHUB_TOKEN`, or `GITHUB_TOKEN`.
2. The Switchboard external-CI command failed with `mirror_sync_failed` because neither the
   temporary worktree nor the Dropbox checkout was visible as a local checkout to the
   coordinator.
3. The immutable merge-group ref was fetched from the canonical repository and its exact SHA
   was verified.
4. That exact SHA was pushed to the dedicated CI branch
   `ci/mg-7c3c123c03d6` in `6th-Element-Labs/projectplanner-ci`.
5. The push triggered Actions run
   [30186758908](https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/30186758908).
   Both required merge-group contexts appeared as pending on the correct SHA.

No other PR, branch, task, or merge-group SHA was dispatched or modified.

## Relationship to prior fixes

`BUG-173` fixed missing or stale merge-authorization publication. That fix was working here:
the PR-head merge authorization was green. This incident occurred one handoff later because
the merge-group CI run itself was absent.

The current code comments identify `BUG-180` as protection against a recorded merge-group
dispatch that points to a dead run. This incident exposed the preceding case: there was no
durable dispatch attempt or failure receipt at all.

## Required durable behavior

1. Every GitHub `merge_group` event produces one durable receipt keyed by repository,
   merge-group SHA, and action.
2. The receipt records webhook delivery, inbox application, dispatch decision, mirror push,
   workflow run ID, and any failure class as separate stages.
3. Missing required contexts on an `AWAITING_CHECKS` merge group trigger bounded,
   idempotent reconciliation of that exact immutable SHA.
4. Recovery does not depend on an operator checkout being visible to the coordinator.
5. A green PR-head result is never presented as proof that merge-group CI ran.
6. Acceptance includes a protected PR reaching canonical `master` without an operator status
   write or manual mirror push.

## Current status at capture

The manual exact-SHA mirror trigger was accepted. Merge-group run 30186758908 subsequently
passed both required contexts, and GitHub marked the queue entry mergeable. Canonical
completion still required GitHub merge provenance and Switchboard reconciliation.

## Operator postmortem

The agent now registered as `codex/LEEROY-JENKINS` made avoidable mistakes while handling this
documentation change:

1. It reran CI once on PR #935, which belonged to another agent. It made no code, commit, or
   merge change there, but consuming that CI was outside its scope.
2. It polled GitHub REST too aggressively and exhausted the shared account's REST rate limit,
   impairing subsequent log reads.
3. It turned a small Markdown merge into prolonged queue intervention and created a separate
   follow-up PR before keeping the work tightly bounded.
4. Its first PR #940 CI run failed because the initial index simplification removed required
   public-package links. The links were restored and the exact-head checks then passed.

These were operator errors, not failures by the agents that owned other work.

The controls for future work are:

- Never rerun, cancel, update, review, or merge another agent's PR unless ownership is
  explicitly transferred.
- Resolve task, branch, PR, and exact head SHA before any write.
- Use bounded status reads and durable Switchboard receipts; do not substitute rapid provider
  polling for missing observability.
- For a queue stall, record the missing handoff first. Intervene only on the owned immutable
  merge-group SHA, with an idempotency key and a verified recovery path.
- Keep documentation-only changes documentation-only, run link checks before opening the PR,
  and stop after canonical merge and reconciliation evidence exists.
