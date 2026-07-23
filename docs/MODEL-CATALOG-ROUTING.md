# Switchboard model catalog + discovery contract

Status: design draft for Autopilot / ADAPTER / COORD  
Depends on: typed `execution_connection_id`, provider-auth capability matrix, abstract `model_tier` on `claim_next`, LiteLLM API-only boundary

**One-line contract:** Discover often, pin slowly, dispatch abstractly, launch concretely — and never let LiteLLM speak for a personal CLI.

---

## 0. Summary (how we'll do it)

Autopilot dispatches on abstract **runtime + tier + effort**, resolved against each user's eligible execution connection, while adapters discover concrete models into a Switchboard catalog and only slow, promoted pins become defaults. Personal CLI catalogs and LiteLLM stay separate lanes with no silent fallback, and every choice is recorded as an explainable `routing_decision` with Tally attribution. New models enter as candidates; stale or unresolved pins fail closed, and quota exhaustion pauses that lane rather than hopping providers.

---

## 1. Goals

1. Autopilot speaks **runtime + tier + effort**, not raw vendor IDs on every task.
2. Concrete models stay current via **discovery**, not hand-edited forever lists.
3. Personal CLI catalogs and LiteLLM API catalogs are **separate lanes** that never silently substitute.
4. Every routing choice is explainable and Tally-attributable.
5. Re-resolve **per wake / claim turn**, not once per session or deliverable (borrowed from Klaat's per-request tiering).

Non-goals: LiteLLM brokering personal subscriptions; auto-promoting every new model to fleet default; per-task hardcoding of `claude-opus-4.8`; a black-box hosted router that hides provider/model; silent cross-provider failover on failure or quota exhaustion.

---

## 2. What we keep vs what we borrow (KlaatCode review)

Reviewed [KlaatAI/klaatcode](https://github.com/KlaatAI/klaatcode): the open-source CLI is a thin client; provider + concrete model selection lives in hosted Klaatu-o1 and is not in that repo. Client speaks tier aliases (`klaatu-nano` … `klaatu-heavy`); server returns `tier`, `reason`, `model`, `provider`.

### Keep (Switchboard)

| Ours | Why |
|---|---|
| Project-owned, auditable routing | Need explainable decisions, BYOA connections, fail-closed billing |
| Runtime ≠ tier | Codex / Claude Code / Cursor are first-class; Klaat collapses this |
| Discover → promote → pin | Visible catalog beats opaque router |
| LiteLLM API-only boundary | Personal CLI auth must never go through the gateway |
| No silent provider hop | Quota/pause, not cross-account fallback |

### Borrow (mechanics only)

1. **Per-request (per-turn) tiering** — re-resolve `tier`/`effort` each wake/claim, not pin one model for a whole deliverable drain.
2. **Tier-aware tool/context budgets** — don't hand a `cheap` worker the full tool schema and a 200k pack (Klaat dialects + per-tier context windows).
3. **Structured quality feedback into routing** — failed launches, schema errors, empty responses feed Tally/promotion (Klaat `Model-Feedback` pattern).
4. **Persona / task_class → default tier** — explore/review/build maps to our `task_class` defaults; review prefers ≠ implementer runtime.
5. **User-facing “why”** — Mission/Autopilot shows one-line reason from `routing_decision` (Klaat `/why`).
6. **Explicit no-more-failover signal** — fail closed when escalation is exhausted; no client retry storms (Klaat `X-KlaatAI-Retry: no`).

### Do not copy

- Proprietary Klaatu-o1 as the decision brain
- Hiding provider/model behind product names only
- Auto provider cascade on failure
- “Tool rounds free, only messages bill” economics

---

## 3. Schemas

### 3.1 `switchboard.model_offer.v1` — one discovered model

```json
{
  "schema": "switchboard.model_offer.v1",
  "offer_id": "offer/claude-code/personal_subscription/claude-sonnet-5",
  "provider": "anthropic",
  "runtime": "claude-code",
  "auth_mode": "personal_subscription",
  "execution_path": "native_cli",
  "model_id": "claude-sonnet-5",
  "display_name": "Claude Sonnet 5",
  "aliases": ["sonnet-5", "claude-sonnet-5"],
  "capabilities": {
    "tools": true,
    "vision": true,
    "adaptive_thinking": true,
    "effort_levels": ["low", "medium", "high", "xhigh", "max"],
    "max_context_tokens": 1000000,
    "max_output_tokens": 128000
  },
  "entitlements": {
    "requires_plan": ["pro", "max", "team"],
    "metered": false
  },
  "provenance": {
    "source": "cli_probe",
    "source_ref": "claude --list-models",
    "cli_version": "1.2.3",
    "discovered_at": 1784260000.0,
    "evidence_expires_at": 1784864800.0,
    "host_id": "host/abc",
    "connection_fingerprint": "acct/…"
  },
  "status": "candidate"
}
```

| Field | Notes |
|---|---|
| `execution_path` | `native_cli` \| `litellm_gateway` \| `provider_api_direct` |
| `auth_mode` | Align with provider-auth matrix: `personal_subscription`, `direct_api`, `api_gateway`, `host_bound_login`, … |
| `status` | `candidate` \| `allowed` \| `default_for_tier` \| `deprecated` \| `blocked` |
| `evidence_expires_at` | Stale ⇒ cannot be newly selected (same spirit as auth capability expiry) |

### 3.2 `switchboard.model_catalog.v1` — project (or org) registry snapshot

```json
{
  "schema": "switchboard.model_catalog.v1",
  "catalog_id": "catalog/switchboard/2026-07-17T05:00:00Z",
  "project": "switchboard",
  "generated_at": 1784260000.0,
  "offers": ["/* model_offer.v1[] */"],
  "by_runtime": {
    "codex": { "auth_modes": ["personal_subscription", "direct_api"] },
    "claude-code": { "auth_modes": ["personal_subscription", "direct_api", "api_gateway"] },
    "cursor": { "auth_modes": ["host_bound_login", "direct_api"] },
    "litellm-gateway": { "auth_modes": ["api_gateway"] }
  },
  "integrity": {
    "offer_count": 42,
    "stale_offer_count": 0,
    "hash": "sha256:…"
  }
}
```

### 3.3 `switchboard.routing_policy.v1` — slow-changing pins

```json
{
  "schema": "switchboard.routing_policy.v1",
  "project": "switchboard",
  "version": 3,
  "default_runtime": "codex",
  "allowed_runtimes": ["codex", "claude-code", "cursor"],
  "litellm": {
    "eligible_auth_modes": ["direct_api", "api_gateway"],
    "forbidden_for": ["personal_subscription", "host_bound_login"],
    "may_broker_subscription_auth": false
  },
  "tiers": ["cheap", "balanced", "strong", "frontier"],
  "effort_levels": ["low", "medium", "high", "xhigh", "max"],
  "task_class_defaults": {
    "docs_edit": { "tier": "cheap", "effort": "medium" },
    "boilerplate": { "tier": "cheap", "effort": "medium" },
    "feature_impl": { "tier": "balanced", "effort": "high" },
    "refactor_multi": { "tier": "strong", "effort": "high" },
    "debug_subtle": { "tier": "strong", "effort": "xhigh" },
    "architecture": { "tier": "strong", "effort": "xhigh" },
    "explore": { "tier": "cheap", "effort": "medium" },
    "review": {
      "tier": "strong",
      "effort": "high",
      "prefer_runtime_neq_implementer": true
    },
    "ci_remediation": {
      "tier": "balanced",
      "effort": "medium",
      "prefer_same_runtime_as_implementer": true
    },
    "security_sensitive": {
      "tier": "strong",
      "effort": "xhigh",
      "min_tier": "strong",
      "approval_required_for_upgrade": true
    },
    "browser_ui": {
      "tier": "balanced",
      "effort": "high",
      "preferred_runtime": "cursor"
    }
  },
  "tier_budgets": {
    "cheap": {
      "tool_dialect": "concise",
      "max_context_tokens_hint": 40000
    },
    "balanced": {
      "tool_dialect": "full",
      "max_context_tokens_hint": 200000
    },
    "strong": {
      "tool_dialect": "full",
      "max_context_tokens_hint": 200000
    },
    "frontier": {
      "tool_dialect": "full",
      "max_context_tokens_hint": 200000
    }
  },
  "catalog_pins": {
    "codex": {
      "cheap": { "model_id": "gpt-5.6-luna", "effort": "medium" },
      "balanced": { "model_id": "gpt-5.6-terra", "effort": "high" },
      "strong": { "model_id": "gpt-5.6-sol", "effort": "xhigh" },
      "frontier": { "model_id": "gpt-5.6-sol", "effort": "max" }
    },
    "claude-code": {
      "cheap": { "model_id": "claude-sonnet-5", "effort": "medium" },
      "balanced": { "model_id": "claude-sonnet-5", "effort": "high" },
      "strong": { "model_id": "claude-opus-4-8", "effort": "xhigh" },
      "frontier": { "model_id": "claude-fable-5", "effort": "xhigh" }
    },
    "cursor": {
      "cheap": { "model_id": "composer-2.5", "effort": "medium" },
      "balanced": { "model_id": "claude-sonnet-5", "effort": "high" },
      "strong": { "model_id": "claude-opus-4-8", "effort": "xhigh" },
      "frontier": { "model_id": "claude-fable-5", "effort": "xhigh" }
    }
  },
  "escalation": {
    "max_auto_effort_bumps": 1,
    "max_auto_tier_bumps": 1,
    "max_auto_runtime_switches": 0,
    "require_human_for_frontier": true,
    "quota_exhaustion": "pause_lane_no_fallback",
    "exhausted_signal": "no_more_failover"
  },
  "promotion": {
    "auto_promote": "off",
    "candidate_ttl_days": 14,
    "require_tally_samples": 5,
    "require_dogfood_task_class": "feature_impl",
    "quality_feedback_counts": true
  },
  "enforcement_default": "advisory"
}
```

Pin rule: a `catalog_pins` model_id must resolve to an offer with `status ∈ {allowed, default_for_tier}` for that `(runtime, auth_mode)` and non-expired evidence. Otherwise dispatch fails closed with `pin_unresolved`.

Codex example pins use OpenAI's GPT-5.6 capability tiers (Luna / Terra / Sol) as concrete IDs under our abstract `cheap` / `balanced` / `strong` / `frontier` — replace via promote when discovery refreshes.

### 3.4 `switchboard.routing_decision.v1` — what Autopilot emits (per turn)

```json
{
  "schema": "switchboard.routing_decision.v1",
  "decision_id": "rdec/…",
  "project": "switchboard",
  "task_id": "COORD-8",
  "task_class": "feature_impl",
  "runtime": "claude-code",
  "auth_mode": "personal_subscription",
  "execution_connection_id": "execconn/…",
  "execution_path": "native_cli",
  "model_tier": "balanced",
  "effort": "high",
  "model_id": "claude-sonnet-5",
  "offer_id": "offer/claude-code/personal_subscription/claude-sonnet-5",
  "tool_dialect": "full",
  "alternatives_skipped": [
    { "runtime": "codex", "reason": "prefer_runtime_pin" },
    { "runtime": "cursor", "reason": "no_eligible_connection" }
  ],
  "policy_rule": "task_class_defaults.feature_impl + catalog_pins.claude-code.balanced",
  "enforcement": "claim_gate",
  "reason": [
    "task_class=feature_impl",
    "risk=medium",
    "budget=ok",
    "connection=claude personal eligible"
  ],
  "why_one_liner": "Claude Code · balanced (Sonnet 5 @ high) — feature_impl, budget ok",
  "created_at": 1784260000.0,
  "actor": "coordinator/autopilot"
}
```

This goes into wake metadata, `claim_next.recommendation`, coordinator decisions, and the Mission/Autopilot “why” surface.

### 3.5 `switchboard.routing_feedback.v1` — quality signal (borrowed pattern)

```json
{
  "schema": "switchboard.routing_feedback.v1",
  "project": "switchboard",
  "task_id": "COORD-8",
  "claim_id": "claim/…",
  "decision_id": "rdec/…",
  "runtime": "claude-code",
  "model_id": "claude-sonnet-5",
  "model_tier": "balanced",
  "error_type": "empty_response",
  "detail": "adapter launch returned empty after 30s",
  "created_at": 1784260000.0
}
```

`error_type` examples: `tool_validation`, `schema_error`, `edit_failure`, `timeout`, `empty_response`, `launch_failed`, `quota_exhausted`, `user_retry`, `failure`.

Feeds promotion scoring and reliability-weighted runtime preference — never bypasses allowlists.

---

## 4. Adapter contract: `discover_models`

### 4.1 Interface

```text
discover_models(input) -> DiscoverModelsResult
```

**Input**

```json
{
  "schema": "switchboard.discover_models_command.v1",
  "project": "switchboard",
  "runtime": "claude-code",
  "auth_mode": "personal_subscription",
  "execution_connection_id": "execconn/…",
  "execution_path": "native_cli",
  "host_id": "host/…",
  "cli_version": "1.2.3",
  "timeout_s": 30
}
```

**Result**

```json
{
  "schema": "switchboard.discover_models_result.v1",
  "ok": true,
  "runtime": "claude-code",
  "auth_mode": "personal_subscription",
  "execution_path": "native_cli",
  "cli_version": "1.2.3",
  "discovered_at": 1784260000.0,
  "evidence_expires_at": 1784864800.0,
  "offers": ["/* model_offer.v1[] with status=candidate or refreshed */"],
  "errors": [],
  "verification": "probed"
}
```

`verification`:

- `probed` — live CLI/API list
- `documented` — adapter static table from reviewed vendor docs (allowed only with short TTL)
- `gateway` — LiteLLM `/model/info`
- `unknown` — fail closed for enforcement ≥ `claim_gate`

### 4.2 Per-runtime obligations

| Runtime | `execution_path` | Discovery source | Must not |
|---|---|---|---|
| `codex` | `native_cli` | Codex CLI / account-visible model list for that auth | Invent API models as personal |
| `claude-code` | `native_cli` | Claude CLI model list for binary+auth | Use LiteLLM list as personal catalog |
| `cursor` | `native_cli` | `cursor-agent` model/list surface for that auth | Treat browser login models as portable to ephemeral hosts |
| `litellm-gateway` | `litellm_gateway` | Gateway model info | Appear as personal_subscription offers |

### 4.3 Failure behavior

- Probe timeout / auth error → `ok:false`, no catalog mutation, reason code (`discover_auth_failed`, `discover_timeout`, …)
- Empty list on a previously populated runtime → do **not** wipe allowed pins; mark refresh failed; keep last good catalog with warning
- New IDs → upsert as `candidate` only
- Missing previously pinned ID → pin stays but `pin_health=missing_from_discovery` (dispatch may use last known if not expired; else `pin_unresolved`)

---

## 5. Registry lifecycle

```text
discover (adapter)
  → upsert offers (candidate | refresh allowed)
  → operator/policy promote
  → catalog_pins point at allowed/default_for_tier
  → routing_decision resolves pin (per turn)
  → launch with tier-shaped tool/context budget
  → report_usage + optional routing_feedback
  → (optional) auto-promote under promotion rules
```

### Status transitions

```text
candidate ──promote──► allowed ──set_default──► default_for_tier
    │                     │                         │
    │                     ├──deprecate──► deprecated │
    │                     └──block──────► blocked    │
    └──────────── block ─────────────────────────────┘
```

Only `allowed` / `default_for_tier` may be selected by Autopilot without an operator override.

### Refresh triggers

1. Cron (e.g. daily per project)
2. Agent Host image / CLI version change
3. Connection enroll / rotate
4. Operator “Refresh models” in Settings
5. Adapter version bump

### Stale policy

- `now > evidence_expires_at` ⇒ offer not eligible for **new** decisions
- Auth capability matrix expiry still independently gates the whole runtime/auth_mode

---

## 6. Resolution algorithm (dispatcher)

Runs **once per wake / claim turn** (not once per session):

```text
1. Classify task_class (pin > derive > default feature_impl)
2. Load routing_policy + latest catalog for project
3. EligibleRuntimes =
     policy.allowed_runtimes
     ∩ runtimes with non-denied provider-auth capability
     ∩ runtimes with usable execution_connection for requesting user
     ∩ capability/tool requirements
4. Score EligibleRuntimes (pins, lane bias, tally, review diversity, quality feedback)
5. Pick runtime (fail closed if empty → no_eligible_runtime)
6. tier, effort ← task model_policy > task_class_defaults > risk/budget heuristic
   (apply attempt# escalation: effort bump before tier bump)
7. model_id ← catalog_pins[runtime][tier]
   (must resolve fresh allowed offer for connection's auth_mode)
8. Attach tool_dialect + context hint from tier_budgets
9. Emit routing_decision.v1 + why_one_liner; attach to wake + claim recommendation
10. Adapter launches exactly that model_id/effort or refuses (runner_enforced)
```

Escalation on failure (same connection):

```text
effort bump → tier bump → emit exhausted_signal=no_more_failover → human
runtime switch only if policy.max_auto_runtime_switches > 0
             AND failure_class = capability_mismatch
             AND never on quota_exhaustion
```

---

## 7. LiteLLM boundary (normative)

| Allowed | Forbidden |
|---|---|
| Discover models for `execution_path=litellm_gateway` | Personal subscription discovery via LiteLLM |
| API/paygo connections with explicit opt-in | Silent fallback from CLI personal → gateway |
| Cost/pricing + Tally callback | Storing OAuth / auth.json / setup-tokens |
| Logical model names for platform features (`ask_plan`, narrator) | Claiming a gateway model satisfies a `personal_subscription` proof row |

LiteLLM catalog rows must set `auth_mode` ∈ policy `litellm.eligible_auth_modes` or be dropped at ingest.

---

## 8. MCP / REST surface (proposed)

| Tool / route | Purpose |
|---|---|
| `discover_project_models(project, runtime?, connection_id?)` | Trigger / return probe results |
| `get_model_catalog(project)` | Latest registry snapshot |
| `update_routing_policy(project, policy_json)` | Pins, defaults, promotion (scoped write) |
| `promote_model_offer(project, offer_id, status)` | candidate→allowed→default_for_tier |
| `record_routing_feedback(...)` | Quality signal for promotion / reliability |
| `list_provider_auth_capabilities` | Existing — gates runtime eligibility |
| `claim_next` / wake | Include `recommendation` = routing_decision summary |

Wake / claim recommendation shape (extends today’s `model_tier`):

```json
{
  "recommendation": {
    "runtime": "claude-code",
    "model_tier": "balanced",
    "effort": "high",
    "model_id": "claude-sonnet-5",
    "offer_id": "offer/…",
    "execution_connection_id": "execconn/…",
    "execution_path": "native_cli",
    "tool_dialect": "full",
    "enforcement": "advisory",
    "why_one_liner": "Claude Code · balanced (Sonnet 5 @ high) — feature_impl, budget ok",
    "reason": "…"
  }
}
```

---

## 9. Enforcement ladder

| Mode | Behavior |
|---|---|
| `advisory` | Recommend; record drift if actual model differs |
| `claim_gate` | Refuse claim if agent/host cannot satisfy runtime+tier/model |
| `runner_enforced` | Host launches configured model; block if unavailable |

Ship: advisory → claim_gate → runner_enforced per runtime as adapters mature.

---

## 10. Tally dimensions (required on spend)

Every coding-agent spend row should carry:

`project, task_id, claim_id, runtime, auth_mode, execution_path, model_id, model_tier, effort, task_class, tool_dialect?, offer_id?, decision_id?, verified_outcome?`

Used later for reliability-weighted runtime scoring — never to bypass allowlists.

---

## 11. Acceptance criteria

1. Discovering Claude personal models never creates LiteLLM offers, and vice versa.
2. Pin to unknown/stale/blocked offer ⇒ dispatch `pin_unresolved`, no silent substitute.
3. Quota exhaustion pauses that connection’s runtime; does not select another provider; emits `no_more_failover` when escalation is exhausted.
4. New vendor model appears as `candidate` within one refresh; does not become tier default without promote.
5. `routing_decision` is persisted per turn and listed beside coordinator decisions; UI can show `why_one_liner`.
6. Three CLI adapters each implement `discover_models` with `verification=probed` or documented short-TTL fallback.
7. LiteLLM discovery works only for API/gateway connections.
8. Autopilot dogfood: arm deliverable → each task wake carries full recommendation tuple → adapter launch matches tuple or fails loud.
9. `cheap` / concise dialect does not receive the full tool schema by default.
10. Routing feedback (launch/schema/empty failures) is recorded and available to promotion scoring.

---

## 12. Suggested board split

| Task | Lane | Delivers |
|---|---|---|
| ADAPTER-MCatalog-1 | ADAPTER | Schemas + store for catalog/offers/pins |
| ADAPTER-MCatalog-2 | ADAPTER | `discover_models` for Codex / Claude / Cursor CLIs |
| ADAPTER-MCatalog-3 | ADAPTER | LiteLLM discover path (API-only) + ingest guards |
| DISPATCH / COORD | COORD | Per-turn resolver → `routing_decision` on wake/`claim_next` |
| ADAPTER / COORD | COORD | `routing_feedback` ingest + escalation exhausted signal |
| UI-Settings | UI | Catalog browser, promote, refresh, pin editor, why surface |
| Tally | TALLY | Persist tier/effort/task_class/decision_id on spend |

---

## 13. Relation to Autopilot deliverables

- **MVP (Codex-first):** `allowed_runtimes: ["codex"]`; same schemas; Claude/Cursor rows may exist as non-eligible until connections qualify.
- **Provider expansion:** flip eligibility when personal/API connections are supported; do not redesign routing.
- Coordinator T1/T4 attaches the recommendation as dispatch metadata; adapters enforce launch.
