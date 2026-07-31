# ADR-0026 — Compand benchmark claims require reproducible evidence releases

- **Status:** Accepted (operator decision, 2026-07-31)
- **Date:** 2026-07-31
- **Canonical reference:** `ADR-0026/Compand-evidence`
- **Applies to:** Compand Phase 1, Phase 2 Technique Lab, later certification,
  Value Index movement, and public savings claims
- **Normative specification:** [Compand Benchmark Publication Standard — CES-1](../COMPAND-BENCHMARK-STANDARD.md)
- **Relates to:** [Compand research bundle](../TOKEN-OPTIMIZATION-OVERVIEW.md) ·
  [Tally specification](../TALLY-SPEC.md)

## Context

A token transform can report a large local compression percentage while increasing
the cost or reducing the quality of a complete coding task. Provider prefix caching,
extra turns, retries, recovery calls, output tokens, latency, and quality regressions
can erase or reverse the apparent saving. Stochastic agent trajectories also make a
single run or aggregate percentage a weak product claim.

Compand therefore needs one durable boundary between experimentation, certification,
Value Index movement, and public claims. The detailed benchmark method will evolve;
the product decision that evidence controls promotion and claims must not.

## Decision

Compand treats benchmark publication as a product capability, not a marketing step.
Every certification or savings claim is governed by the current version of CES.

Four rules are architectural:

1. **A claim may not exceed its evidence tier or evidence state.**
2. **The complete coding task is the economic denominator.**
3. **Confirmatory methods are frozen before confirmatory traffic runs.**
4. **Every published aggregate regenerates from immutable run evidence.**

Certification is always scoped to an exact technique version, agent dialect/version,
model/provider snapshot, workload revision, and CES release. A certification is
suspended when a material change invalidates that tuple until recertification passes.

Local compression ratio, eligible-item savings, or projected token reduction may be
reported as diagnostics. They may not impersonate provider-billed whole-task savings
or cost per verified coding outcome.

## Phase boundary

- **Phase 1** proves insertion, safety, accounting, recovery, and one exact technique.
  Its twelve-run `line-rle-v1` pilot may produce at most provisional mechanism
  evidence for the named corpus. It cannot establish product-wide savings,
  production ROI, or market value.
- **Phase 2** owns statistically designed technique certification, grader
  certification, immutable evidence compilation, clean-environment reproduction,
  publication, and the certification registry.
- A product-wide public savings claim requires at least one winning technique to be
  independently reproduced. Narrower verified results remain accurately labeled by
  their release and certified lane.

Only verified outcomes may move the canonical Value Index. Projected, measured,
market-validated, independently reproduced, and IP-supported evidence remain
distinct states and cannot substitute for one another.

## Ownership

- **Compand** owns transform eligibility, execution, recovery, coverage, transform
  receipts, the CES evidence compiler, and certification state.
- **Tally** owns canonical spend and outcome reconciliation. It does not certify
  transform correctness.
- **Switchboard** may provide verified delivery outcomes and coordinate the work.
  Compand remains capable of using a declared standalone task oracle.
- **Transport or routing layers** do not own Compand policy, session history,
  certification, or publication truth.

## Consequences

CES adds deliberate work and provider expense to certification. That cost is
accepted because an irreproducible token-saving claim has little commercial or
technical value.

The detailed arms, statistics, fixtures, gates, evidence schemas, release layout,
retention rules, and reproduction procedure live in the versioned CES
specification. Updating those mechanics does not require rewriting this ADR unless
the four architectural rules, phase boundary, or ownership model changes.

Published evidence is immutable. Corrections create a new release and changelog
entry rather than silently replacing prior results.

## Alternatives rejected

### Publish aggregate token reduction only

Rejected because it hides eligibility, cache economics, extra turns, quality,
failures, and whole-task cost.

### Treat Phase 1 as product validation

Rejected because the pilot is designed to prove the gateway mechanism and one
technique, not population-level generalization.

### Keep all methodology in the ADR

Rejected because experimental mechanics and schemas must evolve independently of
the durable product and authority decision.
