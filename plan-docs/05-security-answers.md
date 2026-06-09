# Security & Operational Answers

**Audience:** TEEP Barnett — Darko Jankovic, Information Security team
**Date:** 2026-05-18

Direct answers to Darko's 2026-05-14 email. References to detailed specs are in [04-system-integrations.md](04-system-integrations.md).

---

## A. Data Scope & Access Model

### A.1 Feedback on the demo dataset

> *"Was data provided in adequate format / Was the right data provided / Was enough data provided to determine the cause / Was data from the relevant time windows."*

**Retrospective complete (2026-05-18).** Sierra's xlsx was loaded into the `emissions.alerts` schema on `main` — 168 alerts + 1,677 daily notes + 64 LO notes + 32 pad baselines. Findings:

- **Format adequacy: good.** All 22 columns map cleanly to `emissions.alerts`; no schema changes needed. See [sierra-xlsx-analysis.md](sierra-xlsx-analysis.md).
- **System adequacy: 5 systems (not 6).** Real `how_cleared` notes show **Cygnet (95), TaskHub (93), ProCount (56), Carte (22)**. WellView was raised on the call but is unused (0/168). Dropped from scope.
- **Time-window adequacy: narrow.** Sample spans 2026-01-01 to 2026-01-23 (22 days). For seasonal KPI baselining we need 6–12 months — flagged in open items.
- **Asset cross-identifiers:** Sierra's `Pad Code` numeric field (e.g. `906003`) is likely the bridging key across systems. See §5 of [04-system-integrations.md](04-system-integrations.md).

### A.2 Read-only vs. write-back

**Phase 1: read + bounded write (TaskHub only).** The agent's value depends on **closing the loop** — investigate, decide, then act. To act on `Unexpected` events (real leaks, hatches, equipment issues) the agent must create a TaskHub dispatch task and later close it once the LO is done. Without that, the agent is reduced to a recommendation engine.

Phase-1 write surface is scoped narrowly: **TaskHub only**. No writes to Cygnet, ProCount, Carte, or Sensirion. Within TaskHub:

- `POST /v1/fmp/tasks` — create dispatch task with full evidence pack
- `PATCH /v1/fmp/tasks/{id}` — add monitoring notes during the dispatch wait
- `PATCH /v1/fmp/tasks/{id}` (status=closed) — close the task once LO completes field work + Sensirion sensor returns to baseline

All writes are idempotent (client-supplied request IDs), audited in `emissions.event_audit`, and rate-limited (≤ 5/min globally).

**Bootstrap fallback if TaskHub write isn't ready by pilot week 4:** the agent falls back to emailing the MRO team for dispatch cases, with the same evidence pack in the email body. We lose automatic close-out and end-of-month report population for field events until write is enabled, but the office-cleared 56.5% of events still benefit fully.

### A.3 Phase 1 minimum dataset

Defined in [04-system-integrations.md](04-system-integrations.md). Five systems:

- **Sensirion:** event-level kg/hr crossing notification + per-event PPM and kg/hr time series + device→pad mapping (webhook preferred).
- **Cygnet (SCADA):** tubing/line/casing pressure, sales rate, compressor metrics — latest snapshot + 4-hour pre-event window. *(95/168 mentions in real notes.)*
- **TaskHub / FMP:** lease-operator notes around `emission_start` + scheduled tasks + work orders. *(93/168.)*
- **ProCount (IFS Merrick):** active down/up codes + operator comments + LO-submitted work orders. *(56/168.)*
- **Carte (IFS Merrick):** injection-rate drops — optional if served through ProCount API. *(22/168.)*

Plus an asset registry mapping IDs across the five systems ([04-system-integrations.md](04-system-integrations.md) §5). Sierra's `Pad Code` numeric field is likely the bridging key.

## B. API Design

### B.1 Direct DB access wrapped as APIs, or a curated service layer?

**Curated service layer**, exactly as Darko's email proposes. We do not want SQL-over-HTTP or any pattern that exposes DB schema. The contracts in [04-system-integrations.md](04-system-integrations.md) are deliberately small, domain-oriented endpoints — TEEP is free to back them by any storage technology.

### B.2 Detailed API specification

See [04-system-integrations.md](04-system-integrations.md). All endpoints proposed there:

- JSON-over-HTTPS
- REST (no GraphQL dependency)
- Versioned at `/v1/` so we can iterate
- Idempotent reads; idempotent writes via client-supplied request IDs (TaskHub `POST`/`PATCH`)

### B.3 Persistent connections or polling?

**Both supported, preference for hybrid:**

- **Webhook (push):** Preferred for Sensirion event notifications. TEEP POSTs to a Taikun-side HTTPS endpoint signed with TEEP's gateway credentials. Cuts latency.
- **Polling (pull):** Used as the backstop for Sensirion and as the primary mode for Cygnet, TaskHub, ProCount, and Carte lookups. Polling cadence is event-driven (lookups happen *per event*, not on a clock), so volume is naturally proportional to incident rate — ~30-40 enrichment calls/day at observed ~7.6 events/day.

No long-lived persistent connections (no WebSockets, no MQTT) required for Phase 1.

## C. Security & Authentication

### C.1 Authentication mechanism

**Preferred: OAuth 2.0 client credentials grant** (RFC 6749 §4.4), with rotating short-lived access tokens.

**Also supported by Taikun: mTLS** (mutual TLS with TEEP-issued client certificate to Taikun, pinned at TEEP's gateway).

**Also supported: API key + IP allowlist** — listed as an acceptable fallback, though Darko's "no static credentials" directive suggests OAuth2 or mTLS is the right answer for production.

| Auth method | Taikun supports? | Recommend for Phase 1? |
|---|---|---|
| OAuth 2.0 client credentials | Yes | **Yes — primary** |
| mTLS | Yes | Yes — equivalent |
| API key + IP allowlist | Yes | Acceptable for bootstrap only |
| Shared user credential | **No** | Not under any circumstance |
| Static long-lived token | **No** | Not under any circumstance |
| VPN tunnel | **No** | Not under any circumstance |

### C.2 Gateway / proxy consumption

**Yes.** All Taikun API calls target the TEEP gateway endpoint. We do not request any direct DB ports, direct system endpoints, or any path that bypasses the gateway. The agent is gateway-only by design.

For webhooks from TEEP to Taikun, the inbound endpoint is exposed at:

- AWS API Gateway in our `us-east-1` account
- Backed by a Lambda receiver that verifies a TEEP-issued HMAC signature on every request
- Rate-limited, with replay protection (timestamp + nonce in the signed payload)

We can provide the specific signature scheme TEEP should implement; or adopt TEEP's preferred scheme if there's a TotalEnergies standard.

## D. Data Protection & Residency

### D.1 Caching API responses

**Yes, time-bounded and minimal:**

| Data class | Cached? | TTL | Purpose |
|---|---|---|---|
| Sensirion event metadata + series | Yes | Duration of open event, max 7 days after close | Decision trace; audit |
| Cygnet snapshots / 4h pre-event series | Yes | 90 days | Event audit + reporting context |
| TaskHub LO notes + tasks linked to event | Yes | 90 days | Decision trace |
| ProCount codes + comments | Yes | 90 days | Decision trace |
| Carte injection-rate series | Yes | 90 days | Decision trace |
| Asset registry / metadata | Yes | 24 hours | Cross-system ID resolution |
| Auth tokens | Yes | Until expiry | Standard OAuth2 |

**Aggregated event records** (classification, duration, kg, resolution) — retained per a mutually agreed retention policy. Default proposal: **3 years** in line with EPA reporting retention norms; configurable per TEEP requirement.

### D.2 Caching can be disabled or time-bounded

**Yes.** All cache TTLs above are configuration. TEEP can request:

- Hard zero-cache mode (every lookup hits TEEP gateway) — supported, will increase API call volume by ~5×
- Shorter TTLs on any specific data class
- Forced cache invalidation via a `DELETE /v1/cache/{key}` admin endpoint Taikun exposes to TEEP

### D.3 Logs and observability

API responses **are written to** Taikun's observability pipeline (CloudWatch Logs in `us-east-1`):

- **Logged:** endpoint URL, timestamp, status code, latency, request and response hash.
- **NOT logged:** response bodies (raw TEEP data is not persisted in logs).
- Log retention: 90 days, then archived to S3 Glacier for 2 years, then deleted.

Decision-trace context for each event records *which* TEEP records were consulted (by ID + hash), not the records themselves.

### D.4 Data residency

All TEEP data processed by the agent stays in **AWS us-east-1** (Taikun's tenant). No replication to other regions. No off-cloud storage. No third-party analytics platform receives TEEP data.

The agent calls Anthropic's API for the LLM fallback in Phase 2 (free-text reconciliation). Anthropic's API is `us-east-1`-resident. We will redact PII before any LLM call and the prompts/responses are not used for model training (per Anthropic's terms).

## E. Observability & Incident Handling

### E.1 Tracking API calls (success / failure / reason)

Yes — emitted as Prometheus metrics → CloudWatch:

- `teep_api_calls_total{system, endpoint, status}` — every call counted
- `teep_api_latency_ms{system, endpoint}` — p50/p95/p99
- `teep_api_errors_total{system, reason}` — error rate, with reason taxonomy (auth, timeout, 4xx, 5xx, network)

Per-call audit lives in the immutable `event_audit` table — searchable by event ID, system, time range.

### E.2 Failure notification to TEEP

When an API call fails persistently (≥ 5 failures within 5 minutes against the same system), Taikun's monitoring will notify a TEEP-designated contact via:

- **Email** (primary, default channel)
- **PagerDuty / Opsgenie** (if TEEP provides an integration key)
- **Microsoft Teams webhook** (if TEEP provides a webhook URL)

**Open item:** Darko to nominate the contact / channel.

For single transient failures (e.g. one 503), the agent retries with backoff (3 retries, jittered, max 30s); no notification sent.

For auth failures (401/403), the agent stops immediately, rotates the token, retries once, and notifies on failure. **No silent retries with potentially-revoked credentials.**

### E.3 Incident escalation

If an incident exceeds 15 minutes of API unavailability, the agent's behavior:

1. All events in flight at the time of failure are flagged `Undetected`.
2. Devin (TEEP MRO) is notified via dashboard + email that automated classification is degraded.
3. The reporting dashboard remains available — Sierra can continue to work — but new events surface as "manual review required" until the API recovers.

The agent never silently drops data and never invents a classification when source systems are unreachable.

---

## Summary table — direct answers

| Darko's question | Answer |
|---|---|
| Read or write? | **Read + bounded write on TaskHub only** — 3 write endpoints (POST create dispatch, PATCH notes, PATCH close) + 1 inbound webhook (task.updated). No writes to Cygnet, ProCount, Carte, or Sensirion. Required Phase 1 for close-the-loop dispatch on 43.5% of events. |
| Direct DB or service layer? | Service layer (curated REST), no DB exposure |
| Detailed spec? | [04-system-integrations.md](04-system-integrations.md) |
| Persistent or polling? | Hybrid — webhook for Sensirion events; polling for the rest |
| Auth? | OAuth2 client credentials primary; mTLS equivalent |
| Gateway-only consumption? | Yes |
| No static creds / shared accounts / VPN? | Acknowledged and accepted |
| Will you cache responses? | Yes, time-bounded; configurable; can be zero |
| Are responses stored in logs? | Metadata only — never full bodies. 90d retention. |
| Can caching be disabled? | Yes |
| Track API calls? | Yes (Prometheus + audit table) |
| Notify on failure? | Yes — email primary; PagerDuty/Teams if provided |
