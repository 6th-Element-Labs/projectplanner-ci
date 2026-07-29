# How do we know it will work? — feasibility deep dive on sitting in the middle

Status: research draft, fourth in the series (techniques → market → tech design → this)
Question answered: can we actually, provably, sit in the request path of Claude
Code, Codex, and Cursor as a trusted middlebox — and how do we prove it before
building the product?

**One-line answer:** The middle position already exists, is officially documented
by two of the big three, and carries enterprise traffic today through LiteLLM,
Bedrock/Vertex, and commercial gateways; our transforms ride a sanctioned seam
rather than creating a new one. The open question is not *whether* we can sit
there — it is *how much of the traffic* is addressable per agent (measured below)
and whether transforms survive protocol contracts (they do, with rules this doc
inventories). The rest is a three-week falsifiable experiment plan.

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
- **Claude Code actively cooperates with gateways**: on startup it calls
  `GET /v1/models` against `ANTHROPIC_BASE_URL` and adds returned models to its
  picker labeled "From gateway." The client is *designed* to talk to us.
- **Commercial gateways (Portkey, Helicone, Cloudflare, Kong, LLM Gateway)
  carry coding-agent traffic today** — passthrough, observability, sometimes
  caching. Nobody has protocol-level trouble being in the path; they simply
  don't transform.

So the novel part of our product is not the seat — it's what we do in it. That
reduces feasibility risk to claims 2 and 3.

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

### 3.3 Cursor — partial by architecture; be honest about it

Cursor's flow is unlike the CLIs: **client → Cursor's backend (api2.cursor.sh)
→ providers**. Prompt assembly happens on their servers.

**Lanes.**
- Cursor-served models (their subscriptions, Composer/tab models): **closed to
  us.** Their backend is the middle, and there is no configuration surface.
- BYO-key with base-URL override: **addressable.** Requests still transit
  Cursor's backend for prompt processing, but egress goes to the customer's
  configured base URL with the customer's key — i.e., *Cursor's backend
  becomes our client*, and we sit between it and the provider. Documented
  gateway integrations (OpenRouter, LLM Gateway) prove this works for the AI
  panel: plan mode and agent mode route through the override.
- Known quirk: Cursor hijacks recognized model names to its own routing;
  community-documented workaround is a custom model-name prefix (e.g.
  `cus-…`) to force the custom endpoint. Fragile — goes in the dialect
  registry as a Cursor-specific rule, monitored per release.
- Tab autocomplete and inline edit (Cmd/K): **locked to Cursor's backend**,
  not addressable, and also not the spend problem (agent-mode sessions are
  where the 400k–2M-token tasks live).

**Feasibility verdict for Cursor:** the agent-mode BYO-key lane — the
expensive lane — is addressable; the rest is not. We say so plainly in
positioning and measure the split in E2 rather than hand-waving it.

### 3.4 Addressability matrix

| Agent | Lane | In-path? | Mechanism | Notes |
|---|---|---|---|---|
| Claude Code | API key / enterprise | **Yes** | `ANTHROPIC_BASE_URL` | Officially documented; model discovery cooperates |
| Claude Code | Bedrock / Vertex | Yes, with work | Same seam, cloud dialects | Dialect cost, enterprise-heavy lane |
| Claude Code | Pro/Max OAuth | **No (red line)** | Harness lever only | CodexZero-class local techniques |
| Codex | API key | **Yes** | `model_providers` + Responses API | Must speak Responses; explicit provider select |
| Codex | ChatGPT OAuth | **No (red line)** | Harness lever only | |
| Cursor | BYO-key agent/plan mode | **Yes** | Base-URL override | Via Cursor's backend as client; name-hijack quirk |
| Cursor | Cursor-served models, tab, Cmd/K | **No** | — | Closed loop; also not the token-burn center |

Every "No" above has a stated fallback or a stated reason it doesn't matter;
every "Yes" has a documented mechanism used in production today by others.

---

## 4. Will transforms survive? The fidelity argument

The middlebox contracts, inventoried:

1. **Streaming**: SSE event grammar per provider passes through unbuffered
   except the bounded hold-back for gateway-owned tool_use detection (tech doc
   §6.4). Gateways already prove streaming passthrough at scale.
2. **Signed content**: thinking/reasoning blocks verbatim (I3-aligned, §3.1).
   Transforms operate on tool results and injected content only — the spans
   with no integrity seal, which are exactly the spans with the redundancy.
3. **Caching**: `cache_control` and automatic prefix caching function through
   gateways (caching keys on org/key + prefix bytes, both of which we
   forward/stabilize). E1 verifies with `usage.cache_read_input_tokens`
   through-proxy vs direct.
4. **Ancillary endpoints**: `/v1/models`, `count_tokens`, error/429/retry
   semantics — faithful proxying, verified by conformance fixtures.
5. **Task success under transforms**: the existence proof is CodexZero's
   repeated benchmark — 15% fewer tokens, identical 29/36 score — using the
   same tier-1/2 transform families we ship first. Provider-side context
   editing and compaction betas are second-source evidence that managed
   history doesn't break agents when diagnostics and recency are preserved.
6. **Client-side history rewriting** (agents' own compaction): detected as
   forks by the session chain (tech doc §3); the session degrades to
   conservative profile instead of breaking. E3 measures how often this
   actually happens per agent.

Residual fidelity risk is therefore concentrated in one place: dialect drift —
agents changing transcript shape between releases. That is a monitoring-and-
cadence problem (dialect registry, auto-demote to passthrough on unrecognized
shapes), not an architecture problem.

---

## 5. The proof plan — falsifiable, ~3 weeks, before any product build

Each experiment has pass/fail criteria. Failing ones falsify specific product
claims, not vibes.

- **E1 — Passthrough fidelity.** Vanilla proxy (no transforms) in front of
  each agent; run a fixed benchmark task set (terminal-bench-style subset)
  through-proxy and direct.
  *Pass:* zero protocol errors; task outcomes statistically indistinguishable;
  provider cache-read rates preserved within noise; added latency p50 ≤ 25 ms.
  *Falsifies if failed:* the seat itself (unlikely — see §2 precedent — but
  this is the cheap check that finds cert pinning, header, or SSE surprises).
- **E2 — Redundancy census (the prize measurement).** Capture N ≥ 100 real
  sessions per agent (dogfood + consenting users); compute per-transform
  would-have-saved: duplicate output bytes, RLE potential, re-read frequency
  and diffability, schema resend volume, cache hit/miss economics, and the
  addressable-lane split from §3.4.
  *Pass:* aggregate tier-1..3 savings ≥ 10% of input tokens on tool-heavy
  sessions.
  *Falsifies if failed:* the market-size claim (the residual-waste graph from
  the market doc, measured for real).
- **E3 — Session-chain robustness.** Run the Merkle-chain matcher over E2's
  captures. Measure: continuation match rate, fork rate per agent (client-side
  compaction frequency), false-merge rate (must be zero).
  *Pass:* ≥ 95% of turns matched as clean continuations for CLI agents;
  forks detected, never mis-merged.
  *Falsifies if failed:* the stateful architecture (would force stateless-only
  transforms — a different, weaker product).
- **E4 — Transform survival.** Tier 1–2 enabled on the E1 benchmark set,
  A/B against passthrough.
  *Pass:* task score parity within confidence bounds; measured token
  reduction ≥ 60% of E2's predicted savings; zero signature or protocol
  errors; dollar savings positive after cache simulation (I7).
- **E5 — Cursor lane verification.** BYO-key agent-mode sessions through us:
  confirm the backend-as-client flow, the model-name quirk workaround, and
  capture Cursor's egress dialect for the registry.
  *Pass:* agent-mode parity; documented dialect entry.

Kill criteria, stated up front: E2 savings < 5% on realistic workloads, or E3
fork rates so high that statefulness rarely engages, or E4 score regression —
any of these means the standalone thesis fails and the effort folds back into
harness-lever work inside Switchboard only.

---

## 6. The trust posture (what makes the man in the middle *trusted*)

- **Consent is structural.** We exist in a session only because the operator
  set our URL. Every request carries their key, forwarded, never stored.
- **Red lines respected mechanically, not by policy doc**: OAuth/subscription
  auth flows are refused at ingress (we never terminate them), so the
  ToS-sensitive lanes cannot transit us even by misconfiguration.
- **Anthropic's not-endorsed caveat** is managed, not ignored: stay
  Claude-to-Claude on the Claude Code seam (their stated non-support is about
  routing to non-Claude models), keep transforms auditable, and pursue the
  partner conversation from a position of "we increase cache hit rates and
  reduce waste" — behavior providers have shipped features to encourage.
- **Fail-open + invariant monitors + per-request audit** (tech doc §9–11): the
  operator can verify, on any request, exactly what we did and what it would
  have cost without us. Trust is a property of the audit trail, not the
  brand.
- The quality-incident history in this ecosystem (e.g. Anthropic's own 2026
  postmortem on Claude Code quality reports) shows even first parties break
  agent behavior through infrastructure changes — and that the recovery
  currency is transparent postmortems and receipts. We design for that
  standard from day one because as a third party we get no benefit of the
  doubt.

---

## 7. Verdict

- **Path:** proven by documentation and production precedent for Claude Code
  and Codex API lanes; proven-with-caveats for Cursor's BYO-key agent lane;
  correctly impossible for OAuth lanes (served by the harness lever instead).
  The seat is real and already warm.
- **Fidelity:** the dangerous contracts (signatures, streaming, caching) are
  inventoried and — notably — our pre-existing invariants (prefix freeze,
  tool-result-only transforms, fail-open) are the *same rules the protocols
  themselves enforce*. The architecture and the protocol point the same way.
- **Trust:** structural consent, mechanical red lines, audit-trail-based
  verification.
- **Remaining unknown:** the size of the prize on real fleets — which is
  E2, a measurement, not a bet. Three weeks of experiments convert this
  document's argument into numbers before a line of product code is written.

Sources: code.claude.com/docs/en/llm-gateway · docs.litellm.ai Claude Code
tutorials · morphllm.com / truefoundry.com / dsebastien.net LiteLLM setup
guides · github.com/openai/codex issue #11698 and config.toml guides
(ofox.ai, mcsaguru.com, openrouter.ai Codex tutorial) · Cursor gateway
integrations (docs.llmgateway.io, openrouter.ai cookbook) and community
routing analyses (dev.to Cursor proxy deep dive, forum.cursor.com) ·
platform.claude.com docs on extended thinking and context editing ·
anthropic.com April 2026 quality postmortem · Retro2512/CodexZero benchmark
reports. Companions: TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md ·
TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md · TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md.
