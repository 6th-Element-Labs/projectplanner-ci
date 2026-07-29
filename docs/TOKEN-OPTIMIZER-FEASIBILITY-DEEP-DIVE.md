# How do we know it will work? — feasibility deep dive on sitting in the middle

Status: research draft, fourth in the series (techniques → market → tech design → this)
Question answered: can we actually, provably, sit in the request path of Claude
Code, Codex, and Cursor as a trusted middlebox — and how do we prove it before
building the product?

**One-line answer:** Supported API seams and live loopback canaries prove that
provider-ready Codex and Claude Code requests can reach us; Cursor is partial and
subscription OAuth remains out of path. Passthrough fidelity, addressable traffic,
transform safety, and net outcome value remain experiment results—not conclusions.

---

## 1. Decomposing "will it work"

Three separate claims, each needing its own evidence:

1. **Path**: can we get into the request path at all, per agent, per auth lane?
2. **Fidelity**: can we transform payloads without breaking protocol contracts
   (streaming, tool calls, signatures, caching, error semantics)?
3. **Trust**: can we hold the position legitimately — consented, ToS-clean,
   auditable — rather than as an adversarial interceptor?

We are *not* a man-in-the-middle in the security sense, and the distinction is
the foundation of claim 3: agents connect **to us** over TLS because the user
configured our URL. We are a configured endpoint, not an interceptor. No
certificate games, no TLS stripping — the exact problem that plagued
WAAS-era optimization (which had to break TLS to optimize) does not exist here.
The seam is a supported configuration surface.

---

## 2. Precedent: the middle is already occupied, at scale

Before any per-agent analysis, the strongest feasibility evidence is that this
position is already an ecosystem:

- **Anthropic officially documents the LLM-gateway configuration** for Claude
  Code (code.claude.com/docs/en/llm-gateway), including LiteLLM specifically.
  LiteLLM publishes Claude Code tutorials; a cottage industry of setup guides
  exists (morphllm, truefoundry, dsebastien, feiskyer).
- **Enterprises already route Claude Code through middleboxes in production**:
  corporate proxies, LiteLLM for key management and spend tracking, and
  Bedrock/Vertex/Azure-Foundry backends where the "provider" is itself a
  gateway translating wire formats.
- **Claude Code actively cooperates with gateways** through
  `ANTHROPIC_BASE_URL`. In the local 2.1.218 canary it called `HEAD /api/hello`
  and then `/v1/messages?beta=true`. Optional model-discovery behavior must be
  verified per client version rather than assumed.
- **Commercial gateways (Portkey, Helicone, Cloudflare, Kong, LLM Gateway)
  carry coding-agent traffic today** — passthrough, observability, sometimes
  caching. Their existence is precedent for configured proxying, not proof that
  every client feature, transform, or provider contract is compatible.

So the novel part of our product is not basic configured proxying—it's certified
wire fidelity, transformation, coverage proof, and outcome evidence. Existing
gateways reduce path risk but do not prove our transforms or all client features.

One caveat to carry honestly: Anthropic's docs note it doesn't endorse,
maintain, or audit third-party gateways, and doesn't support routing Claude
Code to non-Claude models through one. Documented-but-not-endorsed is the
current posture; §6 covers what we do about it.

---

## 3. Agent-by-agent traffic anatomy

### 3.1 Claude Code — the front door is open

**Path.** Two env vars: `ANTHROPIC_BASE_URL` (points at us) and
`ANTHROPIC_AUTH_TOKEN` (the static key sent to us; we forward or map to the
tenant's real key). Alternate enterprise paths (`CLAUDE_CODE_USE_BEDROCK`,
Vertex) mean some fleets reach Claude through cloud endpoints — we can sit in
front of those too by speaking the same seam, at the cost of supporting the
Bedrock/Vertex wire dialects (a scoped, known amount of work).

**Lanes.**
- API key / enterprise gateway lane: **fully addressable**, officially
  documented. This is the lane enterprises use for spend control already —
  our buyer's lane.
- Personal subscription (Pro/Max OAuth) lane: **not proxyable, by design and
  by terms**. OAuth session auth must go direct to Anthropic. This is a hard
  red line (consistent with MODEL-CATALOG-ROUTING: personal CLI auth never
  transits a gateway). This lane is served by the harness lever
  (CodexZero-style local techniques), not the proxy.

**Wire contracts we must honor** (the fidelity inventory):
- `/v1/messages` with SSE streaming (event grammar: `content_block_delta`,
  `thinking_delta`, `signature_delta`, …) — pass-through untouched (tech doc §2).
- `cache_control` breakpoints — forwarded, and optimized (tech doc §7).
- `/v1/models` discovery and `/v1/messages/count_tokens` — implement both.
- **Thinking blocks carry cryptographic signatures and must round-trip
  unmodified**; on tool-use turns the last assistant message's thinking block
  is mandatory and verbatim. Any middlebox that edits assistant content breaks
  the session. This is a *feasibility positive* for us specifically: our
  invariant I3 (prefix freeze) and the rule that transforms touch only
  tool_result/user content were derived from cache economics — the signature
  contract *mandates* the same discipline. Our architecture is aligned with
  the protocol's own integrity mechanism; naive competitors' isn't.
- Precedent that history mutation is survivable when done right: Anthropic's
  own context-editing strategies (`clear_thinking_20251015`, compaction beta)
  mutate conversation history server-side. The protocol anticipates managed
  history; it forbids only *unauthorized* mutation of signed blocks.

### 3.2 Codex — a config block, with two sharp edges

**Path.** `~/.codex/config.toml`, `[model_providers.<id>]` with `base_url`,
`env_key`, and `wire_api`. Declaring a provider and selecting it routes the
CLI through us. CodexZero separately proves the harness lever works (patched
core, env injection) for what the proxy can't reach.

**Sharp edges (both confirmed 2026):**
1. `wire_api = "chat"` (or omitted) **fails on startup** as of February 2026 —
   Codex now requires the **Responses API** dialect. We must speak Responses
   (stateful items, encrypted reasoning items) natively, not just Chat
   Completions. This is real scoped work and a moat-ette: gateways that only
   speak chat-completions are locked out of Codex.
2. Provider IDs `openai`, `ollama`, `lmstudio` are **reserved** — you cannot
   silently repoint the built-in OpenAI provider. Users must select our
   provider explicitly. Slightly more onboarding friction than Claude Code
   (edit config + select provider vs. two env vars), still minutes.

**Lanes.** API-key lane addressable as above. ChatGPT-subscription OAuth lane:
same red line as Claude Code's — harness lever only. Encrypted reasoning items
must round-trip verbatim (same integrity argument as §3.1).

### 3.3 Local loopback evidence captured 2026-07-29

The audit used a local HTTP listener with a dummy gateway credential. It recorded
method, path, selected non-secret headers, body shape, and byte count, then returned
`502` so no upstream provider was called. Credential values and full prompt bodies
were not retained.

| Client | Version/build | Observed gateway traffic | What it proves |
|---|---|---|---|
| Codex CLI | `0.144.5` | `GET /v1/models?client_version=0.144.5`; `POST /v1/responses`; authorization present; representative JSON request 57,691 bytes | custom-provider Responses request reaches configured gateway |
| Claude Code | `2.1.218` | `HEAD /api/hello`; `POST /v1/messages?beta=true`; authorization present; representative JSON request 4,911 bytes | API/gateway Messages request reaches configured gateway |
| Cursor Agent CLI | `2026.07.23-e383d2b` | `POST /aiserver.v1.DashboardService/GetMe`; `POST /auth/exchange_user_api_key` | `--endpoint` redirects Cursor control protocol, not provider-ready inference |
| Cursor IDE | `3.13.25`, commit `31e8d61c448c7472e371505838a0fe34083dad50` | installed bundle exposes local OpenAI base-URL/key configuration; authenticated model canary not run | implementation seam exists; end-to-end coverage remains unproven |

Both Codex and Claude retried after the intentional `502`, making request identity,
attempt identity, and duplicate-safe usage accounting part of the minimum contract.

Reproduction commands used:

```bash
CANARY_GATEWAY_KEY=canary-token \
codex exec --ephemeral --ignore-user-config --skip-git-repo-check \
  -C /tmp -m gpt-5.4 \
  -c 'model_provider="switchboard_canary"' \
  -c 'model_providers.switchboard_canary.name="Switchboard Canary"' \
  -c 'model_providers.switchboard_canary.base_url="http://127.0.0.1:18765/v1"' \
  -c 'model_providers.switchboard_canary.env_key="CANARY_GATEWAY_KEY"' \
  -c 'model_providers.switchboard_canary.wire_api="responses"' \
  'Reply only with canary'
```

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:18765 \
ANTHROPIC_AUTH_TOKEN=canary-token \
DISABLE_TELEMETRY=1 \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
claude --bare --no-session-persistence -p \
  --model claude-sonnet-4-5-20250929 \
  'Reply only with canary'
```

```bash
cursor agent \
  --endpoint http://127.0.0.1:18765 \
  --api-key canary-token \
  --print --mode ask --output-format json \
  'Reply only with canary'
```

The dummy values are intentionally nonfunctional. Future fixtures must log hashes and
bounded structural metadata rather than source-bearing prompt prefixes.

### 3.4 Cursor — partial by architecture; be honest about it

Cursor's flow is unlike the CLIs: **client → Cursor's backend (api2.cursor.sh)
→ providers**. Prompt assembly happens on their servers.

**Lanes.**
- Cursor-served models (their subscriptions, Composer/tab models): **closed to
  us.** Their backend is the middle, and there is no configuration surface.
- BYO-key with base-URL override: **plausibly addressable; E5 pending.** Available
  configuration and third-party reports indicate requests can transit
  Cursor's backend for prompt processing, but egress goes to the customer's
  configured base URL with the customer's key — i.e., *Cursor's backend
  becomes our client*, and we sit between it and the provider. Documented
  gateway integrations (OpenRouter, LLM Gateway) prove this works for the AI
  panel: plan mode and agent mode route through the override.
- Known quirk: Cursor hijacks recognized model names to its own routing;
  community-documented workaround is a custom model-name prefix (e.g.
  `cus-…`) to force the custom endpoint. Fragile — goes in the dialect
  registry as a Cursor-specific rule, monitored per release.
- Tab autocomplete and inline edit (Cmd/K): **locked to Cursor's backend** and
  not addressable. Their share of cost, latency, and quota pressure is unknown
  until measured; do not dismiss it without data.

**Feasibility verdict for Cursor:** the control endpoint is not an inference
gateway, and the BYO-key agent lane remains a limited preview until an authenticated
mock-provider canary proves the final request path and feature coverage.

### 3.5 Addressability matrix

| Agent | Lane | In-path? | Mechanism | Notes |
|---|---|---|---|---|
| Claude Code | API key / enterprise | **Yes** | `ANTHROPIC_BASE_URL` | Officially documented; model discovery cooperates |
| Claude Code | Bedrock / Vertex | Yes, with work | Same seam, cloud dialects | Dialect cost, enterprise-heavy lane |
| Claude Code | Pro/Max OAuth | **No (red line)** | Harness lever only | CodexZero-class local techniques |
| Codex | API key | **Yes** | `model_providers` + Responses API | Must speak Responses; explicit provider select |
| Codex | ChatGPT OAuth | **No (red line)** | Harness lever only | |
| Cursor | BYO-key agent/plan mode | **Pending E5** | Base-URL override | Final provider payload and feature coverage not yet locally proven |
| Cursor | Cursor-served models, tab, Cmd/K | **No** | — | Closed loop; also not the token-burn center |

Every “Yes” must be backed by a versioned canary receipt. A documented configuration
is discovery evidence; it is not equivalent to observed full-feature coverage.

---

## 4. Will transforms survive? The fidelity argument

The middlebox contracts, inventoried:

1. **Streaming**: SSE event grammar per provider passes through unbuffered
   according to the certified response contract (tech doc §6.2). Transparent
   streaming and a hidden gateway-owned tool loop are mutually exclusive.
2. **Signed content**: thinking/reasoning blocks verbatim (I3-aligned, §3.1).
   Transforms operate on tool results and injected content only — the spans
   with no integrity seal, which are exactly the spans with the redundancy.
3. **Caching**: `cache_control` and automatic prefix caching function through
   gateways (caching keys on org/key + prefix bytes, both of which we
   forward/stabilize). E1 verifies with `usage.cache_read_input_tokens`
   through-proxy vs direct.
4. **Ancillary endpoints**: `/v1/models`, `count_tokens`, error/429/retry
   semantics — faithful proxying, verified by conformance fixtures.
5. **Task success under transforms**: CodexZero's reported benchmark is useful
   existence evidence for a related implementation. It does not certify our
   transforms, models, agent versions, or workloads; E4 supplies that evidence.
6. **Client-side history rewriting** (agents' own compaction): detected as
   forks by the session chain (tech doc §3); the session degrades to
   conservative profile instead of breaking. E3 measures how often this
   actually happens per agent.

Residual fidelity risks include dialect drift, signed or opaque state, streaming
ordering, hidden provider state, retry duplication, ambiguous session identity,
artifact reachability, and model sensitivity to changed representations. The
dialect registry and auto-demotion address only part of this set.

---

## 5. The E1–E5 proof plan — falsifiable before product build

Each experiment has pass/fail criteria. Failing ones falsify specific product
claims, not vibes. Sequence is evidence-gated rather than calendar-gated; collecting
representative consenting traffic may take longer than three weeks.

- **E1 — Insertion and passthrough certification.** Put a no-transform mock/proxy
  in front of every claimed `(client version, auth lane, feature profile)`.
  Exercise discovery, non-streaming, SSE, tools, usage, caching metadata,
  compaction where applicable, `401`, `429`, `500`, timeout, disconnect, and
  retry behavior. Run direct and through-proxy paired tasks.
  *Pass:* expected endpoints and wire events round-trip; no unsupported direct
  inference egress; zero protocol/signature errors; usage fields reconcile; the
  predeclared non-inferiority margin is met with adequate statistical power; added
  latency meets a predeclared SLO. Emit `gateway_coverage_receipt.v1`.
  *Falsifies if failed:* the claimed client/auth/feature lane, not unrelated lanes.
- **E2 — Redundancy census (the prize measurement).** Capture N ≥ 100 real
  sessions per target segment—not merely per brand—from dogfood and consenting
  users. Stratify by agent, auth lane, model, task class, session length, and
  cache state. Compute per-transform
  would-have-saved: duplicate output bytes, RLE potential, re-read frequency
  and diffability, schema resend volume, cache hit/miss economics, and the
  addressable-lane split from §3.5. Charge tokenizer calls, storage, optimizer
  compute, cache writes, retries, and latency to the result.
  *Pass:* the target segment shows a preregistered positive net billed-cost or
  quota-throughput opportunity with confidence intervals; raw token reduction is
  diagnostic only.
  *Falsifies if failed:* the standalone optimization thesis for that segment.
- **E3 — Session-identity robustness.** Evaluate declared IDs first and the scoped
  hash-chain fallback over captures plus adversarial replay, parallel identical
  starts, edited retry, compaction, and branch cases. Measure continuation,
  ambiguity, fork, false-split, and false-merge rates.
  *Pass:* zero false merges and zero cross-principal artifact exposure; ambiguous
  cases create a new conservative session; continuation rate is high enough for
  stateful transforms to beat their operating cost.
  *Falsifies if failed:* the stateful architecture (would force stateless-only
  transforms for the affected lane).
- **E4 — Transform survival and outcome non-inferiority.** Enable one transform
  at a time on the E1 corpus, then test approved combinations. Preregister task
  metrics, non-inferiority margins, sample sizes, model snapshots, seeds, and
  rollback criteria.
  *Pass:* the non-inferiority margin is met; provider-reported billed cost improves
  after optimizer overhead; zero signature/protocol violations; artifact retrieval,
  cache behavior, retries, and latency remain within guardrails.
- **E5 — Cursor lane verification.** BYO-key agent-mode sessions through us:
  use a dedicated test account and mock OpenAI-compatible provider to determine
  whether the final payload reaches us from the IDE or Cursor backend. Exercise
  agent, plan, tools, background features, model families, Responses versus Chat
  Completions, and known bypasses.
  *Pass:* provider-ready payload and supported features are observed, direct
  inference bypass is measured, parity meets the E1 standard, and the receipt says
  `full` or `partial` with named exclusions. A control-endpoint call alone fails E5.

Kill criteria, stated up front: no economically material E2 opportunity in the
chosen segment, unsafe/uneconomic session identity in E3, or failure to meet E4's
non-inferiority margin. Any of these folds that lane back into observe-only or
harness-lever work rather than averaging failure away across the fleet.

### 5.1 `gateway_coverage_receipt.v1`

Configuration is not proof of insertion. E1/E5 emit a machine-readable receipt:

```json
{
  "schema": "gateway_coverage_receipt.v1",
  "client": "codex",
  "client_version": "0.144.5",
  "auth_lane": "custom_api_provider",
  "adapter": "openai-responses/v1",
  "certified_features": ["models", "responses", "sse"],
  "observed_endpoints": ["/v1/models", "/v1/responses"],
  "direct_inference_egress_observed": false,
  "coverage": "full",
  "evidence_hash": "sha256:..."
}
```

Coverage values are `full`, `partial`, `control_only`, `unsupported`, and
`unknown`. A client, auth, adapter, feature, or endpoint-map change resets coverage
to `unknown` until recertified.

---

## 6. The trust posture (what makes the man in the middle *trusted*)

- **Consent is structural.** We exist in a session only because the operator
  set our URL. The client uses a gateway credential; upstream provider credentials
  remain server-held or customer-vaulted and never appear in receipts or logs.
- **Red lines respected mechanically, not by policy doc**: OAuth/subscription
  auth flows are refused at ingress (we never terminate them), so the
  ToS-sensitive lanes cannot transit us even by misconfiguration.
- **Anthropic's not-endorsed caveat** is managed, not ignored: stay
  Claude-to-Claude on the Claude Code seam (their stated non-support is about
  routing to non-Claude models), keep transforms auditable, and pursue the
  partner conversation from a position of "we increase cache hit rates and
  reduce waste" — behavior providers have shipped features to encourage.
- **Optimization fail-open + policy fail-closed + per-request audit** (tech doc
  §9–11): transform uncertainty sends the original payload only through an already
  authorized route; auth, tenant, retention, DLP, budget, and egress failures reject.
  Trust is a property of the enforced boundary and audit trail, not the brand.
- The quality-incident history in this ecosystem (e.g. Anthropic's own 2026
  postmortem on Claude Code quality reports) shows even first parties break
  agent behavior through infrastructure changes — and that the recovery
  currency is transparent postmortems and receipts. We design for that
  standard from day one because as a third party we get no benefit of the
  doubt.

---

## 7. Verdict

- **Path:** locally proven for the tested Claude Code and Codex API modes;
  documented but still pending authenticated E5 proof for Cursor's BYO-key lane;
  correctly impossible for OAuth lanes (served by the harness lever instead).
  The seat is real and already warm.
- **Fidelity:** dangerous contracts are inventoried, but streaming, hidden tool
  loops, signatures, caching, retries, and outcome non-inferiority remain E1/E4
  acceptance work.
- **Trust:** structural consent, mechanical red lines, audit-trail-based
  verification.
- **Remaining unknowns:** addressable share, net economic prize, session identity,
  transform non-inferiority, and Cursor coverage. E1–E5 convert those claims into
  measured go/no-go decisions before a product build.

Primary path sources:

- [Anthropic Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Anthropic Claude Code corporate proxy configuration](https://docs.anthropic.com/en/docs/claude-code/corporate-proxy)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml)
- [Codex model-provider implementation at the audited commit](https://github.com/openai/codex/blob/28f3f1f9ef4e9578a5f023f6b6eba018914a5342/codex-rs/model-provider-info/src/lib.rs)
- [Codex client endpoints at the audited commit](https://github.com/openai/codex/blob/28f3f1f9ef4e9578a5f023f6b6eba018914a5342/codex-rs/core/src/client.rs)
- [Cursor custom API keys and feature limitations](https://docs.cursor.com/settings/api-keys)

LiteLLM tutorials, commercial gateway guides, Cursor community reports, and
CodexZero benchmark reports are supporting discovery evidence, not substitutes for
the local canaries or E1–E5.

Companions: TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md ·
TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md · TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md.
