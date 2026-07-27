# ADR-0022 — One fail-closed CI verdict

- **Status:** Accepted (operator decision, 2026-07-27; amended by CI-17)
- **Date:** 2026-07-27
- **Author:** CI-16 simplification
- **Amends:** [ADR-0020 — Merge gates observe](0020-merge-gates-observe-not-enforce.md)
- **Relates to:** [CI strategy](../CI-STRATEGY.md)

## Context

The public `projectplanner-ci` mirror is required because public GitHub Actions
minutes are unlimited while the private repository exhausts its allowance. The
mirror route had accumulated two required technical statuses, a PAT fallback for
status callbacks, and a merge-authorization projection on temporary merge-group
SHAs. These extra paths did not add independent test coverage. They did add
credentials, callbacks, retry conditions, and ways for the native queue to wait
forever.

The 2026-07-26 outage demonstrated the failure mode: the shared bot PAT exhausted
its rate-limit budget, the fallback was selected silently, and CI failed before
running tests. Moving the callback to a dedicated App restored service but left
the redundant paths available to recreate the incident.

## Decision

1. GitHub requires one technical verdict: **`Switchboard CI / VM gate`**.
2. A PR head earns admission to the native merge queue through the fast,
   impacted-test scope. The queue-generated merge-group SHA earns the same verdict
   only after the full `scripts/switchboard_ci.sh` suite and Playwright. The
   canonical merged SHA therefore always has full exact-SHA evidence without
   duplicating the full suite before queue admission.
3. The public workflow mints a dedicated GitHub App installation token. App ID and
   private key are mandatory. There is no PAT fallback.
4. The executable workflow comes only from `projectplanner-ci`'s trusted default
   branch. Mirrored agent branches supply code, never workflow authority. A
   secret-free suite job checks out the exact scratchpad ref; separate announce
   and report jobs mint the status-only App credential.
5. A merge-group webhook dispatches the temporary exact SHA through the same
   technical route in full mode. The claim status remains a PR-scoped advisory.
   Merge authorization stays internal to Switchboard instead of publishing a
   misleading second GitHub status lifecycle.
6. `external_ci_mirror` remains the single mirror engine. The older private
   checkout/repository-dispatch pull route and duplicate backend/sharded workflows
   are retired. `workflow_dispatch` on the trusted workflow is the manual exact-SHA
   recovery path.
7. The existing context name is retained to avoid a simultaneous rename migration.
   Its description and evidence define the broader full-suite-plus-Playwright
   meaning.

## Consequences

The required path is:

```text
exact canonical SHA
  -> external_ci_mirror
  -> disposable public code ref
  -> trusted default-branch verify.yml
  -> fast impacted admission (PR head)
  -> native merge queue
  -> full suite + Playwright (merge-group SHA)
  -> one App-authenticated status
  -> canonical merge provenance
```

Credential failure is visible and terminal instead of silently selecting another
identity. Untrusted tests never share a job with callback credentials. A merge
group has one technical completion condition, so the queue and Autopilot observe
the same verdict. The suite writes a structured `switchboard.ci_result.v1`
artifact and stops scheduling new test files after the first known failure.

Branch protection requires only `Switchboard CI / VM gate`. The merge queue remains
ALLGREEN and runs full verification on its generated merge-group SHA before
landing. A green PR head means safe to enqueue, never safe to bypass the queue.
