# Token Optimization as a Service — research & scoping

Status: research draft for the token-gateway scope (sibling of MODEL-CATALOG-ROUTING)
Depends on: LiteLLM API-only boundary, Tally cost-to-outcome attribution, `routing_decision` records

**One-line contract:** Sit in the model-API path of any coding agent, remove provably
redundant tokens under an exact-tokenizer never-worse gate, keep raw bytes recoverable
by hash, and continuously prove — against verified task outcomes — which optimizations
are safe on which models.

The thesis in one sentence: WAN optimization for the token economy, delivered the way
Cloudflare delivered WAN/edge services (SaaS via a one-line config change) instead of
the way Cisco/Riverbed delivered them (boxes) — where the durable asset is not the
proxy but the evidence flywheel that proves savings never cost outcomes.

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
provider prompt caching (90% input discount on Anthropic, 50% on OpenAI). Any
transform that is not deterministic and prefix-stable can *increase* the bill while
"saving tokens." This is the token-gateway version of the WAAS rule that DRE must not
defeat downstream QoS or application caching.

---

## 2. Techniques from classical CS and networking

### 2.1 Data redundancy elimination → cross-turn dedup

WAAS DRE and Riverbed SDR kept synchronized dictionaries of previously-seen byte
segments at both ends of a WAN link and replaced repeats with short references.
The token gateway has it *easier*: both "ends" are us (we see every request in the
session), and the reference format can be plain JSON the model already understands.
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
the evidence layer: every raw output is stored by SHA-256 *before* any compact
candidate is chosen (fail-closed on hash mismatch), every compact form carries the
raw hash, and an injected `expand_artifact` tool lets the model recover exact bytes
on demand. This is what makes tier-3 "lossy" transforms actually reversible-in-
reachability: lossy in presentation, lossless in access. ccache and Bazel's remote
cache also prove the operational model: content-addressed caches can be shared
fleet-wide safely because the address *is* the proof of identity.

### 2.5 Virtual memory → context paging

Denning's working-set model maps directly: an agent session's context is a memory
hierarchy where "resident" = in the prompt (expensive per turn) and "swapped" = in
the artifact store (free until faulted). MemGPT (arXiv:2310.08560, now Letta) proved
LLMs can drive their own paging via tools. The gateway version needs no agent
cooperation: evict cold, superseded tool outputs (an old test log after a newer run
exists) to a stub with hash + one-line summary, and let the model fault it back.
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
exactly the mental model for **why prefix stability is law** (§2.6): every byte we
keep stable is a byte the provider serves at 10–50% price. For customers running
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

Output tokens cost 3–5x input. Levers: suppress reasoning summaries where the
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
| openai/tiktoken, huggingface/tokenizers, anthropic token-counting APIs | Exact counting | The gate. Approximate counting breaks the never-worse guarantee |
| pleasedodisturb/awesome-llm-token-optimization | Curated field map | Ongoing scan for new techniques |

2026 commercial landscape check (validation, not moat): The Token Company (YC W26)
sells compression-as-API; TokenShift does endpoint-local compression for coding
agents; Kong ships a prompt-compression plugin; Redis ships LangCache. The category
is real and forming; nobody yet combines agent-aware transforms + outcome-verified
evidence + savings-share billing.

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
- **Transparent interception** (WCCP/inline) meant no client changes. Our
  equivalent: base-URL override, which every agent already supports.
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
  burns trust fast. Our tier ladder (observe → lossless → reversible → behavioral)
  is the same ladder with the CodexZero gate making tiers 2–3 provable.
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

- **Data plane** — the proxy: OpenAI-/Anthropic-compatible endpoints, streaming
  pass-through, per-model exact tokenizers, transform pipeline, artifact store,
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
| 1 Hygiene | ANSI/pager strip at source (env), whitespace/JSON compaction | Byte-lossless or presentation-only | On |
| 2 Lossless | RLE, exact dedup w/ state proofs, schema dedup, cache-aware shaping | Strictly-smaller gate; raw retained; model-equivalent content | On |
| 3 Reversible | Stale-output eviction, diagnostic projections, delta-encoded re-reads | Info recoverable via `expand_artifact`; success-only projections | Opt-out |
| 4 Behavioral | Lean prompts, terse commentary, LLMLingua on injected text, summarization | Eval-gated per model; paired outcome evidence | Opt-in |
| 5 Routing | Tier + compression profile per wake (sibling scope join) | Explainable `routing_decision` | Opt-in |

### 6.3 Applicability matrix (summary)

| Technique | Locus | Needs agent hooks | Est. savings share |
|---|---|---|---|
| Exact dedup + state proofs | Gateway | No | High (tool-heavy sessions) |
| RLE / hygiene | Gateway | No (better with env injection) | Medium |
| Stale-output paging | Gateway | No (tool injection) | High on long sessions |
| Delta re-reads | Gateway | No | Medium |
| Prefix-stability / cache shaping | Gateway | No | High (bill, not tokens) |
| Lean prompt / schema minimization | Harness/launcher | Yes | Medium; big on cold start |
| Turn elimination (event-wait, batching) | Harness/launcher | Yes | High per avoided wake |
| Output-side (terse, summaries off) | Harness/launcher | Yes | Medium (3–5x-priced tokens) |
| Hard compression (LLMLingua) | Gateway | No | Small, risky; own-text only |
| Semantic caching | Gateway | No | Small for coding; restricted |

Note the split: the biggest gateway-only wins are dedup, paging, and cache shaping;
the biggest total wins add harness cooperation. Switchboard uniquely holds both
levers (LiteLLM lane + runtime adapters/launch env), including for personal-CLI
lanes that the gateway must never proxy (per MODEL-CATALOG-ROUTING: personal CLI
auth never goes through the gateway).

### 6.4 The constitution (non-negotiables, from CodexZero + Rocket Loader's grave)

1. Candidate vs original decided by the **exact production tokenizer**; ship only
   if strictly smaller. Fail open to the original.
2. Raw bytes stored content-addressed **before** any transform is eligible;
   fail closed on hash mismatch.
3. **Prefix stability is law**: transformed history is frozen; only the suffix is
   processed. Objective is cost-per-task, not tokens.
4. Never invent symbol vocabularies; compact forms must be in-distribution
   (natural-language markers, standard JSON, hashes as opaque IDs).
5. Failed commands, errors, warnings, exit codes are never projected or elided.
6. Every tier-3+ transform carries an escape hatch (`expand_artifact`).
7. Every enforce-mode technique has shadow-mode history and paired outcome
   evidence on the target model before promotion; model releases trigger
   re-validation.
8. BYO keys; zero-retention mode; transforms and savings are auditable per
   request (the `routing_decision` discipline extended to `transform_decision`).

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
- **Structural defense:** OpenRouter can't copy savings-share (it inverts their
  take-rate model); providers won't optimize cross-vendor; frameworks aren't in
  the request path; infra incumbents ship the generic slice (LangCache, Kong
  plugin) but not agent-aware transforms + outcome-verified evidence.

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
4. **Quality liability.** Tier discipline + constitution; contractual scope: tiers
   0–2 guaranteed-equivalent, 3+ evidence-gated opt-in.
5. **Privacy/state.** We hold customer code artifacts. Zero-retention mode,
   regional pinning, self-host option (as deployment, not product).
6. **Tokenizer drift.** Exact counting per model version is load-bearing;
   re-validate on every provider release.
7. **How big is the prize on real fleets?** Unknown until shadow mode runs.
   CodexZero's repeated benchmark says ~15% on Codex/terminal-bench; tool-heavy
   long sessions should be higher (dedup + paging), short sessions lower. This is
   the P0 go/no-go question.

---

## 9. Scoping (phases)

- **P0 — Shadow (in Switchboard's gateway).** LiteLLM callbacks: per-request token
  telemetry, would-have-saved counters for dedup/RLE/hygiene, Tally attribution,
  savings dashboard. Go/no-go on measured prize. No behavior change.
- **P1 — Lossless enforce (API lanes).** Tier 1–2 transforms behind the gate;
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
