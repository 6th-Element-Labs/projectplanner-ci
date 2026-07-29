# Token Optimization Cloud — business model and market potential

Status: research draft, companion spreadsheet: `TOKEN-OPTIMIZATION-BUSINESS-MODEL.xlsx`
(same directory — all figures below are computed by that model; edit the blue
Assumptions cells and everything recalculates)
Series: techniques → market analysis → tech design → feasibility → RouteLLM note → **this**

**One-line thesis:** Coding-agent inference is a ~$7B spend pool in 2026 growing
to ~$38B by 2030; an outcome-verified optimizer that removes ~18% of addressable
waste and takes 20% of verified savings — plus Pro subscriptions and platform
fees — has a serviceable revenue pool crossing $1.8B by 2030, of which a
credible base case captures ~$56M ARR at 85% gross margin, on the FinOps
playbook (nOps/Zesty take rates; CloudHealth/Apptio exit paths).

---

## 1. Why this market, why now (the demand evidence)

Four load-bearing facts from the 2026 research (every figure sourced on the
spreadsheet's Comps tab):

1. **The spend is exploding faster than budgets.** Per-developer AI consumption
   grew ~18.6x in nine months; enterprise monthly AI spend averaged $85.5k in
   2025 (+36% YoY); inference is now ~85% of enterprise AI budgets — the
   second-largest line item after talent. Agentic workflows burn 5–30x the
   tokens of chat, and a single agentic coding task consumes 400k–2M tokens.
2. **The pain is public.** Uber exhausted its annual AI coding budget
   two-thirds through 2026; Microsoft curtailed internal Claude Code access
   over cost. OpenAI *doubled* GPT-5-line API prices in April 2026. This is
   the cloud-cost crisis of 2015 replaying at 4x speed, before the FinOps
   tooling exists.
3. **The savings are proven, not hypothetical.** CodexZero: 15% fewer tokens
   at an identical benchmark score, lossless-only, one agent. ProjectDiscovery:
   cache-hit rate 7%→84%, total LLM spend cut 59–70%, no quality change. Our
   18% blended verified-savings assumption is conservative against both.
4. **The obvious competitors are structurally absent.** Gateway per-token
   markups raced to zero (pipes can't monetize); marketplaces earn a % of
   token flow (savings cut their own revenue); providers optimize only their
   own silo. The FinOps analogy says a neutral savings-verified layer emerges
   anyway — and gets bought (CloudHealth → VMware ~$500M; Apptio → IBM $4.6B).

## 2. Market sizing (bottoms-up; Market tab)

Built from developer counts, not top-down percentages of AI market reports:

| Driver ($ pools in $M/yr) | 2026 | 2030 |
|---|---|---|
| Professional developers worldwide | 32M | 40M |
| Using agentic coding tools | 30% → 9.6M | 70% → 28M |
| API-metered (enterprise) share | 25% → 2.4M devs | 45% → 12.6M devs |
| Avg API spend per metered dev | $150/mo | $210/mo |
| **TAM: metered coding-agent API spend** | **$4,320M** | **$31,752M** |
| + Subscription-lane spend ($35/mo blended) | $3,024M | $6,468M |
| **Total coding-agent spend TAM** | **$7,344M** | **$38,220M** |

Cross-check: the enterprise LLM market is forecast $5.9B (2025) → $91.5B
(2036) at 28.3% CAGR, with inference at ~85% of AI budgets and coding agents
the dominant agentic workload — a $32B metered coding-agent pool by 2030 sits
inside those envelopes rather than exceeding them.

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
| Conservative | 0.4 | 1.6 | 4.4 | 9.7 | 18.7 | 1.0% |
| **Base** | **1.1** | **5.2** | **13.3** | **28.5** | **56.2** | **3.0%** |
| Aggressive | 3.0 | 13.0 | 35.4 | 77.8 | 149.8 | 8.0% |

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
  ≈ **$60.6k** ($24.6k savings-share + $36k platform), customer nets ~$98k.
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
   two proven data points (15% lossless floor, 59–70% cache-shaping case)
   bracket it from both sides.
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
