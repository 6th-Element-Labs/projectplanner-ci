# Token Optimizer — technical deep dive

Status: reviewed engineering-design baseline, third in the series; product design,
not accepted Switchboard architecture
(TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md → market analysis → this)
Scope: the standalone product. It must deliver independently measurable value
on certified coding-agent lanes, without requiring Switchboard.
The Switchboard synergy is an addendum (§13), not a dependency.

**One-line contract:** For every certified request lane: resolve session state without
cross-session ambiguity, freeze prior gateway output, evaluate only eligible suffix
transforms against model-specific count and cache economics, pass through unchanged
when optimization is uncertain, fail closed on security policy, and record the
evidence needed to reproduce the decision.

---

## 1. Design goals and invariants

Goals, in priority order:

1. **Bounded claims.** Deterministic gates can prove byte identity, source
   recoverability, token counts, and projected cache economics. They cannot prove
   that a different model-visible representation preserves all decision-relevant
   information; that requires non-inferiority evidence at the task-outcome level.
2. **Never slower where it counts.** p99 added gateway latency ≤ 150 ms
   non-streaming decision time; streaming responses pass through untouched.
   Net end-to-end latency should usually be *negative*: fewer input tokens =
   less prefill time. A good optimizer is also a latency product.
3. **Minimal integration.** One base-URL change for certified API lanes. Personal
   subscriptions, closed Cursor features, artifact expansion, and some harness
   improvements require explicit client or launcher integration.
4. **Auditable to the permitted boundary.** Every transform is a recorded,
   replayable decision. Model-visible removals are byte-recoverable when retention
   is enabled; transforms requiring recovery are disabled in zero-retention mode.
5. **Optimization fails open; authority fails closed.** Transform failure, timeout,
   unknown shape, or uncertain economics ships the original request unchanged.
   Authentication, tenant isolation, retention, DLP, egress, and budget policy
   failures reject the request; passthrough must never bypass them.

Invariants (violating any of these is a P0 bug, not a tradeoff):

- **I1 — Authoritative-count gate.** A candidate ships only if the most
  authoritative supported count for the certified model says it is strictly
  smaller and the cache-economics model does not project higher billed cost.
- **I2 — Recovery-before-reference.** When policy permits retention, raw content
  is durably stored before a recoverable compact form is eligible. In
  zero-retention mode, transforms requiring later recovery are disabled.
- **I3 — Dual-ledger prefix freeze.** Preserve a canonical client-view ledger
  containing the raw history the agent resends and a provider-view ledger
  containing the exact representation previously shown to the model. Match new
  requests against the client-view prefix, replay the frozen provider-view prefix
  byte-for-byte, and transform only the newly appended raw suffix.
- **I4 — In-distribution output.** Compact forms are natural-language markers,
  standard JSON, unified diffs, or opaque hashes. Never invented symbol
  vocabularies.
- **I5 — Diagnostics are sacred.** Errors, warnings, failed-command output,
  exit codes, and stack-trace heads are never elided, projected, or compressed
  lossily.
- **I6 — Certified escape hatch.** Any transform that removes bytes from the
  model's view must leave a tenant/session-scoped capability the active
  agent/model can demonstrably invoke; a bare content hash is not authorization.
- **I7 — Cost objective is dollars-per-task.** Token reduction that degrades
  provider cache economics is a regression; the cache simulator (§7) has veto
  power over every transform.

---

## 2. System architecture

Three internal optimizer components; the data plane is stateless-restartable with
all state in stores. These labels do not create lifecycle authority and are distinct
from Switchboard's capacity, communication, and coordination planes in ADR-0008.

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

1. **Ingress.** Accept certified OpenAI (`/v1/responses`, and Chat Completions
   only for clients that use it) and Anthropic (`/v1/messages`) wire formats,
   streaming and non-streaming. The client authenticates to the gateway; an
   upstream credential lease is resolved server-side and never returned to the
   client (§10).
2. **Dialect fingerprint** (§8): identify the agent family + version from
   request shape. Unknown dialect → tier 0–1 only (observe + hygiene).
3. **Session resolution** (§3): prefer a certified declared session ID; otherwise
   match the incoming raw message array against known client-view hash-chain
   prefixes. New session → create; match → load both ledgers and suffix pointer.
4. **Suffix extraction.** Verify the incoming raw prefix against the frozen
   client-view ledger. Replay the corresponding provider-view prefix exactly and
   isolate only the newly appended raw suffix. If the agent rewrote its own
   history, declare a new context epoch or fork and demote it to a conservative
   profile; never compare raw client history with transformed provider bytes.
5. **Artifact eligibility.** Under a retention mode that permits it, new tool
   results and large suffix blocks may be chunked, encrypted, stored, and indexed.
   Otherwise recovery-dependent candidates are disabled before generation.
6. **Transform pipeline** (§5): run the dialect profile's enabled transforms
   over the suffix only, generating candidates in parallel.
7. **Cache-economics veto** (§7): simulate the provider bill for
   original-vs-candidate including cache writes/reads/TTLs; drop any candidate
   that loses on dollars.
8. **Count gate** (§4): use the certified counting method; if no candidate is
   strictly smaller than the original, ship the original (I1). This establishes
   mechanical eligibility, not outcome safety.
9. **Deadline check.** Steps 5–8 run under a hard budget (default 120 ms). On
   expiry, ship the original (fail-open on time). Candidates that lose the
   race still get scored async for the evidence plane.
10. **Freeze append** (I3): append the accepted raw suffix to the client-view
    ledger and the shipped suffix representation to the provider-view ledger;
    extend both hash chains atomically.
11. **Dispatch + response contract.** Ordinary certified passthrough profiles
    stream provider events without semantic rewriting while teeing permitted
    telemetry. Profiles with gateway-owned tools either expose the tool to the
    harness or buffer the response for the bounded inner loop (§6.2); they cannot
    simultaneously claim transparent streaming.
12. **Decision log.** Emit a `transform_decision` record: transforms
    considered/applied/vetoed, input and output token effects, cache writes/reads,
    gross and net dollars, latency, retries, expansion faults, artifact hashes,
    dialect, codec/profile versions, and joined outcome evidence. This record is
    the billing substrate, the audit trail, and the evidence-plane input.

---

## 3. Session identity: declared first, inferred second

Some lanes expose stable identity while others only resend a growing message array.
For example, current Claude Code gateway traffic declares session, agent, and
parent-agent IDs. Everything stateful we do depends on using declared identity when
certified and treating inference as a conservative fallback.

**Mechanism: prefer declared session identity; use a scoped hash chain only as a
conservative fallback.**

- Where a certified client supplies a stable conversation, response, launch, or
  gateway-session identifier—including Claude Code's session/agent headers—bind it
  to tenant, principal, auth lane, agent version, and model lane. Never discard
  declared identity in favor of inference.
- Otherwise, for each message in the array, compute
  `h_i = H(h_{i-1} ‖ normalize(m_i))`
  where `normalize` strips volatile fields (request IDs, timestamps in
  metadata) but not content.
- The session store indexes fallback sessions by the chain values of their raw
  client-view ledger. An incoming request is matched by **longest chain-prefix**:
  walk the incoming raw array's chain until it diverges from every known ledger.
- Full-prefix match + longer array → session continuation (the normal case:
  agent appended turns). Divergence *before* a known high-water mark → the
  agent rewrote history (client-side compaction, edited retry) → **fork**:
  open a new session seeded from the common prefix, conservative profile.
- Chains are cheap: O(new turns) per request since prefix chain values are
  cached; matching is a hash-table lookup on the last-known chain head, with
  fallback binary search over the chain only on miss.
- Two agents can replay identical prefixes and then diverge. Tenancy alone does not
  make that collision harmless. Partition fallback matching by tenant, principal,
  launch/connection evidence where available, auth lane, and dialect. If multiple
  live sessions remain plausible, create a new session and use the conservative
  profile; never guess or share artifact capabilities across the candidates.

This yields a client-view **high-water mark** and a paired provider-view replay
boundary, making I3 mechanically checkable on every request.

---

## 4. Counting infrastructure — authoritative, versioned, and reconciled

- **Registry of pinned counting methods** per (provider, model, version):
  provider-authoritative count endpoints where available, pinned local tokenizers
  for supported open encodings, and explicitly labeled estimates elsewhere. Every
  count is attributed to a method and version in the decision log.
- **Drift detection.** Continuously sample real payloads, compare local counts
  against provider-reported usage in responses (`usage.prompt_tokens`).
  Sustained divergence beyond tolerance auto-suspends enforce mode for that
  model (kill switch) and pages us. Providers change tokenization quietly;
  treating the response `usage` block as the oracle-of-record catches it.
- **Incremental local counting.** Where a certified local tokenizer exists, counts
  are memoized per artifact chunk and
  per frozen turn; a request's count is assembled from cached spans + the new
  suffix. Cost per request is O(suffix), not O(context). This — plus §3 — is
  why local counting can remain cheap. Provider-side counts may still require a
  network call and full canonical payload; the latency budget must account for it.
- **Chat-format overhead models** per dialect (message framing, role tokens,
  tool-schema serialization) validated against the same `usage` oracle, so
  local estimates are calibrated at the *request* level, not just the string level.
  Provider-reported usage remains billing truth.

---

## 5. The transform catalog (what runs, exactly)

Ordered by tier; each transform declares: inputs, algorithm, gate conditions,
and its failure mode (which must be "original ships").

### Tier 1 — Hygiene (content-preserving)

- **ANSI/OSC strip**: remove SGR styling; preserve OSC-8 hyperlink text+URL.
- **Line-ending normalization** only for fields whose dialect contract declares
  line endings presentation-only; never for source code, patches, signatures, or
  opaque payloads.
- **JSON minify** of tool results that are valid JSON (whitespace only —
  never key reordering; key order can carry meaning and breaks byte determinism).
- **Trailing-noise trim**: spinner frames, carriage-return progress overwrites
  (keep final frame only — the intermediate frames were never visible to a
  human either).

### Tier 2 — Exact-source references (model-visible representation changes)

- **Line RLE** (`line-rle-v1`, from CodexZero): collapse consecutive identical
  complete lines to `line [repeated N times]` (prose marker, I4) or the JSON
  runs form, whichever counts smaller. The source run is exactly recoverable,
  but the model-visible text is not byte-equivalent and still needs outcome evidence.
- **Exact-duplicate references.** Key simplification vs CodexZero, worth
  stating precisely: CodexZero needed *state proofs* (file hashes, git
  fingerprints) because it decided whether re-execution could be skipped. The
  gateway never skips execution — the command already ran; both outputs
  already exist. We are deduplicating the *representation* of an output that
  is byte-identical to one already in context. Content identity is necessary but
  not sufficient: the exact source bytes must also be marked `model_visible` in
  the replayed provider-view ledger. The reference form is
  `{"same_output_as": "<turn/tool_use_id>", "sha256": …, "note": "byte-identical output already shown above"}`
  and it is valid iff the referenced span is still visible in the frozen
  provider-view ledger and the count is strictly smaller.
- **Sub-result chunk dedup.** FastCDC (target 2 KB, min 512 B, max 8 KB) over
  large tool results; chunks already present verbatim in frozen context can be
  referenced by quoting their first/last lines + `[unchanged from above]`
  markers. Conservative gate: only fires on ≥ 70% chunk overlap, otherwise
  the framing overhead loses — the exact-count gate enforces this naturally.
- **Tool-schema dedup** (dialect-scoped): when a dialect resends identical
  schemas every request, the schemas become part of the frozen prefix by
  construction (I3) — the win here is cache shaping (§7), not rewriting.
- **Parallel-output overlap dedup.** Within a fan-out turn, detect exact repeated
  rows, files, or JSON subtrees across sibling tool results. Emit one canonical
  model-visible span plus self-describing sibling references only after the
  provider-view visibility and strict-count gates pass.
- **Structured-data codecs.** For certified homogeneous JSON arrays or tables,
  deterministically encode a schema plus ordered rows/columns. The codec is
  versioned, self-describing, preserves ordering and scalar types, and must beat
  ordinary JSON minification. This changes model-visible representation and
  therefore requires outcome evidence even when decoding is exact.

### Tier 3 — Reversible (bytes leave the view, stay reachable)

- **Delta re-reads.** A re-read of a file whose earlier version is in frozen
  context ships as a unified diff against that version:
  `File re-read; unified diff vs the copy shown at <ref> (full content: expand_artifact <hash>)`.
  Gate: diff must count < 60% of full content (diffs are token-dense);
  binary/high-churn files excluded.
- **Context projection and paging.** A transparent continuation cannot
  retroactively evict a stale provider-visible turn without breaking prefix
  stability. Projection is therefore eligible only (a) before content is first
  shown to the provider, (b) through a cooperative agent/harness or provider-native
  compaction event that opens a new context epoch, or (c) with an explicit cache
  reset accepted by policy. Residency scoring may recommend candidates in observe
  mode, but it cannot silently rewrite a frozen provider-view prefix.
- **Successful-check projection** (CodexZero's): success-only, recognized
  check commands, ≥ 80 lines → head + diagnostic lines + tail, with the
  artifact hash attached.
- **Command-aware codecs.** A versioned registry parses recognized `git`, test,
  compiler, package-manager, search, and linter outputs into compact typed views.
  Each codec declares required fields such as command, exit status, failures,
  filenames, line numbers, and diagnostic head/tail. Unknown versions, parser
  ambiguity, failed safety checks, or non-improving counts ship the original.
- **Vision budgets.** For certified image-bearing tool results, bound dimensions,
  tile count, metadata, and optional color conversion under a visual-task
  non-inferiority profile. Report image-token economics separately from text.

### Tier 4 — Behavioral (opt-in, eval-gated)

- **Injected efficiency instructions** (dialect-aware placement): terse
  commentary, no-repeat-verification guidance. Injected as a stable block so
  it freezes into the cacheable prefix.
- **LLMLingua-2-style compression of our own injected text** (synopses,
  eviction stubs) — never of user or tool content.
- **Output shaping**: `max_tokens` discipline per task class where the
  dialect's response patterns make it safe. Attribute response-token savings
  separately from input/context savings.

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

### 6.2 Gateway-satisfied tools (the inner loop)

The agent, not the gateway, executes tools — so a model call to
`expand_artifact` would leak to an agent that doesn't implement it. Therefore:
when a provider response contains a tool_use for a gateway-injected tool, the
gateway can satisfy it from the artifact store, append the tool result, and
re-issue the request to the provider under a strict loop and byte budget.

This is not transparent streaming. A provider can emit visible content before a
gateway-owned tool call; once those bytes are released, the gateway cannot safely
hide the call and replace the response. A certified profile must therefore choose
one of two explicit contracts:

- expose `expand_artifact` to an agent/harness that implements it, preserving normal
  streaming; or
- use a buffered gateway-owned inner loop and accept the latency/streaming tradeoff.

The gateway must not claim byte-for-byte streaming passthrough while silently
intercepting tools. E1 and E4 certify the selected contract per dialect.

---

## 7. The cache-economics engine (I7's enforcement arm)

Per-provider models, versioned in the control plane:

- Anthropic: explicit or automatic cache behavior depending on model and API
  feature; current write/read prices, TTLs, and breakpoint limits are versioned
  data, not constants in application code.
- OpenAI: automatic prefix caching behavior and cached-input prices vary by model;
  eligibility thresholds and retention are versioned data.
- Self-hosted (vLLM/SGLang): radix-style prefix reuse — no discount to model,
  but real latency/throughput gains; the simulator optimizes hit length.

Functions:

1. **Breakpoint placement.** For Anthropic dialects, place/normalize
   `cache_control` markers at the stable boundaries our freeze ledger already
   defines (end of tools+system, end of frozen conversation). Marker changes alter
   the provider request and must be certified for that dialect; ProjectDiscovery's
   production result establishes potential on one workload, not an automatic saving
   for every agent.
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
  considered/applied/vetoed and why; input and output tokens; cache writes and
  reads; gross and net dollars; gateway latency; retries; expansion faults;
  artifact hashes; counting method; dialect, codec, and profile versions; and
  joined outcome status where available. Append-only; drives billing, audit,
  and learning.
- **Shadow scoring**: disabled/losing candidates are still generated (async,
  off the latency path) and scored, so we estimate mechanical opportunity before
  enabling a transform. Shadow mode cannot establish task-outcome safety by itself
  and remains a permanent evidence source;
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
- **Keys**: clients use tenant-scoped gateway credentials. Upstream provider keys
  are server-held or customer-vaulted, exposed only as short-lived internal leases,
  and never logged, returned, or written into decision receipts. An explicit
  passthrough-BYOK deployment mode may exist, but it is not the default trust model.
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
| Optimization component errors/times out | Ship original payload through the already-authorized route, log, alert |
| Auth, tenant, retention, DLP, budget, or egress policy fails | Reject; never bypass policy through passthrough |
| Session store unavailable | Tier 0–1 only (stateless transforms), degrade banner in dashboard |
| Artifact store write fails | Transform ineligible (I2), original ships |
| Tokenizer drift detected | Auto-suspend enforce for that model |
| Agent rewrote frozen history (fork) | New session, conservative profile |
| expand-fault storm (bad eviction) | Auto-demote paging for that dialect |
| Provider format change | Dialect auto-demotes to passthrough; alert |

SLO target: 99.95% authorized data-path availability. Optimization may degrade to
the original payload, but passthrough still traverses authentication, tenancy,
retention, DLP, budget, and egress policy. I1–I7 violations trigger automatic
demotion or rejection according to the affected boundary.

---

## 12. Standalone differentiation hypotheses

These are mechanisms worth testing, not established superlatives or claims of
competitive absence:

1. **Session-stateful optimization.** The scoped session model
   model (§3) turns a stateless wire protocol into incremental computation:
   O(suffix) work, memoized exact counts, frozen prefixes. Stateless
   compression APIs re-process the whole prompt every call — which forces them
   to approximate, breaks provider caches, and caps them at per-request
   tricks. Statefulness can unlock higher-value transforms (dedup,
   deltas, paging, cache shaping), and it is an architecture, not a feature
   a stateless competitor can patch in.
2. **Optimize billed cost per outcome, not tokens.** The cache-economics veto (§7) is the
   difference between a demo and a product: "20x compression" that busts a
   90%-discounted prefix is a bill increase. To our knowledge no shipping
   optimizer simulates provider cache economics per candidate; the
   ProjectDiscovery case shows cache shaping can materially reduce spend on one
   production workload.
3. **Bounded guarantees plus outcome evidence.** I1–I7 can establish identity,
   counting, recoverability, authorization, and fallback properties. Paired
   non-inferiority testing establishes whether model-visible substitutions preserve
   task outcomes for a certified profile. Do not collapse those proof types into a
   universal “never worse” claim.
4. **It is agent-aware where everyone else is generic.** The dialect registry
   (§8) is the WAAS application-optimizer playbook applied to agents:
   fingerprints, parsers, per-dialect profiles, days-not-months response to
   harness releases. Generic gateways cannot touch transcript interiors
   safely *because* they lack this layer.
5. **It has a memory hierarchy, not just a compressor.** Context paging with
   certified expansion (§6) may make savings scale with session length. E2
   measures the workload distribution; E4 measures whether faults and extra
   round trips preserve outcomes and net value.
6. **Make claims inspectable.** Open decision schema,
   always-on shadow scoring, promotion state machine, published latency and
   savings distributions, per-request audit. The customer can check every
   number we bill against.

The potential compounding loop is: requests generate consented shadow evidence →
profiles improve per dialect × model → better measured savings and safety records →
more trusted traffic → better evidence. E2/E4 and customer retention must establish
whether that loop is real and defensible.

---

## 13. Addendum — Switchboard + optimizer: 2 + 2 = 5

Everything above stands alone. Switchboard adds the two things a standalone
optimizer structurally lacks, and the optimizer repays Switchboard in kind:

1. **True outcome ground truth.** Standalone eval uses proxies (§9).
   Switchboard has completion gates, CI receipts, review verdicts — the
   promotion state machine can evaluate cost against verified delivery outcomes
   rather than transcript proxies alone.
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

2+2=5, precisely: Switchboard can make the optimizer's *evidence* materially
better through verified outcomes, while the optimizer can improve Switchboard's
economics and throughput where E2/E4 show a real opportunity. Both products retain
independent value and an explicit integration seam.
