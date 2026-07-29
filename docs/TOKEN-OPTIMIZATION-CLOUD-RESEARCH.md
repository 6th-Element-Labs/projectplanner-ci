# Token Optimization as a Service — research & scoping

Status: research and product-scoping draft; not accepted architecture or an implementation commitment
Depends on: LiteLLM API-only boundary, Tally cost-to-outcome attribution, `routing_decision` records

**One-line contract:** Give coding-agent teams a context flight recorder and compiler:
show where spend and reliability leak, remove deterministic waste on certified traffic
lanes, preserve recoverability where policy permits, and measure whether each
optimization improves cost per verified outcome.

The thesis in one sentence: WAN optimization for the token economy, delivered the way
Cloudflare delivered WAN/edge services (SaaS via a one-line config change) instead of
the way Cisco/Riverbed delivered them (boxes) — where the durable asset is not the
proxy but the evidence flywheel that proves savings never cost outcomes.

## 0. Product boundary: standalone layer, amplified by Switchboard

This product must stand on its own in the agentic-AI coding stack. Its standalone
contract is an agent- and provider-compatible context-efficiency layer that can sit
between a supported agent API lane and a model provider, or run beside the agent as a
local harness. It provides value without Switchboard: coverage attestation, context
linting, request and usage receipts, safe transforms, bounded artifact recovery, and
customer-supplied outcome joins.

Switchboard is the 2+2=5 environment, not a hard dependency. It contributes task and
project identity, runtime adapters, routing policy, CI/review/merge evidence, and
Tally's cost-to-verified-outcome denominator. Gateway observations remain evidence;
they cannot establish capacity, start work, acknowledge communication, complete a
claim, or prove Done.

The first product is not generic prompt compression. It is a no-mutation Context
Doctor and flight recorder that answers:

- which inference traffic is captured, bypassed, or unsupported;
- which exact spans caused cache misses, repeated spend, retries, or unnecessary wakes;
- what could have been saved after provider cache discounts and optimizer overhead;
- whether a later enforced optimization changed task success, latency, or reliability.

---

## 1. The lineage map

Every core technique in this product has a 20–40-year-old ancestor. That matters for
two reasons: the failure modes are already documented, and the analogies tell us which
parts are commodity (the transforms) and which are the business (measurement, trust,
distribution).

| Classical technique | Where it came from | Token-economy equivalent |
|---|---|---|
| Data redundancy elimination (DRE) | Cisco WAAS, Riverbed SDR | Cross-turn duplicate tool-result suppression |
| Content-defined chunking | LBFS (SOSP '01), rsync | Sub-result dedup of file reads / logs across turns |
| Delta encoding | VCDIFF (RFC 3284), RFC 3229, rsync, Cloudflare Railgun | Turn-over-turn context deltas; re-read-after-edit diffs |
| Stateful header compression | Van Jacobson TCP, ROHC, HPACK (RFC 7541)/QPACK | Tool-schema and boilerplate dedup across requests |
| Run-length encoding | Everywhere since ITU fax | Repeated-line collapse in terminal output (CodexZero `line-rle-v1`) |
| HTTP conditional requests (ETag/304) | HTTP/1.1 | "Same output as tool call N, source state hash X" references |
| Content-addressed storage / Merkle trees | git, Nix, Bazel remote cache, ccache | SHA-256 artifact store; proof-bound reuse |
| Virtual memory / paging / working set | OS design (Denning '68) | Context paging: evict cold tool outputs, fault them back via an `expand_artifact` tool |
| Cache eviction science (LRU/LFU/TTL) | CPU caches, Redis | Context eviction policy: which turn content stays resident |
| Materialized views + incremental view maintenance | Databases | Prefix-stable incremental context: transform only the new suffix, never rewrite history |
| Protocol-specific application optimizers | WAAS CIFS/MAPI/HTTP AOs | Agent-family-aware optimizers (Claude Code vs Codex vs Cursor transcript shapes) |
| TCP Fast Open / connection warmup | Networking | Cold-start prefix minimization: lean prompts, lazy tool-schema loading |
| Semantic/result caching | Memoization, Redis, CDN edge cache | Semantic caching of idempotent sub-queries (GPTCache lineage) |

The single most important discipline borrowed from all of them: **optimize the layer
you're on without breaking the caching layer above you.** For us that layer is
provider prompt caching, whose write/read prices, eligibility, and retention vary by
provider and model. Any transform that is not deterministic and prefix-stable can
*increase* the bill while
"saving tokens." This is the token-gateway version of the WAAS rule that DRE must not
defeat downstream QoS or application caching.

---

## 2. Techniques from classical CS and networking

### 2.1 Data redundancy elimination → cross-turn dedup

WAAS DRE and Riverbed SDR kept synchronized dictionaries of previously-seen byte
segments at both ends of a WAN link and replaced repeats with short references.
On a certified API lane the gateway can observe each routed request in the session,
and the reference format can be standard JSON the model already understands.
CodexZero's exact-duplicate suppression is the proven miniature: replace a repeated
read-only result with `{"same_output_as_tool_call": …, "source_state_sha256": …}`
only when content is byte-identical, the command is proven read-only, source state
(file blob hash, git HEAD/index fingerprint) is unchanged, and the original is still
in active context.

Borrow from LBFS/rsync: **content-defined chunking** (Rabin fingerprints) so that
*partially* repeated content dedups too — the classic case is a re-read of a file
after a small edit, or a test log that repeats 90% of the previous run. Anchored
chunk boundaries survive insertions; fixed-size blocks don't.

### 2.2 Delta encoding → "changed since turn N" results

rsync/VCDIFF taught: when both sides share an old version, send only the diff.
Coding agents re-read files they just edited constantly. A gateway that holds the
turn-N version can rewrite the turn-M re-read as a unified diff against the copy
already in context — smaller *and* often more useful to the model. Cloudflare
Railgun did exactly this for dynamic HTML between edge and origin. Railgun's
retirement is also the cautionary tale: it required a paired component at the origin
(deployment friction) and its gains shrank as Brotli/HTTP2 improved — see §6 on the
shrinking-residual risk.

### 2.3 Stateful header compression → schema and boilerplate dedup

HPACK/QPACK compress HTTP headers by maintaining a shared dynamic table across
requests on a connection: the first `user-agent` costs full price, every subsequent
one costs an index. The agent-transcript equivalent is glaring: agents resend full
tool schemas, system prompts, and instruction boilerplate on **every** request.
Providers already discount stable prefixes via prompt caching — so the gateway's job
here is not to compress these bytes itself but to **maximize prefix stability**
(HPACK's table = the provider's KV cache) and to shrink what enters the prefix at
all: lazy/deferred tool schemas (load on first use via a tool-search affordance),
minimal system prompts (CodexZero's 4.8 KB lean prompt vs stock), dialect-reduced
JSON schemas for cheap-tier calls.

Critical distinction this analogy surfaces: **wire compression ≠ token compression.**
gzip between agent and gateway saves bandwidth, zero tokens. A transform only counts
if it survives the model's tokenizer as fewer tokens *and* remains intelligible to
the model. This kills "clever symbol vocabulary" ideas: invented glyph codes are
out-of-distribution and cost explanation tokens; natural-language markers
(`[repeated 12 times]`) and standard JSON are free because pretraining already paid
for them.

### 2.4 Conditional requests and content addressing → the artifact store

HTTP's `If-None-Match`/304 pattern and git/Nix/Bazel content addressing combine into
the evidence layer: when policy permits retention, raw output is stored before a
compact candidate is eligible, compact forms carry opaque artifact capabilities, and
a compatible `expand_artifact` path lets the model recover exact bytes on demand. In
zero-retention mode, transforms that require later expansion are unavailable unless
the customer supplies the store.

A content hash proves byte identity, not authorization, freshness, tenancy, or safe
reuse. Artifact capabilities must be tenant- and session-scoped; physical dedup must
not let one tenant infer another tenant's content through hashes, errors, or timing.

### 2.5 Virtual memory → context paging

Denning's working-set model maps directly: an agent session's context is a memory
hierarchy where "resident" = in the prompt (expensive per turn) and "swapped" = in
the artifact store (free until faulted). MemGPT (arXiv:2310.08560, now Letta) proved
LLMs can drive their own paging via tools. A gateway can identify candidates without
agent cooperation, but reliable recovery requires a tool or protocol the agent/model
can actually invoke. Without a certified expansion path, remain in observe mode.
Eviction policy is Redis's science — LRU/LFU approximations, TTLs, and the key
insight that *recency and frequency both matter*: a file read 10 turns ago that the
model keeps referencing must stay resident.

### 2.6 Incremental view maintenance → prefix stability as a law

Databases learned not to recompute materialized views from scratch; we must learn
never to re-transform history. **Rule: a transformed turn is frozen forever; only
the new suffix is ever processed.** This makes transforms idempotent and
deterministic, keeps provider prompt caches hot, and makes replay/audit possible.
It is the single non-negotiable architectural law of the data plane.

### 2.7 Application optimizers → agent-family awareness

WAAS shipped per-protocol optimizers (CIFS, MAPI, HTTP) because generic byte-level
DRE missed protocol-specific waste (chatty round trips). Same here: generic dedup
misses agent-specific waste. Per-family optimizers know that Claude Code transcripts
carry system-reminder blocks and tool schemas with a known shape, Codex carries
`exec` output framing, Cursor batches file context differently. The registry of
"known agent dialects" is a growing, defensible asset — exactly like the AO catalog
was for WAAS.

### 2.8 Protocol chattiness → turn elimination

The biggest WAAS wins were often not compression but **round-trip elimination**
(CIFS read-ahead, TFO). Token equivalent: eliminating whole model calls, which cost
the entire accumulated context each time. CodexZero's event-driven wait (empty poll
results never wake the model) and batched validation runs (`run-checks` returns one
structured result for N commands) are the pattern. A gateway sees repeated no-op
polling turns and can, with agent cooperation, collapse them. Fewer wakes beats
smaller wakes.

---

## 3. Techniques from LLM academic research

Marked by where they can run: **[G]** gateway-applicable as-is, **[G+A]** needs
agent cooperation, **[P]** provider/model-side only (watch, don't build).

### 3.1 Hard prompt compression [G, lossy tier]

- **Selective Context** (Li et al., 2023) and **LLMLingua / LongLLMLingua /
  LLMLingua-2** (Microsoft; arXiv:2310.05736, 2310.06839, 2403.12968): use a small
  LM to drop low-information tokens; up to ~20x on RAG-style prompts with modest
  degradation. The 2024 survey (arXiv:2410.12388) is the field map.
- Caution: results are **benchmark-dependent** — compression that is free on GSM8K
  can hurt code tasks. For coding agents this is strictly a tier-4 opt-in, applied
  only to low-risk spans (old commentary, doc excerpts), never to code, diffs, or
  diagnostics, and always behind paired outcome evals.
- Where it shines for us: compressing *our own* injected summaries and stubs, where
  we control the text anyway.

### 3.2 Learned/soft compression [P — watch only]

Gist tokens (arXiv:2304.08467), ICAE, AutoCompressors compress prompts into learned
embeddings. Requires model-weight access; a black-box gateway cannot use them. Track
because providers may productize (this is what native "context compaction" features
become), which shrinks our residual — see §6.

### 3.3 Semantic caching [G, restricted]

GPTCache (Zilliz), MeanCache, and Redis **LangCache** (managed semantic caching,
2025) validate the category; published measurements find ~30% of queries in
chat-style workloads are semantically similar to prior ones. For coding agents,
similarity-based reuse is dangerous (near-identical prompts with one changed line
*must not* reuse). Restrict to: exact-match reuse under proof of unchanged source
state (our DRE, effectively), and similarity reuse only for provably idempotent,
side-effect-free lookups (docs queries, symbol lookups) with the proof recorded.
Redis's role here is also a build-vs-buy signal: the cache substrate is commodity;
the *proof-of-safety layer* is not.

### 3.4 KV-cache and prefix-reuse systems [P — dictates our discipline]

vLLM PagedAttention (SOSP '23), SGLang **RadixAttention** (prefix-tree KV reuse),
CacheGen (SIGCOMM '24, KV-cache compression/streaming), CacheBlend. These are
provider/self-host-side, but RadixAttention's radix-tree view of shared prefixes is
  exactly the mental model for **why prefix stability is law** (§2.6): stable bytes
  may remain eligible for provider or self-hosted cache reuse. For customers running
self-hosted models (vLLM/SGLang), the gateway can go further and actively shape
requests to maximize radix-tree hits — a segment where our savings are largest.

### 3.5 Cascades and routing [G+A — the sibling scope]

FrugalGPT (arXiv:2305.05176) established the cascade + prompt-adaptation + cache
triad; RouteLLM (lm-sys) ships trained routers. In Switchboard this is the
MODEL-CATALOG-ROUTING scope: dispatch-time tier selection with explainable
`routing_decision`. The join point: a per-wake decision should select **tier +
compression profile together** (Klaat-style tier-aware context budgets — "don't hand
a cheap worker the full tool schema and a 200k pack" — is compression-by-routing).

### 3.6 Agent memory and summarization [G+A]

MemGPT/Letta (paging, §2.5), hierarchical summarization buffers (LangChain lineage),
A-MEM. Gateway-side: replace stale spans with summary + artifact hash. Summarization
is lossy and model-generated, so it lives in tier 4 with eval gates — but unlike
generic summarization research, we hold ground truth (the artifact) and an escape
hatch (expand tool), which most academic setups lack.

### 3.7 Tool-call efficiency [G+A]

CodeAct (arXiv:2402.01030) — executable code actions instead of JSON tool calls —
matches CodexZero's "Focused" exec-cell mode: one code cell composes N tool calls,
collapsing N round trips of schema + result framing into one. Also in this family:
schema minimization (strip descriptions for high-tier models that don't need them),
deferred tool loading, and batched parallel reads. These need agent-side or
harness-side hooks — in Switchboard, the runtime adapters are that hook.

### 3.8 Output-side economics [G+A]

Output tokens are often materially more expensive than input, with ratios varying
by provider and model. Levers: suppress reasoning summaries where the
harness allows (CodexZero keeps encrypted reasoning, omits the summary), terse
commentary instructions ("caveman mode" — works because commentary is for humans
and doesn't affect task correctness), `max_tokens` discipline per task class, and
structured-output schemas that don't force verbose framing. Speculative decoding is
[P] — latency, not tokens.

---

## 4. GitHub repos to mine

| Repo | What it proves | What to borrow |
|---|---|---|
| Retro2512/CodexZero | Local "box" version of this product for one agent; ~15% on terminal-bench with score parity | The constitution: exact-tokenizer strictly-smaller gate, SHA-256 artifact store, fail-open to original, self-describing markers, proof-bound dedup, fixture replay suite |
| microsoft/LLMLingua | Hard prompt compression works, black-box | Tier-4 compressor for our own injected text |
| zilliz/GPTCache | Semantic caching architecture | Cache interface shape; embedding-similarity plumbing (restricted per §3.3) |
| BerriAI/litellm | The de-facto OSS gateway; we already bundle it | Middleware/callback insertion points; provider abstraction; don't rebuild |
| Helicone / Portkey / Kong AI gateway | Gateways add observability, caching, governance — none do deep payload transforms | The gap we fill; also their pricing/PLG motions |
| lm-sys/RouteLLM | Trained model routing | Router features for the routing scope join |
| letta-ai/letta (MemGPT) | Context paging via tools works | Page-fault UX for `expand_artifact` |
| sgl-project/sglang, vllm-project/vllm | Prefix reuse economics | Request shaping for self-hosted lanes |
| openai/tiktoken, huggingface/tokenizers, anthropic token-counting APIs | Model-specific or provider-authoritative counting | Candidate-size evidence; provider usage remains billing truth |
| pleasedodisturb/awesome-llm-token-optimization | Curated field map | Ongoing scan for new techniques |

2026 commercial landscape check (validation, not moat): The Token Company (YC W26)
sells compression-as-API; TokenShift does endpoint-local compression for coding
agents; Kong ships a prompt-compression plugin; Redis ships LangCache. The category
is real and forming. Agent-aware transforms plus outcome evidence and
savings-aligned billing are a differentiation hypothesis to revalidate.

---

## 5. Company playbooks

### 5.1 Cisco WAAS (and Riverbed): the box era

What worked, and transfers:

- **Symmetric two-ended optimization.** DRE needed synchronized state both ends. We
  get this for free (gateway sees every turn), but the deeper lesson is that the
  *stateful* optimizer beat stateless compression 5:1. Our state = session
  transcripts + artifact store + fleet evidence.
- **The Central Manager sold the product.** Savings dashboards per link/application
  were the sales artifact — customers bought the *report*, then kept the box.
  Our `codex-zero savings` / shadow-mode savings report is the same wedge.
- **Transparent interception** (WCCP/inline) meant no client changes. Our preferred
  equivalent is a supported base-URL override where available, with explicit
  harness integration for personal-subscription and closed vendor lanes.
- **Per-protocol AOs** were the roadmap engine — each new AO expanded the market.
  Ours: per-agent-family optimizers.

What failed, and warns:

- **Appliance economics died.** Hardware refresh cycles, per-site deployment, and
  the cloud shift (traffic stopped flowing site-to-site) killed the category.
  Lesson: never let the product require a customer-deployed component in the
  critical path (CodexZero's patch-the-binary approach is the box; we are the
  cloud). A self-hosted edition can exist for enterprises, but as a deployment
  option of the same software, not a separate product.
- **Optimization fought encryption.** TLS everywhere forced painful MITM
  architectures. Our version of this risk: provider ToS and E2E-signed requests.
  Stay BYO-key and terms-clean from day one.

### 5.2 Cloudflare: the SaaS conversion

- **Onboarding is the product.** DNS change → instant value. Ours: one env var.
  Anything requiring an SDK or code change loses to this.
- **The trust ladder.** Cloudflare started observe/protect (CDN, WAF in front,
  content untouched), earned trust, then shipped content-modifying features
  (Minify, Polish) as opt-in. **Rocket Loader** — their JS-rewriting feature — broke
  sites and taught the lesson: payload mutation without a provable-equivalence gate
  burns trust fast. Our tier ladder follows the same adoption pattern, but every
  tier must publish its narrower proof class. Strictly fewer tokens and retained
  source bytes do not prove semantic equivalence.
- **Railgun's retirement**: a delta-compression product whose residual shrank as
  the baseline improved (Brotli, HTTP/2, origin-pull caching). Expect the same
  pressure from provider-native caching/compaction, and plan the moat around
  evidence, not transforms (§6).
- **Workers**: the edge became a platform — customers write their own middleware.
  End-state for us: a policy/transform SDK where customers (and we) ship custom
  optimizers into the data plane, gated by the same evidence framework.
- **Free tier as data engine.** Free traffic trained their threat models. Our free
  shadow mode generates the savings-and-safety corpus that trains the evolution
  loop.

### 5.3 Redis: the OSS wedge

- **Be the default component, monetize the managed version.** An OSS gateway core
  (or first-class LiteLLM middleware) makes us the default way anyone measures
  token waste; the cloud sells the evidence flywheel, fleet-trained profiles,
  artifact storage, and savings-verified billing. This also answers the
  LangChain/OpenRouter squeeze: frameworks integrate us instead of building it.
- **Eviction science as differentiation.** Redis won partly on the sophistication
  of a "simple" thing (approximated LRU/LFU, TTL semantics). Context-eviction
  policy is our equivalent deep-simple problem.
- **LangCache** proves an infra incumbent will enter with the generic version —
  and validates that the agent-aware, outcome-verified version is the defensible
  slice.

---

## 6. Product synthesis

### 6.1 Three planes

These are internal optimizer components, not Switchboard lifecycle authorities.
They remain subordinate to ADR-0008 in an integrated deployment.

- **Data plane** — the proxy: OpenAI-/Anthropic-compatible endpoints, streaming
  fidelity, model-specific counting, transform pipeline, artifact store,
  session state. Stateless-restartable; state in the store.
- **Control plane** — policy: which transform tiers per customer/lane/model,
  prefix-freeze ledger, agent-dialect registry, cache-economics model per provider
  (breakpoints, TTLs, discounts).
- **Evidence plane** — the moat: per-technique savings telemetry, paired outcome
  evals (fixture replay + live A/B against verified outcomes), auto-promotion of
  transforms per model release, per-customer bill-verified savings reports.

### 6.2 Technique tiers (the trust ladder)

| Tier | Contents | Guarantee | Default |
|---|---|---|---|
| 0 Observe | Telemetry, would-have-saved counters | No mutation | On |
| 1 Hygiene | ANSI/pager strip at source (env), whitespace/JSON compaction | Byte-preserving or bounded presentation change | On after dialect certification |
| 2 Exact reference | RLE, byte-identical references, schema/cache shaping | Source identity proven; model-visible representation changed | Canary after outcome evidence |
| 3 Recoverable projection | Stale-output eviction, successful-check projections, delta re-reads | Exact source retrievable through a certified expansion path | Opt-in |
| 4 Behavioral | Lean prompts, terse commentary, LLMLingua on injected text, summarization | Eval-gated per model; paired outcome evidence | Opt-in |
| 5 Routing | Tier + compression profile per wake (sibling scope join) | Explainable `routing_decision` | Opt-in |

### 6.3 Applicability matrix (summary)

| Technique | Locus | Needs agent hooks | Est. savings share |
|---|---|---|---|
| Exact dedup + state proofs | Gateway | No | High (tool-heavy sessions) |
| RLE / hygiene | Gateway | No (better with env injection) | Medium |
| Stale-output paging | Gateway plus certified tool loop | Usually | Unknown until E2/E4 |
| Delta re-reads | Gateway plus expansion path | Usually | Unknown until E2/E4 |
| Prefix-stability / cache shaping | Gateway | No | High (bill, not tokens) |
| Lean prompt / schema minimization | Harness/launcher | Yes | Medium; big on cold start |
| Turn elimination (event-wait, batching) | Harness/launcher | Yes | High per avoided wake |
| Output-side (terse, summaries off) | Harness/launcher | Yes | Unknown; model prices vary |
| Hard compression (LLMLingua) | Gateway | No | Small, risky; own-text only |
| Semantic caching | Gateway | No | Small for coding; restricted |

Note the split: the biggest gateway-only wins are dedup, paging, and cache shaping;
the biggest total wins add harness cooperation. Switchboard uniquely holds both
levers (LiteLLM lane + runtime adapters/launch env), including for personal-CLI
lanes that the gateway must never proxy (per MODEL-CATALOG-ROUTING: personal CLI
auth never goes through the gateway).

### 6.4 The constitution (non-negotiables, from CodexZero + Rocket Loader's grave)

1. Candidate eligibility uses the most authoritative available model-specific count;
   ship only if strictly smaller and projected billed cost does not increase. Counts
   alone never establish semantic safety.
2. When retention policy permits, raw bytes are stored before a transform requiring
   recovery is eligible. In zero-retention mode those transforms are disabled.
3. **Prefix stability is law**: transformed history is frozen; only the suffix is
   processed. Objective is cost-per-task, not tokens.
4. Never invent symbol vocabularies; compact forms must be in-distribution
   (natural-language markers, standard JSON, hashes as opaque IDs).
5. Failed commands, errors, warnings, exit codes are never projected or elided.
6. Every tier-3+ transform carries an escape hatch (`expand_artifact`).
7. Every enforce-mode technique has shadow-mode history and paired outcome
   evidence on the target model before promotion; model releases trigger
   re-validation.
8. Gateway credentials and upstream provider credentials are distinct. Upstream
   credentials are server-held or customer-vaulted, never returned to the agent.
   Retention modes and every transform decision remain auditable per request.

---

## 7. Business model and GTM

- **Wedge:** free shadow-mode savings report ("here is what you would have saved
  last month, per technique, per model, with zero risk taken"). The WAAS Central
  Manager lesson: the report sells the product.
- **Billing:** savings-share on bill-verified reductions (WAN optimizers were sold
  on measured bandwidth). Flat infra pricing as fallback for procurement that
  can't do variable.
- **OSS:** proxy core open (Redis lesson) — likely as LiteLLM-ecosystem middleware
  rather than a new gateway binary; monetize the evidence cloud, fleet profiles,
  artifact store, and verified billing.
- **Distribution via Switchboard:** the fleet is dogfood corpus + first customer;
  outcome verification (completion gates, CI receipts, review verdicts) closes the
  quality loop no standalone gateway can close. Build behind the existing LiteLLM
  boundary with a clean seam (attribution via headers; empty = standalone mode) so
  spin-out stays cheap.
- **Structural-defense hypothesis:** cross-agent compatibility evidence, replay,
  rollback, and verified outcomes are harder to copy than transforms. Revalidate
  this against competitors and customer interviews; do not rely on assumptions
  about another company's future business model.

### 7.1 The pip-install wedge, concretely

The try-before-paying experience is a local launcher and diagnostic proxy,
installable in one line and explicit about which traffic it can observe:

```text
pip install tokenlens        # name TBD; pipx/uvx work too
tokenlens doctor claude      # reports supported auth/feature lanes
tokenlens run claude         # only when the selected lane is gateway-compatible
tokenlens report
```

- **`run` wraps the launch** (the CodexZero onboarding pattern): starts a
  localhost proxy and launches a certified API-key/custom-provider lane with
  the required environment or config. It must refuse to imply that Claude
  Pro/Max OAuth, ChatGPT subscription OAuth, Cursor-hosted models, or Cursor Tab
  traffic is routed through it. `tokenlens up` exists for manual configuration
  and prints the exact supported snippet.
- **Default mode is observe**: no request mutation. The client authenticates to
  the local gateway, which resolves the configured upstream credential without
  logging or returning it. Streaming and usage fidelity are claims earned by E1
  per client/auth/feature profile—not assumed from startup success.
- **`report` is the go-to-market in one screen**: tokens by session,
  repeated-context findings, cache behavior, retries, coverage gaps, and
  would-have-saved estimates. Provider `usage` fields are billing evidence;
  locally computed transform deltas remain estimates until an enforced paired
  run verifies them.
- **`optimize` enables only profiles that passed E4.** The report distinguishes
  `observed`, `estimated`, `mechanically verified`, and `outcome-validated`;
  recoverability is not labeled semantic equivalence.

Technical shape: pip is the distribution hypothesis, not a commitment to a Rust
core. Start with the smallest implementation that can run E1–E2 and profile it;
move bounded hot paths behind a compiled extension only if measured latency
requires it. Local decisions can live in SQLite. Source-bearing artifacts are
disabled by default or encrypted under explicit retention policy, never silently
placed in a global content-addressed directory.

Free-forever vs cloud: the local tier is a complete single-developer
diagnostic and certified-lane optimizer. The paid hypothesis is what local
structurally cannot do: fleet aggregation, team policy, hosted endpoints for
API-based CI/cloud agents, continuously recertified profiles, governed shared
artifacts, billing exports, SLA, and SSO. Hosted service does not unlock closed
subscription or Cursor-hosted lanes.

Two deliberate details: telemetry is opt-in and content-free (aggregate
counters only — technique, token deltas, model, dialect version; never
payloads), because conspicuous cleanliness is a sales asset for a product
that lives in the request path. And the upgrade/share nudge fires once, only
after the first report shows nonzero measured opportunity (the star-prompt
pattern) — the report ends with a one-line paste-into-Slack summary, because
"we observed this repeated spend; here's the receipt" spreading inside a company is what
reaches the cloud buyer.

P0 tie-in: the package is the candidate E1–E2 harness. It becomes the
top-of-funnel product only after E1 certifies fidelity and E2 measures an
economically material segment.

### 7.2 Individual conversion: the free tier proves us, shadow mode prices the upgrade

"Individuals free forever" needs a real upgrade path. Shadow mode can estimate
mechanical opportunity for profiles not currently enabled, but it must not label
those estimates safe or bill-verified before E4 and a paired run:

```text
Provider-reported billed input:       8.1M tokens
Mechanically verified free savings:   1.4M tokens
Estimated additional opportunity:     2.2M tokens
  paging 1.3M · delta re-reads 0.5M · current profiles 0.4M
```

The first two lines are reconcilable to actual requests; the third is a shadow
estimate with confidence and eligibility labels. The report becomes a
personalized upgrade hypothesis, not a promise.

Three high-intent moments, instrumented (and the only places the nudge is
allowed to fire):

1. **API quota or rate-limit pressure.** On certified API lanes, the gateway can
   observe provider `429` responses and estimate whether verified reductions
   would have changed the request volume. Personal-subscription limits are not
   visible to the gateway; a harness may report them only through an explicit,
   separately certified local integration.
2. **The localhost wall.** API-based CI runners and remote custom-provider agents
   may need a hosted endpoint. Claude Code web, Codex cloud subscription, and
   Cursor-hosted traffic do not become addressable merely because a hosted
   gateway exists. Coverage receipts must show that distinction.
3. **Model-release day.** Free gets dialect/profile updates at
   package-release cadence; Pro gets them continuously with the promotion
   state machine behind them. The product reports `certified`, `unknown`, or
   `suspended`; it does not claim same-day safety until the release canary and
   outcome suite actually pass.

Candidate paid packaging: hosted endpoint for supported API lanes, synced
session state, governed recoverable transforms, continuously evaluated profiles,
and quota/cost forecasting. Price and packaging remain interview and willingness-
to-pay experiments; do not fix `$10–19/month` from desk research.

Individuals can become the organization motion through an explicit share or
invite flow. Do not infer coworkers or expose domain-level adoption from email,
credentials, traffic, or telemetry without affirmative workspace consent.

Anti-dark-pattern rules, explicit: the free tier never degrades over time,
safety fixes are never withheld from free users, and nudges are permanently
dismissible. Telemetry is opt-in, minimized, and purpose-bound; source content,
credentials, raw prompts, and cross-user identity are excluded. Upgrades come
from demonstrated operational value, not the free tier being painful.

---

## 8. Risks and open questions

1. **Shrinking residual (the Railgun risk).** Provider-native caching, context
   compaction, and smarter agents eat the baseline waste. Mitigation: moat =
   cross-vendor evidence + verification layer, not transforms; expand into the
   adjacent spend problems (routing, turn elimination, output economics).
2. **Prompt-cache interaction.** Naive transforms can raise bills. Prefix-stability
   law + per-provider cache-economics model; measure cost-per-task, never raw
   tokens.
3. **Provider ToS / neutrality.** Stay BYO-key, transparent, and modification-
   disclosed; the WAAS-vs-TLS lesson says don't build on adversarial interception.
4. **Quality liability.** No blanket equivalence claim. Publish each decision's
   proof class; model-visible substitutions remain outcome-tested even when omitted
   bytes are exactly recoverable.
5. **Privacy/state.** We hold customer code artifacts. Zero-retention mode,
   regional pinning, self-host option (as deployment, not product).
6. **Tokenizer drift.** Model-specific counting and provider-usage reconciliation are load-bearing;
   re-validate on every provider release.
7. **How big is the prize on real fleets?** Unknown until shadow mode runs.
   CodexZero's repeated benchmark says ~15% on Codex/terminal-bench; tool-heavy
   long sessions should be higher (dedup + paging), short sessions lower. This is
   the P0 go/no-go question.

---

## 9. Scoping (phases)

- **P-1 — Compatibility and truth.** Certify the supported Codex and Claude API
  lanes, preserve streaming/tools/errors/usage, issue coverage receipts, and detect
  direct inference bypass. Cursor remains partial until E5 passes.
- **P0 — Shadow (standalone first, Switchboard dogfood second).** Per-request
  provider usage, cache effects, retries, would-have-saved counters, and evaluator
  joins. Add Tally attribution in Switchboard. Go/no-go on net billed opportunity
  after cache discounts and optimizer overhead. No provider-request mutation.
- **P1 — Exact-reference canary (API lanes).** Tier 1–2 transforms behind bounded gates;
  artifact store; `transform_decision` records; verified-outcome regression watch.
- **P2 — Harness lever (CLI + API lanes).** Lean prompt, terminal hygiene env,
  schema deferral via runtime adapters; A/B across fleet.
- **P3 — Reversible tier + paging.** `expand_artifact` tool injection, stale-output
  eviction, delta re-reads; fixture replay suite (CodexZero-style) as CI.
- **P4 — Routing join + productization.** Per-wake tier+profile selection with the
  routing scope; standalone-mode hardening (empty attribution), external design
  partners, savings-share billing pilot.

---

## 10. References

Research: Prompt-compression survey (arXiv:2410.12388) · LLMLingua
(arXiv:2310.05736) · LongLLMLingua (2310.06839) · LLMLingua-2 (2403.12968) ·
Selective Context (2304.12102) · Gist tokens (2304.08467) · MemGPT (2310.08560) ·
FrugalGPT (2305.05176) · RouteLLM (2406.18665) · CodeAct (2402.01030) · PagedAttention
(SOSP '23) · CacheGen (SIGCOMM '24) · LBFS (SOSP '01) · VCDIFF (RFC 3284) · HPACK
(RFC 7541).

Repos: Retro2512/CodexZero · microsoft/LLMLingua · zilliz/GPTCache · BerriAI/litellm
· lm-sys/RouteLLM · letta-ai/letta · sgl-project/sglang · vllm-project/vllm ·
pleasedodisturb/awesome-llm-token-optimization.

Market (2026): Redis LangCache and token-optimization guidance
(redis.io/blog/llm-token-optimization-speed-up-apps) · PointFive token-optimization
and prompt-compression category guides (pointfive.co/guides) · The Token Company
(YC W26) · TokenShift · Kong AI gateway prompt-compression plugin · Cloudflare AI
Gateway docs · Cisco WAAS DRE documentation · Cloudflare Railgun retirement notes.
