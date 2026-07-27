# ADR-0022 — One fail-closed CI verdict

- **Status:** Accepted (operator decision, 2026-07-27)
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
2. That verdict is successful only after the exact SHA passes the full
   `scripts/switchboard_ci.sh` suite and Playwright. Playwright remains mandatory
   evidence, including its uploaded receipt; only its duplicate GitHub status is
   removed.
3. The public workflow mints a dedicated GitHub App installation token. App ID and
   private key are mandatory. There is no PAT fallback.
4. A merge-group webhook dispatches the temporary exact SHA through the same
   technical CI route. Advisory claim and merge-authorization statuses remain
   PR-scoped and are not projected onto merge-group SHAs.
5. `external_ci_mirror` remains the single primary mirror engine. The older pull
   route remains a short-lived rollback bridge and is deleted separately after
   the push route proves stable under real queue traffic.
6. The existing context name is retained to avoid a simultaneous rename migration.
   Its description and evidence define the broader full-suite-plus-Playwright
   meaning.

## Consequences

The required path is:

```text
exact canonical SHA
  -> external_ci_mirror
  -> public verify.yml
  -> full suite + Playwright
  -> one App-authenticated status
  -> native merge queue
```

Credential failure is visible and terminal instead of silently selecting another
identity. A merge group has one technical completion condition, so the queue and
autopilot observe the same verdict. UI failures remain blocking and retain their
receipt; they no longer need a second status lifecycle.

Branch protection must require only `Switchboard CI / VM gate`. The merge queue
remains ALLGREEN and tests its generated merge-group SHA before landing. Queue
timeout reduction is a later operational change based on observed latency, not
part of this decision.
