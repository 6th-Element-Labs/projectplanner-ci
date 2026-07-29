# Why we don't fork RouteLLM and "become the active version of it"

Status: reviewed build-versus-adopt note, fifth in the series (techniques → market →
tech design → feasibility → this); revisit if RouteLLM's maintained scope changes
Question answered: RouteLLM is open source and does model routing — why not
fork it and evolve it into the active, transforming optimizer we've designed?

**One-line answer:** Forking RouteLLM would adopt a research router trained for
a different objective, workload, and protocol layer while leaving the optimizer's
hard parts unbuilt. We should evaluate and borrow its *ideas* (threshold calibration, router
architectures, evaluation metrics) into our tier-5 control plane and train
them on our own outcome data; we do not take its code as a foundation.

---

## 1. What RouteLLM actually is

lm-sys/RouteLLM (Berkeley/LMSYS, paper arXiv:2406.18665) is a **research
framework for serving and evaluating LLM routers**: given a single prompt,
predict whether a "weak" (cheap) or "strong" (expensive) model will produce a
preferred answer, and route accordingly under a cost/quality threshold α.

Its contents:

- Four router architectures — similarity-weighted ranking, matrix
  factorization, a BERT classifier, a causal-LM scorer — trained on ~80k
  Chatbot Arena **human preference comparisons** (augmented with GPT-4-judge
  labels), hosted on Hugging Face.
- An OpenAI-compatible serving shim and an evaluation harness with the APGR
  metric (average performance-gap recovered vs. always-strong).
- Headline result: ~40% fewer GPT-4 calls at <5% MT-Bench quality loss.

It is a good paper and a clean reference implementation. None of that makes it
a foundation for our product, for five compounding reasons.

---

## 2. Reason 1 — wrong layer: it decides *which* model, we transform *what's sent*

RouteLLM's entire action space is a single decision per request: strong or
weak. It never touches the payload. Our product's value is overwhelmingly in
the payload path: session-stateful dedup, RLE, paging, delta re-reads, cache
shaping, the artifact store, the exact-tokenizer gate, the evidence plane
(tech deep dive §§3–9). Fork RouteLLM and those components remain unbuilt.
"Becoming the active version of RouteLLM" still means building nearly all of the
optimizer-specific system; the fork decision only governs whether the routing
decision function starts from someone else's
research scaffolding or from our own control plane, where it must live anyway
(MODEL-CATALOG-ROUTING already specifies our routing contract: explainable
`routing_decision` records, discover→promote→pin, fail-closed lanes — none of
which RouteLLM has).

## 3. Reason 2 — wrong training distribution: Arena preferences ≠ agent outcomes

The routers are calibrated to predict **which of two open-ended chat answers a
human prefers**. Our traffic is multi-turn agentic tool use, where the
criterion is **did the structured task succeed** — tests pass, diff applies,
CI green. These are different prediction targets on different input
distributions; the published routers are explicitly not calibrated for
structured agent-task success, and single-turn prompt features barely exist
mid-session (the "prompt" is a 200k-token transcript). The trained models —
the only genuinely hard-won asset in the repo — are therefore the part we
*cannot* use. We would retrain from scratch on outcome-labeled fleet data
(Tally cost-per-verified-outcome, completion gates), which is precisely the
dataset advantage our evidence plane exists to build. A fork inherits the
scaffolding and discards the crown jewels; that is the worst trade available.

## 4. Reason 3 — wrong protocol surface for 2026 agent traffic

RouteLLM speaks single-shot OpenAI chat completions. Our feasibility doc
(§3) established the actual requirements: Anthropic `/v1/messages` with SSE
and signed thinking blocks that must round-trip byte-exact; the OpenAI
**Responses API** (which Codex now *requires* — chat-completions providers
fail on startup); `cache_control` forwarding; `/v1/models` discovery;
`count_tokens`; gateway-satisfied tool inner loops. Retrofitting a stateless
single-turn research server into a session-stateful, signature-preserving,
streaming middlebox is strictly more work than building on our designed
architecture — and mid-session model switching, RouteLLM's core move, is
exactly the operation our cache-economics engine must veto most of the time
(switching models discards the provider prefix cache; a hot 1-hour Anthropic
prefix is a routing input, not a free variable). RouteLLM has no concept of
this constraint; our design treats it as first-class (tech doc §13, item 3).

## 5. Reason 4 — the repository is not the product foundation we need

It is an evaluation-first research framework with published assets centered on
the model generation studied by the project. Current maintenance and production
support should be rechecked in any dependency spike rather than inferred from that
origin. Model landscapes turn over quickly; a router is only as good as its most
recent calibration.
The durable thing to own is the **recalibration loop** (our promotion state
machine and release-day re-validation, tech doc §9), not any particular
trained router. Forking may hand us stale-for-our-workload weights and a serving
shim while leaving the loop to build ourselves. Licensing is not the obstacle;
fitness for our protocol, state, and outcome objective is.

## 6. Reason 5 — wrong strategic lane

The market doc (§1.3) treats routing as a crowded and increasingly bundled lane.
OpenRouter's auto-router, Martian, NotDiamond, and gateway routing features
compete there. "The active version of RouteLLM" is a positioning statement
that files us in that lane, against free, at exactly the moment the lane's
differentiation collapsed. Our niche claim is the layer none of them occupy:
outcome-verified payload optimization with routing as *one joined decision
input* — tier + compression profile + cache state, chosen per wake, recorded
in one explainable decision (MODEL-CATALOG-ROUTING + tech doc §13, item 3).
Routing is a feature of our decision; it must not become our identity.

---

## 7. What we do take from RouteLLM

Borrow the ideas, cite the paper, reimplement inside our control plane:

1. **The α-threshold abstraction** — a single dial trading cost vs. quality,
   per tenant/task-class. Clean UX for our tier-5 profile selection.
2. **APGR-style evaluation** — "performance gap recovered per dollar saved"
   restated over verified outcomes instead of MT-Bench becomes our headline
   routing metric.
3. **Router architecture menu** — matrix factorization and small-classifier
   routers are the right *shape* of model for per-wake tier selection; we
   train them on outcome-labeled fleet data, following our routing doc's
   discover→promote→pin lifecycle.
4. **The honesty of its result** — 40% call reduction at <5% quality loss on
   the right distribution shows routing's real headroom; it stacks with,
   rather than substitutes for, payload optimization.

The training data, calibration loop, compatibility contract, and joined
cache-aware decision are the durable work regardless of whether code is reused.
Before reimplementing, perform a bounded dependency spike: inventory reusable
evaluation components, verify license and maintenance state, estimate adapter
cost, and compare that with a small native implementation. The architectural
verdict is “do not adopt it as the product foundation,” not “none of its code can
ever be a useful dependency.”

---

## 8. Verdict

Foundation fork: no. Evaluate components and borrow ideas: yes — abstractions,
metrics, and architecture choices into
the tier-5 routing join specified in MODEL-CATALOG-ROUTING and the tech deep
dive, trained on the evidence plane's outcome data. RouteLLM answered "which
model should answer this prompt?" for 2024 chat traffic. Our product answers
"what is the cheapest verified way to finish this task?" for 2026 agent
fleets — a question whose answer is mostly *not* a routing decision, and
whose routing component needs our data, not theirs.

Sources: github.com/lm-sys/RouteLLM · RouteLLM paper (arXiv:2406.18665) ·
router comparison and routing-architecture surveys (clawrouters.com,
openlegion.ai, zylos.ai) · HyDRA hybrid routing (arXiv:2605.17106) ·
companions: TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md ·
TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md · TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md ·
TOKEN-OPTIMIZER-FEASIBILITY-DEEP-DIVE.md · MODEL-CATALOG-ROUTING.md.
