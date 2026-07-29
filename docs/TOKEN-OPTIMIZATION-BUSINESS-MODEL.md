# Token Optimization Cloud — business model and market potential

Status: assumption-driven operating model, not an investment-grade forecast
Companion spreadsheet: `TOKEN-OPTIMIZATION-BUSINESS-MODEL.xlsx`
(same directory; edit the blue Assumptions cells to stress the scenario)
Series: techniques → market analysis → tech design → feasibility → RouteLLM note → **this**

**One-line thesis:** Under the model's explicit adoption, spend, addressability,
savings, and pricing assumptions, coding-agent spend reaches ~$7B in 2026 and
~$38B by 2030; an outcome-verified optimizer could create a serviceable revenue
pool above $1.8B and a base scenario of ~$56M ARR. These are scenario outputs to
falsify with E1–E5 evidence, not established market facts or a forecast.

---

## 1. Why this market, why now (the demand evidence)

Four inputs and hypotheses motivate the model. Evidence labels follow the market
analysis; a citation on the Comps tab does not convert a secondary estimate into a
primary fact:

1. **Spend-growth signal, secondary.** Several sources repeat an 18.6x
   per-developer consumption increase, but the underlying cohort and denominator
   have not been located. It is motivation for E2, not an externally usable fact.
2. **Budget-pressure signal.** Public reports describe coding-agent budget pressure
   at large companies. GPT-5.5 also launched at higher API prices than GPT-5.4;
   that supports measuring cost per outcome, not claiming an unchanged model's
   price doubled.
3. **Lever-existence evidence.** CodexZero reports ~15% fewer tokens at the same
   observed benchmark score for one agent/run family. ProjectDiscovery reports a
   7%→84% cache-hit improvement and 59–70% spend reduction on one production
   workload. Neither establishes our 18% blended savings assumption.
4. **Positioning hypothesis.** Existing gateways, routers, providers, and agent
   vendors have different incentives and product boundaries. Their absence from
   outcome-verified coding optimization is a claim to revalidate, not a structural
   certainty.

## 2. Market sizing (bottoms-up; Market tab)

Built from editable developer, adoption, metered-share, and spend assumptions rather
than a claim that the resulting TAM has already been observed:

| Driver ($ pools in $M/yr) | 2026 | 2030 |
|---|---|---|
| Professional developers worldwide | 32M | 40M |
| Using agentic coding tools | 30% → 9.6M | 70% → 28M |
| API-metered (enterprise) share | 25% → 2.4M devs | 45% → 12.6M devs |
| Avg API spend per metered dev | $150/mo | $210/mo |
| **TAM: metered coding-agent API spend** | **$4,320M** | **$31,752M** |
| + Subscription-lane spend ($35/mo blended) | $3,024M | $6,468M |
| **Total coding-agent spend TAM** | **$7,344M** | **$38,220M** |

The external enterprise-LLM forecasts cited in the workbook are only an envelope
check. They do not prove that coding agents are the dominant workload or that the
modeled $32B metered pool will materialize.

**SAM — our monetizable revenue pool** (addressable × savings × take, plus the
two subscription streams):

| Pool ($M/yr) | 2026 | 2030 |
|---|---|---|
| Addressable metered spend (65% of lanes reachable) | $2,808 | $20,639 |
| Verified savings created for customers (18%) | $505 | $3,715 |
| Savings-share revenue pool (20% take) | $101 | $743 |
| Pro subscriptions ($15/mo × 8% attach ceiling) | $104 | $222 |
| Enterprise platform fees ($10/dev/mo × 60% attach) | $173 | $907 |
| **Total SAM** | **$378** | **$1,872** |

The strategic point inside those numbers: by 2030 the model has us
*delivering ~$3.7B/yr of verified savings* to customers to earn our share —
the customer keeps ~80% of every dollar saved. That asymmetry is the sales
motion (see the FinOps precedent: vendors charging 10–20% of realized savings
scaled to $500M–$4.6B exits).

## 3. Revenue scenarios (SOM; Revenue tab)

Penetration of the SAM pools, ramping over five years:

| Scenario (ARR, $M) | 2026 | 2027 | 2028 | 2029 | 2030 | 2030 penetration |
|---|---|---|---|---|---|---|
| Conservative | 0.4 | 1.6 | 5.1 | 10.7 | 18.7 | 1.0% |
| **Base** | **1.1** | **5.2** | **15.2** | **31.3** | **56.2** | **3.0%** |
| Aggressive | 3.0 | 13.0 | 40.6 | 85.2 | 149.8 | 8.0% |

Base-case revenue mix in 2030: ~40% savings-share, ~12% Pro subscriptions,
~48% enterprise platform fees. The platform-fee share growing over time is
the deliberate design: savings-share opens the door (aligned, easy yes);
platform fees (SLA, SSO, dashboards, evidence exports) make revenue
predictable as the relationship matures — the two-track pricing FinOps
vendors converged on.

## 4. Unit economics (UnitEcon tab)

- **Gross margin ~85%** (COGS = proxy compute, artifact storage, and — the
  biggest slice — eval/shadow compute for the evidence flywheel).
- **Illustrative enterprise customer**: 500 metered devs × $175/mo →
  $1.05M/yr coding-agent spend; we deliver ~$123k verified savings; ACV
  ≈ **$60.6k** ($24.6k savings-share + $36k platform). The customer retains
  ~$98.3k after the savings share and ~$62.3k in net dollar savings after both
  fees. Platform value such as SLA, SSO, policy, and evidence is additional,
  not counted as cash savings.
  Base-2030 revenue implies ~**930 enterprise-equivalent customers** —
  comparable scale to Cloudability's 250 customers managing $9B of spend at
  acquisition.
- **CAC is PLG-shaped**: the pip-install wedge (research doc §7.1) and
  shadow-mode savings report do the qualification; sales enters at the
  team/enterprise conversion (§7.2), so payback is driven by content and
  community until the enterprise stage.

## 5. What has to be true (sensitivity honesty)

The model's four load-bearing assumptions, in order of leverage:

1. **Verified savings rate (18%).** Below ~8%, savings-share revenue thins and
   the business leans on platform fees — still viable, weaker story. This is
   exactly what feasibility experiment E2 measures before product build; the
   the two existence data points (15% on one agent/run family and 59–70% on one
   cache-shaping workload) show that levers exist but do not statistically bracket
   our fleet result.
2. **Metered share of agentic devs (25%→45%).** If providers win the world
   onto flat subscriptions, the dollars story shrinks — but the capped-lane
   value ("more agent-hours per subscription") grows in its place, monetized
   via Pro; the market doc's quarterly capped-vs-metered graph is the
   steering instrument.
3. **Residual waste survives provider-native optimization.** The Railgun
   risk: Anthropic's compaction beta et al. eat the baseline. Mitigation is
   the audit position (verify *their* features too) and the harness/turn-
   elimination levers providers won't touch.
4. **Take rate (20%) holds.** FinOps comps sat at 10–20% for a decade because
   verified savings-share is self-justifying; competition could compress it —
   at 10% the base case is still a ~$45M-ARR 2030 business on the same
   penetration.

Every one of these is a blue cell in the spreadsheet — stress them directly.

## 6. The exit/comparable frame

The FinOps arc is the template: category emerges ~3 years after the spend
crisis (2015 cloud → CloudHealth exit 2018), first exits at ~$500M
(CloudHealth/VMware), category consolidator at $4.6B (Apptio/IBM), market at
$14.9B → $26.9B (2025→2030). Token FinOps is at the 2015 point of that arc
with the spend growing faster. The differentiated asset at exit is the same
one that wins the market: the outcome-verified evidence flywheel — the thing
an acquirer (provider, gateway platform, or DevOps suite) cannot rebuild by
writing code, only by having been in the request path.

---

*Model mechanics: all drivers live on the spreadsheet's Assumptions tab
(blue = editable, yellow = key levers); Market, Revenue, and UnitEcon tabs
are formula-driven; every external figure is cited on the Comps tab. Update
the assumptions as E1–E5 experiment data arrives — the model is meant to be
falsified forward.*
