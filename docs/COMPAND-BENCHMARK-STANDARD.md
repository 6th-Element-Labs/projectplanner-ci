# Compand Benchmark Publication Standard — CES-1

- **Status:** Normative version 1; governed by
  [ADR-0026](decisions/0026-compand-benchmark-publication.md)
- **Date:** 2026-07-31
- **Canonical reference:** `CES-1`
- **Applies to:** Compand Phase 1 cloud pilot, Phase 2 Technique Lab, and later
  Compand certification or savings claims
- **Relates to:** [Compand research bundle](TOKEN-OPTIMIZATION-OVERVIEW.md) ·
  [Tally specification](TALLY-SPEC.md)

## Context

Token-saving products can produce a large local compression percentage without
materially changing a coding task's total cost. Provider prefix caching, extra
turns, retries, recovery calls, output tokens, and quality regressions can erase
or reverse the apparent saving. Coding-agent trajectories are also stochastic:
two runs of the same task and configuration can have materially different token
and cost totals.

The Compand research bundle already requires paired outcomes, provider usage,
cache-aware admission, immutable evidence, mutation testing, blinded evaluation,
and Tally reconciliation. Those requirements were spread across research
documents and board tasks, while “publish scorecards” did not define what a
credible publication contains or which claims the evidence permits.

This is the versioned product standard that implements ADR-0026 because
Compand's public claims, promotion state machine, Value Index, customer trust,
and defensibility all depend on one evidence contract. Benchmark publication is
part of the product, not a marketing step after experimentation.

## Normative standard

Compand adopts the **Compand Benchmark Publication and Evidence Standard v1
(`CES-1`)**.

A Compand technique is not `certified`, and a savings claim is not `verified`,
unless the claim, frozen protocol, immutable run evidence, uncertainty,
limitations, and reproducibility package ship as one versioned evidence release.

CES-1 has four binding rules:

1. **The claim may not exceed its evidence tier.**
2. **The complete coding task is the economic denominator.**
3. **Confirmatory methods are frozen before confirmatory traffic runs.**
4. **Every published aggregate must regenerate from immutable run evidence.**

### 1. Claim and evidence ladder

Each higher tier includes all lower-tier requirements:

| Tier | Required evidence | Maximum permitted claim |
|---|---|---|
| C1 — Mechanical validity | Transform oracle, golden and negative fixtures, mutation tests, exact recovery where claimed | The technique is implemented correctly for the declared inputs |
| C2 — Reproducible benchmark | Frozen paired protocol, pinned systems, independent repetitions, public or reviewable artifacts, and uncertainty | The technique changed measured outcomes on the named CES-1 release |
| C3 — Certified robustness | Hidden holdouts, workload strata, agent/model profiles, and severe-tail and failure analysis | The technique generalizes across the explicitly certified lanes |
| C4 — Production economics | Provider usage or billing truth, cache effects, Compand overhead, retries, recovery, and complete-task outcomes | The technique reduced observed net provider cost in the measured deployment |
| C5 — Verified value | Completed-work outcomes, Tally reconciliation, design-partner evidence, and independent reproduction where claimed | The technique improved cost per verified coding outcome for the measured population |

The evidence state is separate from the claim tier:

| State | Meaning |
|---|---|
| `exploratory` | Development, shadow, or diagnostic evidence; methods may still change |
| `provisional` | Frozen internal experiment completed, but the full CES-1 release or clean-machine reproduction is incomplete |
| `verified` | All declared CES-1 gates pass and the immutable release regenerates on a clean environment |
| `independently_reproduced` | A named party outside the result-producing team obtains the main result and publishes a reproduction record |
| `suspended` | A model, client, provider, tokenizer, protocol, or material benchmark change invalidated the prior certification |

`Certified` means `verified` for an exact tuple of technique version, agent
dialect/version, model/provider snapshot, workload revision, and CES-1 release.
It is never a timeless product-wide adjective.

### 2. Experimental contract

Every technique follows the same arm vocabulary:

```text
B0  unchanged baseline
S1  shadow detection and projected economics
E1  one technique enforced alone
C1  combinations of techniques that individually passed
```

The protocol must pin and hash, as applicable:

- hypotheses, primary estimands, confirmatory and exploratory endpoints;
- task IDs, repository commits, corpus revision, licenses, workload strata,
  contamination limits, and hidden-holdout policy;
- agent and harness build, model/provider snapshot, reasoning effort, tokenizer,
  tool surface, timeout, network policy, and evaluator version;
- arm ordering or randomization, cache-conditioning schedule, repetition policy,
  retry policy, stop rules, exclusions, missing-data treatment, and pricing date;
- quality non-inferiority margin, safety gates, and promotion/rollback thresholds.

Sample size is not a fixed marketing number. Phase 1 supplies the observed
trajectory variance used by a preregistered power or confidence-interval
precision analysis for Phase 2. Results report task-level paired effects,
numerators, denominators, independent repetition counts, 95% intervals, central
tendency, and material tails. Confirmatory analysis accounts for multiple
comparisons when one corpus promotes several techniques.

Infrastructure-invalid attempts remain in the evidence with retry lineage.
Normal task failures, timeouts, agent errors, recovery calls, and expensive tails
remain experimental outcomes and may not be silently retried or excluded.

### 3. Measurement contract

Published scorecards keep these measures separate:

- eligible events and tokens versus all routed events and tokens;
- provider input, cached input, output, and reasoning usage when available;
- dated provider price schedule and observed provider charge when available;
- Compand counting, compute, storage, gateway, and recovery overhead;
- end-to-end task latency and request-path latency, including p50 and p95;
- retries, fail-open events, recovery faults, bypasses, timeouts, and failures;
- task pass rate or harness score and the paired difference from `B0`;
- exact-reconstruction, protocol-integrity, tenant/session-isolation, and
  artifact-authorization results;
- net provider cost per verified completed task;
- Tally and Value Index reconciliation without double-counting host and cloud
  effects or overlapping techniques.

Local compression ratio, would-have-saved tokens, or eligible-item savings is
diagnostic. None may be presented as whole-task or billing savings.

Provider-reported usage is the economic source of truth when exposed by the
provider. Local tokenization remains a decision input and independently checked
estimate. Any unexplained disagreement is visible and blocks economic
certification.

### 4. Hard gates

These failures block certification regardless of average savings:

- cross-tenant, cross-session, or unauthorized artifact access;
- protocol, signature, ordering, continuation, retry, or streaming corruption;
- failure of a claimed exact transform to reconstruct the original bytes and hash;
- unexplained traffic bypass or incomplete coverage presented as full coverage;
- quality outside the preregistered non-inferiority margin;
- non-positive net economics for the claimed population after cache effects and
  Compand overhead;
- inability to regenerate a required table, figure, or scorecard from the
  released evidence.

Unsupported is a valid result. Host-only techniques remain `unsupported`,
`emulated`, or `shadow_only` in the cloud lab until a later host experiment
proves them. CodexZero and other systems provide prior art and adversarial cases;
their results are not Compand evidence and are not assumed cumulative.

## Phase requirements

### Phase 1 — cloud pilot

Phase 1 remains a mechanism and engineering pilot for `line-rle-v1`; it is not a
statistically powered product-validation study.

Before the paid paired run, Phase 1 must freeze a preliminary CES-1 manifest for
the existing `B0`/`E1` run matrix. Its release must include:

- exact task, agent, model, provider, tokenizer, evaluator, configuration, and
  price identities;
- immutable per-run manifests and hashes;
- provider usage, cached-token fields when available, complete-task cost,
  quality, latency, retry, recovery, coverage, and safety results;
- all failures, invalid attempts, exclusions, and retry lineage;
- a machine-readable scorecard and one command that regenerates the report from
  sanitized evidence.

The twelve-run result may be labeled `provisional C2 mechanism evidence` if its
gates pass. It may claim only that insertion and `line-rle-v1` produced the
measured effect on the named pilot corpus. It may not claim general coding-agent
savings, production economics, market value, or broad non-inferiority.

Phase 1 also records variance, failure distributions, and instrumentation gaps
needed to power and freeze Phase 2. A result that cannot produce its CES-1
preliminary bundle does not authorize expansion.

### Phase 2 — Technique Lab

Phase 2 makes CES-1 an executable publication workstream:

1. freeze the benchmark card, corpus manifest, hypotheses, estimands, grade
   schema, Value Index mapping, and statistical analysis plan;
2. implement immutable raw, normalized, and published evidence layers;
3. run `B0`, `S1`, and each `E1` independently before any `C1`;
4. certify the grader with oracles, golden/negative fixtures, mutation testing,
   blinded evaluation, hidden holdouts, and provider-usage reconciliation;
5. generate every public table and figure from the released evidence in a locked
   clean environment;
6. publish signed checksums, version, changelog, limitations, correction policy,
   and a persistent archive identifier;
7. seek adversarial external review and publish reproduction or discrepancy
   reports;
8. maintain a public certification registry by technique × agent × model ×
   workload × CES-1 version, including recertification triggers and suspension.

A Phase 2 technique may be promoted only when its individual CES-1 scorecard
passes the hard gates and reports attributable technical, user, company, and
asset value separately. Combination results report both incremental and
interaction effects. Only verified outcomes may move the canonical Value Index.

At least one winning technique must reach `independently_reproduced` before
Compand makes a product-wide public savings claim. Other techniques may be
accurately labeled `verified` without pretending independent reproduction.

## Evidence release

The canonical release layout is:

```text
benchmark.yaml
BENCHMARK-CARD.md
LIMITATIONS.md
SECURITY.md
corpus-manifest.json
system-card.json
runs/<run_id>/manifest.json
runs/<run_id>/receipts.jsonl
runs/<run_id>/usage.json
runs/<run_id>/outcome.json
results/task-level.csv
results/scorecards.json
analysis/
reproduce
CHECKSUMS
CHANGELOG.md
reproduction/
```

Private source content, credentials, tenant identity, dialect fingerprints,
cache-simulator calibration, proprietary scoring weights, and customer
distributions remain private. The public or reviewer package must still include
sanitized fixtures, schemas, receipts, hashes, analysis code, and at least one
open exact reference technique sufficient to reproduce the published claim.

Corrections create a new immutable release and changelog entry. Published
evidence is never silently replaced.

## Product boundaries

- **Compand** owns transformation eligibility, execution, recovery, coverage,
  transform receipts, and the CES-1 evidence compiler.
- **Tally** owns canonical spend and outcome reconciliation. It does not certify
  transform correctness.
- **Switchboard** supplies optional verified delivery outcomes and coordinates
  the work. Compand remains capable of using a declared standalone task oracle.
- **LiteLLM** may provide transport or routing. It does not own Compand policy,
  session history, certification, or publication truth.
- A public mirror or artifact repository proves publication only. It never owns
  Switchboard code Done or Compand deployment truth.

## Consequences

CES-1 adds deliberate work and provider expense to each certification. That cost
is accepted because the alternative is a cheaper but commercially weak claim
that cannot survive scrutiny.

The evidence compiler, certification registry, dialect/model history, failure
corpus, and cost-per-verified-outcome dataset become product assets. The open
method can establish category trust while proprietary calibration and customer
evidence remain defensible.

Phase 1 can still move quickly, but its claim stays narrow. Phase 2 can publish
stronger results, but only after measuring variance, freezing the protocol, and
passing reproducibility gates.

## Rejected alternatives

### Publish only aggregate token reduction

Rejected because it hides eligibility, cache economics, extra turns, quality,
and whole-task cost.

### Treat the twelve-run Phase 1 pilot as product validation

Rejected because the pilot is designed to prove insertion, safety, accounting,
and one technique, not population-level generalization.

### Keep the benchmark private

Rejected as the default because vendor-authored claims without reviewable
artifacts do not create category trust. Sensitive internals may remain private,
but the claimed result must remain reproducible from a sanitized package.

### Require external reproduction before any internal promotion

Rejected because it would prevent operational learning. Internal promotion may
use `verified` CES-1 evidence; externally reproduced status remains distinct and
is required before a product-wide public savings claim.

### Let each technique choose its own favorable benchmark

Rejected because result-dependent corpora and metrics destroy comparability and
invite denominator selection. Technique-specific invariants are allowed, but the
shared task, economic, quality, and publication contracts remain fixed.

## Reference standards and evidence

CES-1 is informed by:

- [ACM artifact evaluation and reproducibility criteria](https://sigsim.acm.org/conf/pads/2024/blog/artifact-evaluation/):
  available, functional, reusable, and reproduced results are distinct claims.
- [SWE-bench Verified](https://www.swebench.com/verified.html): human-validated
  tasks, pinned harness/configuration versions, and explicit incomparability
  across materially different releases.
- [MLPerf Endpoints](https://mlcommons.org/benchmarks/endpoints/): system
  descriptions, full run reports, peer review, audit, and verified versus
  unverified result states.
- [NeurIPS 2026 Evaluations & Datasets guidance](https://dev.neurips.cc/Conferences/2026/EvaluationsDatasetsFAQ):
  executable, documented code and durable benchmark artifacts.
- [OpenAI API usage fields](https://platform.openai.com/docs/api-reference/usage/audio_transcriptions_object)
  and [prompt caching](https://openai.com/index/api-prompt-caching/): cached
  input is measured separately and must be included in economic claims.
- [Tura benchmark methodology](https://github.com/Tura-AI/benchmark/blob/main/doc/benchmark-methodology.md):
  pinned run identity, raw/normalized/published evidence layers, explicit invalid
  states, and complete-task reporting for agentic work.
- Current coding-agent forum requests for artifacts, stock comparisons,
  long-horizon tasks, cache-aware economics, repetitions, and independent
  verification:
  [benchmark discussion](https://www.reddit.com/r/codex/comments/1v8zwjl/i_benchmarked_every_single_usage_saving_tool_out/)
  and [CodexZero discussion](https://www.reddit.com/r/codex/comments/1v5dct6/burned_my_full_200_plan_3_rests_while_asking/).

External sources were reviewed on 2026-07-31. They inform the methodology; the
binding architectural decision is ADR-0026 and the executable requirements are
this specification plus its board acceptance gates.
