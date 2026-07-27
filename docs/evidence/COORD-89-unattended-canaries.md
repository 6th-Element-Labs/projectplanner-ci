# COORD-89 unattended canary evidence

Date: 2026-07-27 UTC

## Scope and controls

Four code-strict tasks were started through `start_task` and left to the production
completion controller. No canary received a manual status transition, lifecycle retry,
custom requeue, direct merge, or administrator merge. GitHub's required
`Switchboard CI / VM gate` remained the test authority and the native merge queue
remained the landing authority.

| Leg | Task / PR | Exact head | Observed result |
|---|---|---|---|
| concurrent A | COORD-91 / #999 | `45ad9e3f32116397e3dffffd46790bd51d18a246` | green; native queue merge `7ada0d5147b139004d580f0f89f0ee164e0a1f2c` at 2026-07-27T16:49:56Z |
| concurrent B | COORD-92 / #996 | `150e15e742260983b153609ff2e4eb86b8955c90` | green; native queue merge `9fe01f463d05c1c415656d958ebd528aa4f3114d` at 2026-07-27T16:49:56Z |
| real test failure | COORD-93 / #997 | failing `aeb6e600…`, repaired `65b155c2c9bdc534ceff90d0f457a86a9decf699` | trusted CI failed the intentionally failing direct test; the controller started a fresh `remediation` generation, which repaired the test and produced a green exact-head gate |
| infrastructure loss | COORD-94 / #998 | `abab22d2173d1e4c197f9b2fa9b0d7aae4408ac7` | implementation runner ended after publication without `complete_claim`; recovery did not route infrastructure loss to remediation; exact-head CI passed |

The two independent scopes were live concurrently and their two PRs landed through
the queue at the same recorded second with distinct canonical squash SHAs. That is
contention evidence without a second landing owner.

## Acceptance defect and repair

The first live pass exposed a real production race. A PR could be observed before
required-CI hydration while its implementation runner still owned Capacity. The thin
normalization law mapped `required_ci_hydration_missing` to `BLOCK`, although the only
valid action while implementation remained live was `WAIT`.

COORD-95 / PR #1000 fixes the law so every non-`MERGED` observation with a live
implementation runner becomes `WAIT` / `attach_and_wait`; `MERGED` provenance
observation remains authoritative. The repair adds a focused regression and passed
the canonical local gate before publication. The initial affected canaries were not
manually advanced or retried.

## CI-repair lane

The repair workflow was invoked for PR #996 at exact head
`150e15e742260983b153609ff2e4eb86b8955c90` with `purpose=ci_repair`. It reused the
ordinary trusted `master:verify.yml` evidence at
`https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/30285264616`,
which had run the full gate successfully. The subsequent eligibility read correctly
failed closed because the native queue had already merged the PR. Final audited
eligibility is therefore exercised on the evidence PR rather than treating a closed
PR as eligible.

## Assertions and measurements

- Manual lifecycle interventions on the four canaries: **0**.
- Infrastructure-to-remediation routes: **0**.
- Test-failure-to-remediation routes: **1**, with a fresh fenced generation.
- Direct or administrator canary merges: **0**.
- Custom requeue operations: **0**.
- Duplicate landing owners observed: **0**.
- Stale-snapshot mutations observed: **0**; the acceptance repair makes live
  implementation ownership an explicit `WAIT`.
- Orphan runner requiring operator cleanup: **0**.

Exact head-green, queue, merge, and deployment-reflection timestamps are retained in
the Switchboard task activity, GitHub check/merge records, and the COORD-89 completion
evidence. Where a provider exposes only second precision, lag is reported at that
precision rather than inferred.
