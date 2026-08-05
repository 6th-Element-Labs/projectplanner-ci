# Compand Phase 2 Technique Lab — frozen CES-1 benchmark card

- Status: **frozen contract; confirmatory traffic blocked**
- Version: `compand-phase2-technique-lab/1.0.0`
- Authority: [ADR-0026](../../decisions/0026-compand-benchmark-publication.md) and
  [CES-1](../../COMPAND-BENCHMARK-STANDARD.md)
- Machine contract: [`benchmark.yaml`](benchmark.yaml)
- Technique inventory: [`technique-catalog.json`](technique-catalog.json)
- Corpus/system identity: [`corpus-manifest.json`](corpus-manifest.json) and
  [`system-card.json`](system-card.json)
- Public result shape: [`public-scorecard.schema.json`](public-scorecard.schema.json)

## Purpose and claim boundary

The lab determines which researched context-efficiency techniques work in a named cloud
gateway lane, on a frozen workload and system tuple, without worsening verified coding
outcomes. The complete coding task—not a transformed span—is the economic denominator.
Local compression and projected savings remain diagnostics.

This freeze authorizes implementation, corpus construction, shadow replay, grading, and
paired development tests. It does not authorize production promotion or confirmatory
traffic yet. QA-56 materializes and hashes the sanitized development and golden partitions,
while the hidden-holdout plan intentionally exposes no payload or oracle to plugin authors.
Confirmatory traffic remains blocked until the independent QA-58 custodian attests the
encrypted holdout payload commitments, and until the final provider, model and price
snapshot, Phase 1-informed variance input, calculated sample size, and six live Switchboard
KPI row bindings are frozen in a versioned successor manifest.

CodexZero is prior art and an adversarial-fixture source only. Its reported correctness,
savings, and cumulative effects are not Compand evidence. Host and cloud effects are never
assumed additive.

## Experimental arms

| Arm | Contract |
|---|---|
| `B0` | Byte-unchanged authorized baseline. No transform or shadow marker reaches the provider. |
| `S1` | Detect and estimate only; provider-visible bytes remain identical to `B0`. |
| `E1` | Exactly one cloud-gateway-enforceable technique version. |
| `C1` | Only techniques with frozen passing `E1` scorecards; report standalone, incremental, and interaction effects. |

Randomization uses deterministic hash-seeded blocked pairs and an `ABBA` order schedule
over workload, task, agent/model snapshot, repetition, and cache condition. Evaluators see
opaque run IDs, not arm labels.

## Primary questions

The confirmatory economic estimand is the paired change in provider-confirmed net cost per
verified completed task, including measured Compand overhead. The quality gate is the paired
change in verified task success, with an absolute non-inferiority margin of `-0.05`. Safety
requires zero protocol, authorization, isolation, or exact-recovery failures.

The twelve-run Phase 1 pilot is mechanism evidence. The repository does not contain a usable
task-level paired variance estimate, so this contract does not invent one. An exploratory
variance pilot must provide a hashed paired standard deviation. Final sample size is then
frozen before confirmatory traffic as:

```text
h = max($0.05, 10% of mean B0 net cost per verified task)
N = ceil((1.96 × paired-cost SD / h)²), bounded to [30, 200] task pairs
```

Each task/arm has at least three independent repetitions. If the calculation exceeds 200,
the result is `inconclusive`; the precision target is not relaxed. Confirmatory economic
comparisons use Holm–Bonferroni family-wise alpha `0.05`; simultaneous 95% quality intervals
gate non-inferiority. Exploratory endpoints use Benjamini–Hochberg `q=0.10`. `C1` remains
exploratory until its own combination family is preregistered.

## Cache, retry, exclusion, and missing-data rules

Every pair exercises frozen cold, warm-identical-prefix, warm-changed-suffix, and expired
cache conditions. Provider usage/charge is economic truth. Missing cache fields remain
missing, never zero; local estimates cannot replace provider fields.

Infrastructure-invalid attempts remain in evidence and may retry once only when provider
dispatch/usage did not occur or the fixture could not be loaded. Task failures, timeouts,
agent errors, recovery calls, protocol errors, and expensive tails are outcomes—not retry or
exclusion reasons. Post-arm exclusions are forbidden. Missing provider usage blocks the
economic grade; a missing task outcome fails the primary completion denominator and cannot
move Value Index KPIs.

## Hard gates and grades

Any hard-gate failure yields `F`, irrespective of averages: correctness, single-technique
attribution, tenant/principal/session isolation, deterministic reproducibility, protocol
safety, exact recovery where claimed, fail-open optimization without policy bypass,
positive whole-task economics, quality non-inferiority, and clean-environment regeneration.

Passing techniques receive four separate 0–100 grades:

- Technical: correctness 35, reproducibility 20, failure transparency 20,
  latency/reliability 15, simplicity 10.
- User value: net cost per verified task 40, natural eligible spend coverage 25,
  outcome non-inferiority 25, latency/friction 10.
- Company value: defensible evidence 30, margin potential 25, cross-lane applicability 20,
  operations burden 15, IP/design-around evidence 10.
- Asset value: reusable certified profile 30, failure/rollback corpus 25, cross-lane
  calibration 20, evidence compiler 15, IP/prior-art record 10.

Bands are `A >= 85`, `B 70–<85`, `C 50–<70`, and `D < 50`. Company and asset value cannot
override safety or negative user value. Technique value and asset value never combine into
one savings claim.

## Six frozen KPI output IDs

Scorecards emit exactly these stable contract IDs:

1. `compand.p2.net_cost_per_verified_task_usd`
2. `compand.p2.natural_eligible_spend_coverage_ratio`
3. `compand.p2.task_outcome_noninferiority_rate`
4. `compand.p2.gateway_added_latency_p95_ms`
5. `compand.p2.reliable_request_rate`
6. `compand.p2.exact_recovery_success_rate`

The live deliverable currently has no KPI rows linked. `board_kpi_id` therefore remains
`null`, `pending_live_kpi_registration`, and blocks Value Index movement. This is an explicit
control-plane gap, not permission to invent a remote identifier. TALLY-12 must register and
bind the live rows before any verified outcome is linked.

## Evidence and publication

Raw, normalized, and published layers are immutable. Every public aggregate must trace to
task/run/event IDs and regenerate in a locked clean environment. Corrections create a new
release and changelog entry. Evidence tier (`C1`–`C5`), evidence state (`exploratory`,
`provisional`, `verified`, `independently_reproduced`, `suspended`), and value state
(`projected`, `measured`, `verified`, `market_validated`, `ip_supported`) remain distinct.
Only verified outcomes may move the Value Index. A product-wide public savings claim remains
forbidden until at least one winning technique is independently reproduced.

`trace.scorecard_sha256` is not a recursive self-hash. To compute it, remove the
`/trace/scorecard_sha256` member, serialize the remaining scorecard with the RFC 8785 JSON
Canonicalization Scheme, compute SHA-256 over those serialized UTF-8 bytes, and store the
lowercase hexadecimal digest in the removed member. Verification repeats the same exclusion
and canonicalization before comparing the digest.
