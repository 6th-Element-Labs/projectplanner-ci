# Token Optimization Gateway — product and evidence overview

Status: reviewed series overview and reading path
Scope: standalone agentic-coding infrastructure layer, optionally amplified by Switchboard
Authority: product research and engineering design; not an accepted Switchboard ADR or a
claim that E1–E5 have passed

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [The product](#2-the-product)
3. [Why it can stand alone](#3-why-it-can-stand-alone)
4. [Why Switchboard makes it stronger](#4-why-switchboard-makes-it-stronger)
5. [How the gateway gets into the traffic path](#5-how-the-gateway-gets-into-the-traffic-path)
6. [The technical contract](#6-the-technical-contract)
7. [Transform ladder](#7-transform-ladder)
8. [Evidence and safety](#8-evidence-and-safety)
9. [Product wedge and defensibility](#9-product-wedge-and-defensibility)
10. [Business-model interpretation](#10-business-model-interpretation)
11. [Build sequence and go/no-go gates](#11-build-sequence-and-gono-go-gates)
12. [Known boundaries](#12-known-boundaries)
13. [Document map](#13-document-map)

## 1. Executive summary

The product is a configured gateway and optional local harness for coding-agent LLM
traffic. It measures context waste, cache misses, retries, and coverage; applies only
certified transforms; and proves savings against task outcomes. It is not primarily a
generic proxy, prompt compressor, or model router.

The initial product is a no-mutation **Context Doctor**:

- show which agent/model/auth traffic is captured, bypassed, partial, or unknown;
- explain where input, output, cache, retry, and idle-wake spend originates;
- calculate what certified transforms would have saved after cache and optimizer costs;
- emit reproducible coverage, request, usage, and decision receipts; and
- join savings to customer-provided task outcomes.

Enforcement follows only after measured opportunity, protocol certification, and
non-inferiority evidence. Optimization failures send the original authorized payload;
security, tenancy, retention, credential, DLP, egress, and budget failures reject.

## 2. The product

The product has three cooperating surfaces:

1. **Gateway data path.** OpenAI Responses and Anthropic Messages adapters preserve
   streaming, tools, signed/encrypted blocks, errors, usage, and provider caching.
2. **Optional harness path.** Agent or launcher integration enables turn elimination,
   deferred schemas, cooperative context epochs, artifact retrieval, and lanes a
   transparent gateway cannot safely change.
3. **Evidence plane.** Versioned coverage and transform receipts connect token and cache
   economics to task success, retries, latency, and regressions.

The durable product is the certification and evidence loop around the transforms:
agent-dialect detection, versioned codecs, cache-economic vetoes, outcome evaluation,
promotion, automatic demotion, and customer-visible proof.

## 3. Why it can stand alone

Standalone customers do not need Switchboard. They can supply:

- agent and provider configuration;
- task or session identifiers where available;
- API, CI, benchmark, or workflow outcomes;
- retention and credential policies; and
- an optional local harness or artifact-retrieval integration.

They receive coverage attestation, context linting, safe transform profiles, usage and
savings evidence, policy controls, and regression monitoring. The standalone success
metric is **net cost per accepted customer outcome**, with clearly labeled proxies when
the customer cannot provide a definitive outcome.

## 4. Why Switchboard makes it stronger

Switchboard supplies unusually strong ground truth:

- project, task, execution, and model-routing identity;
- runtime adapters and managed launch configuration;
- CI, review, remediation, merge-group, and canonical merge evidence; and
- Tally cost attribution to a verified outcome.

That makes the combination more than gateway observability: it can measure dollars per
verified coding outcome. Gateway observations remain evidence only; they cannot claim
capacity, acknowledge communication, complete work, authorize a merge, or prove Done.

## 5. How the gateway gets into the traffic path

Coverage is certified per `(client version, auth lane, feature profile, adapter)`:

| Client lane | Current position | Insertion mechanism |
|---|---|---|
| Claude Code API/enterprise | Addressable; certify each release/profile | `ANTHROPIC_BASE_URL` plus gateway credential |
| Claude Code claude.ai OAuth | Technically documented; E1/security/terms certification required | `ANTHROPIC_BASE_URL`, preserved OAuth capability, no gateway credential termination |
| Codex API key | Addressable; local Responses canary observed | User-level `openai_base_url` or explicit model provider |
| Codex ChatGPT OAuth | Unknown until separately recertified | Do not infer from the API-key path |
| Cursor custom-provider/BYOK | Partial; E5 pending | Authenticated IDE mock-provider canary |
| Cursor-served, Tab, Composer, closed features | Unsupported unless vendor exposes a seam | Harness/MCP can add adjacent value but cannot claim traffic coverage |

A configured endpoint is not an adversarial TLS interceptor. Nevertheless,
configuration alone is not proof. A coverage receipt requires protocol fixtures,
direct-vs-gateway task parity, process-level egress observation, and explicit exclusions.

## 6. The technical contract

The state model uses two immutable, paired ledgers:

- **Client view:** canonical raw history received from the agent.
- **Provider view:** exact transformed representation already shown to the model.

Each continuation is matched against the raw client prefix. The frozen provider prefix is
replayed byte-for-byte, and only the newly appended raw suffix is eligible for transforms.
Declared session identity is preferred; scoped hash-chain matching is a conservative
fallback.

Every candidate must pass:

1. dialect and span eligibility;
2. retention and artifact authorization;
3. provider-view visibility proof for references;
4. the best certified model-specific count;
5. cache-adjusted dollar economics;
6. a latency deadline; and
7. the transform profile's promotion state.

## 7. Transform ladder

| Tier | Examples | Default posture |
|---|---|---|
| Observe | Coverage, waste census, cache/retry accounting | On; no provider mutation |
| Certified hygiene | ANSI/pager removal, exact JSON minification, bounded line-ending normalization | On only for certified fields |
| Exact representation | RLE, visible exact references, within-turn overlap dedup, structured-data codecs | Canary after outcome evidence |
| Recoverable projection | Command-aware codecs, successful-check/stack projections, diff re-reads, vision budgets | Opt-in with artifact path |
| Cooperative context | Context epochs, artifact expansion, schema deferral, turn elimination | Requires certified agent/harness/provider support |
| Behavioral | Lean prompts, summaries, output shaping, learned compression | Opt-in, model-specific evaluation |
| Routing | Model tier plus context profile | Separate joined decision, not product identity |

Frozen provider history is never silently rewritten. Retroactive paging requires a
cooperative context epoch, provider-native compaction, or an explicit cache reset.

## 8. Evidence and safety

Two receipts form the minimum evidence surface:

- `gateway_coverage_receipt.v1`: exact client/auth/adapter/features, endpoints,
  egress-observation method and window, exclusions, source version, and evidence hash.
- `transform_decision`: raw and candidate counts, counting method, input/output/cache
  economics, gross and net savings, latency, retries, artifact expansion faults,
  codec/profile versions, and joined outcome.

Promotion is per `(transform, dialect, model)`:

`candidate → shadow → canary → default-on → suspended`

Model or agent releases trigger recertification. Unknown dialects and ambiguous
sessions use observe/hygiene only. A token reduction is not a product success unless
provider-reported economics improve without violating the preregistered task-outcome
margin.

## 9. Product wedge and defensibility

The wedge is a local or managed shadow-mode report: install, route certified traffic,
and receive evidence without request mutation. The report identifies the largest
economic opportunities and which traffic remains outside coverage.

Defensibility compounds across:

- coding-agent-specific command and structured-data codecs;
- agent/version dialect certification;
- cache-aware and provider-aware economics;
- cross-provider outcome evidence;
- fleet-tested promotion and rollback history; and
- savings claims tied to accepted work rather than token counts alone.

The first must-have experience is not “20x compression.” It is: **show me every dollar
my coding agents spend, what was useful, what was avoidable, what you can safely remove,
and the proof that you did not harm delivery.**

## 10. Business-model interpretation

The business workbook is an editable scenario model, not a market fact set. Its TAM,
addressability, 18% savings rate, 20% take rate, attach rates, margins, and penetration
are assumptions to stress and replace with E1–E5 and customer data.

Potential monetization:

- free or low-cost Context Doctor;
- individual Pro subscription for local/harness optimization;
- enterprise platform fee for fleet policy, evidence, SSO, SLA, and deployment options;
- verified-savings share only where baseline and outcome attribution are auditable.

Externally, lead with measured customer results and ranges. Do not use the 18.6x
consumption claim, modeled TAM, competitive absence, or scenario ARR as established fact.

## 11. Build sequence and go/no-go gates

1. **P-1 — Compatibility truth:** implement passthrough adapters and E1 coverage receipts.
2. **P0 — Shadow Context Doctor:** run E2 redundancy/economics census with no mutation.
3. **P1 — Exact and typed codecs:** canary hygiene, exact references, structured data,
   command outputs, and within-turn overlap.
4. **P2 — Harness integrations:** schema deferral, turn elimination, richer outcomes.
5. **P3 — Recoverable/cooperative context:** artifacts, context epochs, diff re-reads,
   vision, and projections.
6. **P4 — Productization:** routing join, enterprise controls, savings-share pilot.

Stop or demote a lane when:

- E1 cannot establish full or explicitly bounded partial coverage;
- E2 finds no material net opportunity after cache and optimizer costs;
- E3 cannot avoid false session merges or cross-principal exposure; or
- E4 cannot establish task-outcome non-inferiority.

## 12. Known boundaries

- Cursor coverage is incomplete until E5.
- Codex subscription OAuth is unknown until recertified.
- OAuth pass-through requires stricter credential, terms, and security review.
- Transparent gateways cannot remove silent process waits or retroactively page frozen
  context.
- Artifact-backed transforms are unavailable without a permitted recovery path.
- Signed/encrypted reasoning and provider-opaque state remain byte-exact.
- Provider-native optimization may shrink the residual opportunity.
- Market size, buyer urgency, savings rates, and pricing remain hypotheses until measured.

## 13. Document map

Read this overview first, then:

1. [`TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md`](TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md) —
   technique lineage, product boundary, risks, and phased scope.
2. [`TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md`](TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md) —
   dated evidence, competitive hypotheses, threats, and positioning.
3. [`TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md`](TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md) —
   dual-ledger architecture, transform contracts, codecs, evidence, and trust.
4. [`TOKEN-OPTIMIZER-FEASIBILITY-DEEP-DIVE.md`](TOKEN-OPTIMIZER-FEASIBILITY-DEEP-DIVE.md) —
   insertion paths, observed canaries, coverage receipts, and E1–E5.
5. [`WHY-NOT-FORK-ROUTELLM.md`](WHY-NOT-FORK-ROUTELLM.md) —
   build-versus-fork decision for the routing component.
6. [`TOKEN-OPTIMIZATION-BUSINESS-MODEL.md`](TOKEN-OPTIMIZATION-BUSINESS-MODEL.md) and
   `TOKEN-OPTIMIZATION-BUSINESS-MODEL.xlsx` — assumption-driven market and revenue
   scenarios.
