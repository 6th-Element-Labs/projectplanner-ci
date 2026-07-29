# Token Optimizer — technical deep dive

Status: engineering design draft, third in the series
(TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md → market analysis → this)
Scope: the standalone product. It must be the best token optimizer in the world
on its own merits, sold to any coding-agent fleet with a base-URL change.
The Switchboard synergy is an addendum (§13), not a dependency.

**One-line contract:** For every request: identify the session by content, freeze
what's frozen, transform only the new suffix under an exact-tokenizer never-worse
gate and a cost-per-task (not tokens) objective, within a hard latency budget,
fail-open always, and record enough evidence to prove every byte of the claim.

---

## 1. Design goals and invariants

Goals, in priority order:

1. **Never worse.** No request may cost more (in dollars, after cache effects)
   or carry less decision-relevant information than the untransformed request.
2. **Never slower where it counts.** p99 added gateway latency ≤ 150 ms
   non-streaming decision time; streaming responses pass through untouched.
   Net end-to-end latency should usually be *negative*: fewer input tokens =
   less prefill time. A good optimizer is also a latency product.
3. **Zero integration.** One base-URL change. No SDK, no client library, no
   agent modification for tiers 0–3.
4. **Auditable to the byte.** Every transform is a recorded, replayable
   decision with the raw bytes recoverable.
5. **Fail-open, everywhere.** Any component failure, timeout, unknown input
   shape, or gate uncertainty ships the original request unchanged.

Invariants (violating any of these is a P0 bug, not a tradeoff):

- **I1 — Exact-count gate.** A candidate ships only if the exact per-model
  tokenizer counts it strictly smaller than the original.
- **I2 — Artifact-first.** Raw content is in the content-addressed store,
  fsynced, before any compact form of it is eligible to ship.
- **I3 — Prefix freeze.** Once a transformed turn has been sent to a provider,
  its bytes are immutable for the life of the session. Only the suffix after
  the frozen high-water mark is ever processed.
- **I4 — In-distribution output.** Compact forms are natural-language markers,
  standard JSON, unified diffs, or opaque hashes. Never invented symbol
  vocabularies.
- **I5 — Diagnostics are sacred.** Errors, warnings, failed-command output,
  exit codes, and stack-trace heads are never elided, projected, or compressed
  lossily.
- **I6 — Escape hatch.** Any transform that removes bytes from the model's view
  (tier 3+) must leave a hash-addressed path for the model to recover them.
- **I7 — Cost objective is dollars-per-task.** Token reduction that degrades
  provider cache economics is a regression; the cache simulator (§7) has veto
  power over every transform.

---

## 2. System architecture

Three planes; the data plane is stateless-restartable with all state in stores.

```
agent ──HTTPS──▶ ┌────────────────── data plane ──────────────────┐
                 │ ingress → dialect fingerprint → session resolve │
                 │   → suffix extraction → transform pipeline      │──▶ provider
                 │   → cache-economics veto → exact-count gate     │◀── (stream)
                 │   → freeze ledger append → dispatch             │
                 └──────┬──────────────┬──────────────┬────────────┘
                        ▼              ▼              ▼
                  session store   artifact store  decision log
                        ▲              ▲              ▲
                 ┌──────┴──────────────┴──────────────┴────────────┐
                 │ control plane: policy, profiles, dialect        │
                 │ registry, kill switches, cache-economics models │
                 ├─────────────────────────────────────────────────┤
                 │ evidence plane: shadow scorer, eval harness,    │
                 │ promotion state machine, savings/billing        │
                 └─────────────────────────────────────────────────┘
```

### 2.1 Request lifecycle (the whole product in twelve steps)

1. **Ingress.** Accept OpenAI (`/v1/chat/completions`, `/v1/responses`) and
   Anthropic (`/v1/messages`) wire formats, streaming and non-streaming.
   BYO credentials pass through; we never store provider keys (§10).
2. **Dialect fingerprint** (§8): identify the agent family + version from
   request shape. Unknown dialect → tier 0–1 only (observe + hygiene).
3. **Session resolution** (§3): match the request's message array against known
   sessions by hash-chain prefix. New session → create; match → load frozen
   ledger + suffix pointer.
4. **Suffix extraction.** Diff the incoming message array against the frozen
   ledger. Everything at or before the high-water mark must be byte-identical
   to the frozen form (if the agent itself rewrote history — some agents
   compact client-side — declare a *fork*, re-resolve, and demote to
   conservative profile for the session).
5. **Artifact capture.** New tool results and large content blocks in the
   suffix are chunked (FastCDC), stored content-addressed, and indexed.
6. **Transform pipeline** (§5): run the dialect profile's enabled transforms
   over the suffix only, generating candidates in parallel.
7. **Cache-economics veto** (§7): simulate the provider bill for
   original-vs-candidate including cache writes/reads/TTLs; drop any candidate
   that loses on dollars.
8. **Exact-count gate** (§4): count surviving candidates with the pinned
   per-model tokenizer; pick the argmin; if none beats the original, ship the
   original (I1).
9. **Deadline check.** Steps 5–8 run under a hard budget (default 120 ms). On
   expiry, ship the original (fail-open on time). Candidates that lose the
   race still get scored async for the evidence plane.
10. **Freeze append** (I3): the shipped suffix representation is appended to
    the session's frozen ledger with its hash chain extended.
11. **Dispatch + stream tee.** Forward to the provider; stream the response
    back byte-for-byte while teeing a copy for capture. If the response
    contains a call to one of *our* injected tools (`expand_artifact`), run
    the gateway-satisfied-tool inner loop (§6.4) before returning.
12. **Decision log.** Emit a `transform_decision` record: transforms
    considered/applied/vetoed, token counts, simulated dollars, latency spent,
    artifact hashes, dialect, profile version. This record is the billing
    substrate, the audit trail, and the evidence-plane input.

---

## 3. Session identity without session IDs

The hardest standalone problem: the wire protocols are stateless. Agents don't
send session IDs; they resend a growing message array. Everything stateful we do
(freeze, dedup, paging) depends on solving this well.

**Mechanism: a Merkle hash chain over normalized turns.**

- For each message in the array, compute `h_i = H(h_{i-1} ‖ normalize(m_i))`
  where `normalize` strips volatile fields (request IDs, timestamps in
  metadata) but not content.
- The session store indexes sessions by the chain values of their frozen
  ledger. An incoming request is matched by **longest chain-prefix**: walk the
  incoming array's chain until it diverges from every known ledger.
- Full-prefix match + longer array → session continuation (the normal case:
  agent appended turns). Divergence *before* a known high-water mark → the
  agent rewrote history (client-side compaction, edited retry) → **fork**:
  open a new session seeded from the common prefix, conservative profile.
- Chains are cheap: O(new turns) per request since prefix chain values are
  cached; matching is a hash-table lookup on the last-known chain head, with
  fallback binary search over the chain only on miss.
- Two agents replaying identical transcripts (CI, evals) would collide;
  tenancy + API-key scoping partitions the space, and collisions within a
  tenant are harmless — identical transcripts genuinely share frozen state.

This also yields the **high-water mark** for free (the deepest chain value we
have frozen) and makes I3 mechanically checkable on every request.

---

## 4. Tokenizer infrastructure — the gate must be exact

- **Registry of pinned tokenizers** per (provider, model, version): tiktoken
  variants, HF tokenizers for open models, Anthropic's tokenizer via its
  count-tokens API for ground truth. Every count is attributed to a tokenizer
  build hash in the decision log.
- **Drift detection.** Continuously sample real payloads, compare local counts
  against provider-reported usage in responses (`usage.prompt_tokens`).
  Sustained divergence beyond tolerance auto-suspends enforce mode for that
  model (kill switch) and pages us. Providers change tokenization quietly;
  treating the response `usage` block as the oracle-of-record catches it.
- **Incremental counting.** Token counts are memoized per artifact chunk and
  per frozen turn; a request's count is assembled from cached spans + the new
  suffix. Cost per request is O(suffix), not O(context). This — plus §3 — is
  why we can afford exactness where competitors approximate: a stateless
  compression API must re-tokenize the whole prompt every call; we never do.
- **Chat-format overhead models** per dialect (message framing, role tokens,
  tool-schema serialization) validated against the same `usage` oracle, so
  counts are exact at the *request* level, not just the string level.

---

## 5. The transform catalog (what runs, exactly)

Ordered by tier; each transform declares: inputs, algorithm, gate conditions,
and its failure mode (which must be "original ships").

### Tier 1 — Hygiene (content-preserving)

- **ANSI/OSC strip**: remove SGR styling; preserve OSC-8 hyperlink text+URL.
- **Line-ending normalization** when it counts smaller.
- **JSON minify** of tool results that are valid JSON (whitespace only —
  never key reordering; key order can carry meaning and breaks byte determinism).
- **Trailing-noise trim**: spinner frames, carriage-return progress overwrites
  (keep final frame only — the intermediate frames were never visible to a
  human either).

### Tier 2 — Lossless (representation-equivalent)

- **Line RLE** (`line-rle-v1`, from CodexZero): collapse consecutive identical
  complete lines to `line [repeated N times]` (prose marker, I4) or the JSON
  runs form, whichever counts smaller.
- **Exact-duplicate references.** Key simplification vs CodexZero, worth
  stating precisely: CodexZero needed *state proofs* (file hashes, git
  fingerprints) because it decided whether re-execution could be skipped. The
  gateway never skips execution — the command already ran; both outputs
  already exist. We are deduplicating the *representation* of an output that
  is byte-identical to one already in frozen context. Content identity is the
  entire proof. The reference form is
  `{"same_output_as": "<turn/tool_use_id>", "sha256": …, "note": "byte-identical output already shown above"}`
  and it is valid iff the referenced turn is inside the frozen ledger (I3
  guarantees it's still what the model saw) and the count is strictly smaller.
- **Sub-result chunk dedup.** FastCDC (target 2 KB, min 512 B, max 8 KB) over
  large tool results; chunks already present verbatim in frozen context can be
  referenced by quoting their first/last lines + `[unchanged from above]`
  markers. Conservative gate: only fires on ≥ 70% chunk overlap, otherwise
  the framing overhead loses — the exact-count gate enforces this naturally.
- **Tool-schema dedup** (dialect-scoped): when a dialect resends identical
  schemas every request, the schemas become part of the frozen prefix by
  construction (I3) — the win here is cache shaping (§7), not rewriting.

### Tier 3 — Reversible (bytes leave the view, stay reachable)

- **Delta re-reads.** A re-read of a file whose earlier version is in frozen
  context ships as a unified diff against that version:
  `File re-read; unified diff vs the copy shown at <ref> (full content: expand_artifact <hash>)`.
  Gate: diff must count < 60% of full content (diffs are token-dense);
  binary/high-churn files excluded.
- **Stale-output eviction (context paging).** Residency policy scores every
  tool result in context: recency, size, supersession (a newer run of the
  same command family), and reference count (does later conversation mention
  its content? cheap n-gram overlap check). Evict = replace with
  `[output evicted: <one-line synopsis>; expand_artifact <hash> to restore]`.
  Constraints: never evict from the last K turns (default 6); never evict
  diagnostics (I5); eviction happens only at suffix-processing time for turns
  not yet frozen — frozen turns are immutable (I3), so paging decisions are
  made once, at the freeze boundary, not retroactively. This preserves prefix
  stability *and* still wins because agents' contexts are append-only.
- **Successful-check projection** (CodexZero's): success-only, recognized
  check commands, ≥ 80 lines → head + diagnostic lines + tail, with the
  artifact hash attached.

### Tier 4 — Behavioral (opt-in, eval-gated)

- **Injected efficiency instructions** (dialect-aware placement): terse
  commentary, no-repeat-verification guidance. Injected as a stable block so
  it freezes into the cacheable prefix.
- **LLMLingua-2-style compression of our own injected text** (synopses,
  eviction stubs) — never of user or tool content.
- **Output shaping**: `max_tokens` discipline per task class where the
  dialect's response patterns make it safe.

### Explicit non-transforms

No paraphrasing of user messages. No key reordering. No summarizing code. No
touching anything inside failed-command output. No semantic-similarity response
reuse for coding traffic (correctness cliff; see research doc §3.3).

---

## 6. Injected tooling and the inner loop

### 6.1 `expand_artifact`

Tier 3 requires the escape hatch (I6). We inject one tool:

```json
{"name": "expand_artifact",
 "description": "Recover the full original bytes of content shown elided/evicted, by its sha256.",
 "input_schema": {"type": "object", "properties": {"sha256": {"type": "string"},
   "byte_range": {"type": "string"}}, "required": ["sha256"]}}
```

### 6.4 Gateway-satisfied tools (the inner loop)

The agent, not the gateway, executes tools — so a model call to
`expand_artifact` would leak to an agent that doesn't implement it. Therefore:
when a provider response contains a tool_use for a gateway-injected tool, the
gateway does **not** return it to the agent. It satisfies the call from the
artifact store, appends the tool result, re-issues the request to the provider,
and loops (bounded: 3 inner iterations, 256 KB total expansion) until the
response contains no gateway-owned calls; only then does it respond to the
agent. The agent never knows the tool exists. Latency cost is one extra provider
round-trip per fault — the virtual-memory page-fault cost, paid only when the
eviction policy guessed wrong, and logged as a policy-quality signal (fault
rate is the eviction tuner's loss function).

Streaming interacts here: we hold back only the *final* event frames needed to
detect gateway-owned tool_use; ordinary content streams through unbuffered.

---

## 7. The cache-economics engine (I7's enforcement arm)

Per-provider models, versioned in the control plane:

- Anthropic: explicit `cache_control` breakpoints; write = 1.25x input, read =
  0.1x; 5-minute base TTL (1-hour variant at higher write cost); up to 4
  breakpoints.
- OpenAI: automatic prefix caching ≥ 1,024 tokens, 50% read discount,
  observed TTL bands.
- Self-hosted (vLLM/SGLang): radix-style prefix reuse — no discount to model,
  but real latency/throughput gains; the simulator optimizes hit length.

Functions:

1. **Breakpoint placement.** For Anthropic dialects, place/normalize
   `cache_control` markers at the stable boundaries our freeze ledger already
   defines (end of tools+system, end of frozen conversation). Agents that
   place none get the full discount for free; agents that place them badly get
   corrected placement (this alone — see the ProjectDiscovery 7%→84% case —
   can halve a bill and is pure tier-2 safety).
2. **Veto simulation.** For every candidate: simulate this request *and* the
   projected next N requests (sessions are append-only, so the current suffix
   becomes the next request's cached prefix) under original vs candidate.
   Candidate must win on cumulative simulated dollars, not just this request's
   tokens. This is where "smaller but cache-busting" candidates die.
3. **TTL-aware session pacing signals** (advisory telemetry): sessions that
   idle past cache TTL and re-pay full write costs are surfaced in the savings
   report — the customer-visible version of "your agent's think time is
   costing you cache misses."

---

## 8. The agent dialect registry

A dialect = fingerprint + parser + transform profile + placement rules.

- **Fingerprinting**: hash of the system-prompt head, tool-schema signature
  set (names + shapes), header/user-agent hints, message-structure motifs
  (e.g., Claude Code's system-reminder blocks, Codex's exec framing). Scored
  match; ambiguity → generic-conservative dialect.
- **Parser**: locates the seams — where tool results live, what framing wraps
  command output, where injection is safe (tier 4), what must never be touched
  (each dialect's own cache breakpoints, encrypted reasoning blocks,
  provider-specific fields — passed through byte-exact).
- **Profile**: enabled transforms + parameters + eval status per model
  (§9's promotion state machine output).
- **Versioned like AO packs** (the WAAS playbook): harness releases change
  transcript shapes; fingerprints carry version ranges; an unrecognized new
  version of a known dialect auto-demotes to conservative until re-validated.
  Target cadence: new agent release → updated dialect entry within days,
  validated by replay.

The registry is a growing, empirical, fleet-tested asset — the standalone
product's deepest defensibility (see §12).

---

## 9. The evidence plane

- **`transform_decision` log** (open schema): per request — transforms
  considered/applied/vetoed and why, exact counts, simulated dollars, latency,
  artifact hashes, tokenizer build, dialect+profile versions. Append-only;
  drives billing, audit, and learning.
- **Shadow scoring**: disabled/losing candidates are still generated (async,
  off the latency path) and scored, so we know the value of every technique on
  every workload *before* anyone enables it. Shadow mode is not a trial phase;
  it never turns off.
- **Eval harness**: (a) deterministic fixture replay (CodexZero-style captured
  payload suites per dialect) run in CI on every profile change; (b) paired
  task-level A/B — same task classes, optimizer on/off — scored on task
  success, cost-per-task, latency, and expand-fault rate. Standalone customers
  get this via opt-in A/B cohorts; task success proxies from observable
  signals (session completed vs abandoned, error-loop rates, tokens-to-
  completion) — imperfect but honest, and stated as such.
  (The addendum's Switchboard integration upgrades this to true verified
  outcomes; the standalone product is designed so that better ground truth
  plugs in, never assumed.)
- **Promotion state machine** per (transform, dialect, model):
  `candidate → shadow → canary(1%) → default-on → suspended`, with automatic
  demotion on eval regression, expand-fault spikes, or tokenizer drift, and
  automatic re-validation triggered by model releases and harness releases.
  Kill switches at every level, per tenant, per technique, effective within
  seconds (control-plane push, data-plane local cache with short TTL).

---

## 10. Deployment, tenancy, and trust mechanics

- **Data plane**: stateless pods; session store (Redis-class, session ledgers
  + chain indexes + memoized counts, TTL = session idle window); artifact
  store (object storage, CAS layout `sha256/ab/cd/<hash>`, per-tenant
  encryption keys); decision log (append-only stream → warehouse).
- **Keys**: provider keys pass through per request (`Authorization`
  forwarded); never persisted. Optional key-vault mode for teams that want
  server-held keys is separate and explicit.
- **Zero-retention mode**: artifacts encrypted with a tenant-held key
  (envelope encryption; we store ciphertext, tenant holds KEK) or artifact
  store disabled entirely (tier 3 then auto-disables — the tiers degrade
  gracefully and visibly).
- **Regional pinning**; **self-host edition** = the same single binary + Redis
  + object store, feature-flagged, phoning home only evidence-plane summaries
  the tenant approves (or nothing). Deployment option, not a fork (the WAAS
  appliance lesson: one codebase).
- **Latency engineering**: candidates race in parallel under the 120 ms
  budget; counts are incremental (§4); the common-case added latency target is
  p50 ≤ 25 ms, p99 ≤ 150 ms — against which the prefill saving from a
  smaller prompt is typically larger, making the optimizer net latency-
  *negative* on long sessions. We publish this distribution live, per tenant.

---

## 11. SLOs and failure modes

| Failure | Behavior |
|---|---|
| Any pipeline component errors/times out | Ship original (fail-open), log, alert |
| Session store unavailable | Tier 0–1 only (stateless transforms), degrade banner in dashboard |
| Artifact store write fails | Transform ineligible (I2), original ships |
| Tokenizer drift detected | Auto-suspend enforce for that model |
| Agent rewrote frozen history (fork) | New session, conservative profile |
| expand-fault storm (bad eviction) | Auto-demote paging for that dialect |
| Provider format change | Dialect auto-demotes to passthrough; alert |

SLOs: 99.95% data-plane availability with fail-open passthrough beneath it
(availability of *optimization* may degrade; availability of *the path* may
not); zero tolerance for I1–I7 violations (each is monitored as an invariant
check on sampled traffic, not assumed).

---

## 12. Why this is the world's best token optimizer, standalone

Each claim names the mechanism that competitors lack, not adjectives:

1. **It is the only session-stateful optimizer.** The Merkle-chain session
   model (§3) turns a stateless wire protocol into incremental computation:
   O(suffix) work, memoized exact counts, frozen prefixes. Stateless
   compression APIs re-process the whole prompt every call — which forces them
   to approximate, breaks provider caches, and caps them at per-request
   tricks. Statefulness is the unlock for every high-value transform (dedup,
   deltas, paging, cache shaping), and it is an architecture, not a feature
   a stateless competitor can patch in.
2. **It optimizes dollars, not tokens.** The cache-economics veto (§7) is the
   difference between a demo and a product: "20x compression" that busts a
   90%-discounted prefix is a bill increase. To our knowledge no shipping
   optimizer simulates provider cache economics per candidate; the
   ProjectDiscovery case shows cache shaping alone can halve real spend.
3. **Never-worse is enforced by construction, not claimed.** I1–I7 with
   fail-open on error *and on time*, exact tokenizers with drift detection
   against the provider's own usage oracle, artifacts before transforms,
   invariant monitors on live traffic. This is what lets a risk-averse
   platform team turn it on: the failure mode of every component is "you got
   exactly what you would have gotten without us."
4. **It is agent-aware where everyone else is generic.** The dialect registry
   (§8) is the WAAS application-optimizer playbook applied to agents:
   fingerprints, parsers, per-dialect profiles, days-not-months response to
   harness releases. Generic gateways cannot touch transcript interiors
   safely *because* they lack this layer.
5. **It has a memory hierarchy, not just a compressor.** Context paging with
   gateway-satisfied faults (§6) means savings scale with session length —
   exactly where agent spend explodes (400k–2M tokens/task) — while the
   fault loop bounds the worst case to one extra round trip.
6. **Its claims are evidence, not marketing.** Open decision schema,
   always-on shadow scoring, promotion state machine, published latency and
   savings distributions, per-request audit. The customer can check every
   number we bill against.

The compounding loop that keeps it best: every request generates shadow
evidence → profiles improve per dialect × model → savings and safety records
improve → more traffic → better evidence. Competitors would need not our code
(most techniques are published) but our accumulated evidence and dialect
corpus — which only time in the request path produces.

---

## 13. Addendum — Switchboard + optimizer: 2 + 2 = 5

Everything above stands alone. Switchboard adds the two things a standalone
optimizer structurally lacks, and the optimizer repays Switchboard in kind:

1. **True outcome ground truth.** Standalone eval uses proxies (§9).
   Switchboard has completion gates, CI receipts, review verdicts — the
   promotion state machine upgrades from "no proxy regression" to "no
   *verified-outcome* regression," the strongest safety claim in the
   category, and one no competitor without an orchestration layer can make.
2. **The second lever.** The gateway can't touch personal-CLI lanes (auth must
   not proxy) or eliminate whole turns. Switchboard's runtime adapters can:
   lean prompts, terminal hygiene env, schema deferral, batched checks,
   event-driven waits. Fleet A/B across both levers, attributed in Tally.
3. **Routing × compression as one decision.** The per-wake `routing_decision`
   (MODEL-CATALOG-ROUTING) selects tier + effort; the optimizer contributes
   the compression profile and the cache-state input ("this session has a hot
   1-hour Anthropic prefix" is a routing feature — switching models throws the
   cache away). Jointly they optimize cost-per-verified-outcome; separately
   each sub-optimizes.
4. **Dogfood → dataset → distribution.** The fleet is first customer,
   evidence corpus, and reference sale. The seam stays clean (attribution via
   headers, empty = standalone; §10's self-host edition doubles as the
   Switchboard-bundled deployment), so the standalone door and the platform
   door sell independently — one flywheel, two products, and the flywheel is
   the part that compounds.

2+2=5, precisely: Switchboard makes the optimizer's *evidence* categorically
better (verified outcomes), and the optimizer makes Switchboard's *economics*
categorically better (every fleet task cheaper, measured in the same ledger
that proves it). Neither claim is available to either product alone.
