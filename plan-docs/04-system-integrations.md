# System Integrations & API Specifications

**Audience:** TEEP Barnett engineering (Darko Jankovic et al.) + Taikun
**Date:** 2026-05-18
**Status:** Proposed — for TEEP review and pushback

---

## Overview

Phase 1 of the Gas Release Triage Agent depends on read access to **five** TEEP-side systems. This was refined from the initial 6-system inventory after analyzing 168 real resolution notes and Sierra's xlsx export:

- **WellView is dropped** — 0 mentions in 168 resolution notes and 0 in Sierra's xlsx. It was raised on the call but is not actually used in emissions triage.
- **ProCount and Carte added** — appear in 56 and 22 resolution notes respectively (both IFS Merrick).
- **Cygnet (not Signet)** — call-transcript artifact; real notes say Cygnet.

This document specifies, for each system: ownership, what the agent reads, evidence type, **proposed REST API contract** for TEEP to expose, and the **frequency it appears in actual resolution notes** so we're sizing the integration to real usage.

All APIs land behind the TEEP API gateway (per Darko 2026-05-14). No direct DB access. JSON-over-HTTPS. **OAuth 2.0 client credentials** primary, **mTLS** equivalent — no static credentials, no shared accounts, no VPN.

### System summary

| System | Vendor | Owner @ TEEP | Mentions in 168 notes | Has API today? | Phase 1 mode | Build effort |
|---|---|---|---|---|---|---|
| Nubo Sensirion | Sensirion AG | Michelle | 168 (origin) | Yes (3 APIs: PPM, location, kg/hr) | TEEP wraps & exposes subset | Wrap-and-expose |
| **Cygnet (SCADA)** | Weatherford | Mike | **95** (most-cited) | Internal CygNet API | TEEP exposes subset | Wrap-and-expose |
| **TaskHub / FMP** | TEEP internal | Michelle | **93** | Internal app, no external API | TEEP builds | Build new |
| **ProCount** | IFS Merrick | Owner TBD (likely Michelle / production accounting) | **56** | Yes — IFS Foundation REST (OData) | TEEP wraps via IFS gateway | Wrap-and-expose |
| **Carte** | IFS Merrick (sits on ProCount) | Owner TBD (same as ProCount) | **22** | Yes — shares store with ProCount | TEEP can satisfy via ProCount API | **Optional separate API** |
| ~~WellView~~ | ~~Peloton~~ | — | **0** | — | **Dropped** | None |

The good news: only **TaskHub/FMP is a real "build from scratch."** The other four are vendor products with documented integration stories; TEEP exposes subsets through their new gateway. **Carte may not even need a separate API** — its data is satisfiable from ProCount.

> **Canonical machine-readable contract:** [`teep-api.yaml`](teep-api.yaml) (OpenAPI 3.1). Every endpoint described below is in the spec, validated by `@redocly/cli` lint, mock-served by Prism, and property-tested by Schemathesis. See [TESTING-EVIDENCE.md](TESTING-EVIDENCE.md) for what passed.

---

## 0. Cross-cutting conventions

These conventions apply to **every** endpoint and webhook below. Spelled out once here; not repeated per-endpoint.

### 0.1 Authentication — OAuth2 client credentials

```
Authorization: Bearer <access_token>
```

- Token issued by TEEP's OAuth2 gateway via client-credentials grant, scoped to the Taikun client.
- Tokens rotate (default 1 h); Taikun handles refresh.
- No static API keys, no shared accounts, no IP allow-listing required.

### 0.2 Idempotency — required on every write

Every `POST` and `PATCH` request from Taikun **must** carry:

```
Idempotency-Key: <uuid-v4>
```

- The agent generates one UUID per logical write attempt and replays it on every retry of the same write.
- TEEP returns the original response (same status, same body) for a repeat of the same key within 24 h — never duplicates the underlying action.
- Without this header, retries on transient failures will create duplicate dispatch tasks.

### 0.3 Webhook signing — TEEP → Taikun

Every inbound webhook (Sensirion event, TaskHub task-updated) **must** carry:

```
X-TEEP-Signature: sha256=<hex>
X-TEEP-Timestamp: 2026-05-12T14:02:11Z
```

- `X-TEEP-Signature` = `HMAC-SHA256(shared_secret, raw_request_body)`, lower-case hex.
- `X-TEEP-Timestamp` = ISO-8601 UTC. Taikun rejects deliveries with timestamp skew > 5 min (replay protection).
- Shared secret rotates per-environment; provisioned at pilot kick-off.

### 0.4 Error envelope — RFC 7807 problem+json

Every non-2xx response carries `Content-Type: application/problem+json` with this shape:

```json
{
  "type":     "https://api.teep.totalenergies.com/errors/idempotency-conflict",
  "title":    "Idempotency-Key was reused with a different body",
  "status":   422,
  "detail":   "A prior request with this Idempotency-Key had a different body hash.",
  "instance": "/v1/fmp/tasks",
  "trace_id": "01HNXZ8Q9YJ6F7N2VRM8A3B4C5"
}
```

Standard status codes Taikun handles: **400** validation, **401** unauthenticated, **403** forbidden, **422** unprocessable (semantic / idempotency conflict), **429** rate-limited (retry-after honoured), **5xx** server (retry with exponential back-off).

### 0.5 Pagination, time format, IDs

- All timestamps: **ISO 8601 UTC** with `Z` suffix (`2026-05-12T14:02:11Z`).
- List endpoints: `?limit=100&cursor=<opaque>`; response includes `next_cursor` when more pages exist.
- IDs are opaque strings — Taikun never parses internal structure.

---

## 1. Nubo Sensirion

### 1.1 Background

Sensirion (Nubo) sensors detect methane plumes at TEEP facilities. Nubo's own SaaS:

- Runs a confirmation algorithm before alerting (cause of the ~4 hour email delay).
- Exposes three native APIs: **PPM**, **location**, **kg/hr** (per Michelle, on the 2026-05-12 call).
- Has an underlying device poll cadence that is faster than the 4 hour email — Michelle confirmed the cadence is shorter than 4h but the exact number is unknown to TEEP. **Open item: Q2 — confirm with Nubo.**

### 1.2 What the agent needs

The 4-hour email delay is *the* problem. The agent needs **event-level data as soon as Nubo computes it** (kg/hr threshold cross) — not the email, not the dashboard view.

| Need | Why |
|---|---|
| Live `kg/hr ≥ threshold` event notification | Replace the email delay |
| Per-event PPM time series | Decision-trace evidence ("PPM spiked then cleared in 3 min" → false positive) |
| Per-event kg/hr time series | Cumulative emissions for reporting |
| Device → asset/pad/well mapping | So we can cross-reference Cygnet, ProCount, Carte, and TaskHub |
| Event metadata: start_ts, location lat/long, confirmation status, calculated peak kg/hr | Reporting + classification rationale |

### 1.3 Proposed API — shapes Maxwell consumes

> **Proxy or transform — TEEP's choice.** The JSON shapes below describe what Maxwell consumes. TEEP may either **(a) proxy Nubo's existing API through the gateway** and rely on Taikun's adapter to map Nubo's native response into these shapes, or **(b) transform on the gateway side** and return these shapes directly. Path (a) is the smaller TEEP lift; (b) gives TEEP tighter control over what crosses the gateway.

TEEP exposes the following at the gateway. Implementation can proxy to Nubo, cache, or transform — Taikun does not care, as long as the contract holds at the wire.

**Notification preferred via webhook; polling acceptable as fallback.**

#### `POST {taikun_webhook}/sensirion/events`  (TEEP → Taikun)

Sent by TEEP when a Sensirion event is detected. **Signed per §0.3** (`X-TEEP-Signature` + `X-TEEP-Timestamp` headers required). Payload:

```json
{
  "event_id": "TEEP-SEN-20260512-00042",
  "device_id": "NUB-D-1234",
  "pad_id": "BARN-PAD-17",
  "asset_path": "TEEP/Barnett/Pad-17",
  "well_ids": ["BARN-W-103"],
  "lat": 33.0987,
  "lon": -97.4321,
  "start_ts": "2026-05-12T14:02:11Z",
  "kg_per_hr": 3.4,
  "kg_per_hr_threshold_crossed_at": "2026-05-12T14:02:11Z",
  "ppm_peak": 215.0,
  "nubo_confirmation_status": "confirmed",
  "nubo_event_url": "https://nubo.sensirion.com/events/abc123"
}
```

#### `GET /v1/sensirion/events?since={iso8601}&limit=100`  (poll fallback)

Returns all events with `start_ts ≥ since`. Same shape as webhook payload, in `events: [...]` array. Used by agent on reconciliation pass every 5 min as backstop.

#### `GET /v1/sensirion/events/{event_id}`  (detail)

Returns the event payload plus full time series since `start_ts`:

```json
{
  "event_id": "TEEP-SEN-20260512-00042",
  "...": "...same as above...",
  "series": {
    "kg_per_hr":  [{"ts": "...", "v": 1.2}, ...],
    "ppm":        [{"ts": "...", "v": 178.0}, ...]
  },
  "end_ts": "2026-05-12T14:42:00Z"     // null if still active
}
```

#### `GET /v1/sensirion/devices/{device_id}`  (metadata)

```json
{
  "device_id": "NUB-D-1234",
  "pad_id": "BARN-PAD-17",
  "asset_path": "TEEP/Barnett/Pad-17",
  "deployed_at": "2025-08-01",
  "device_poll_seconds": 60,
  "last_seen_ts": "2026-05-12T15:30:00Z"
}
```

### 1.4 Cadence & limits

- Webhook: at-least-once delivery; idempotent by `event_id`. Retry on Taikun 5xx for up to 24h with exponential backoff.
- Poll fallback: agent polls `GET /v1/sensirion/events?since=...` every 5 minutes.
- Detail polling: agent polls `GET /v1/sensirion/events/{id}` every 30s while event is open, every 5min once closed.

### 1.5 Open items

- **Q2:** Sensirion device poll cadence — needed to set `kg/hr` time-series sample resolution. Michelle to confirm with Nubo.
- Does Nubo offer a webhook to TEEP today, or must TEEP poll Nubo and re-emit?

---

## 2. Cygnet (SCADA)

### 2.1 Background

Cygnet — **Weatherford CygNet SCADA Platform** (acquired from Cygnet Software in 2014). It's one of the dominant US onshore SCADA platforms. The call transcript records this as "Signet" — a Whisper audio-transcription artifact; Cygnet is what appears throughout the actual `how_cleared` notes in `emissions.alerts`. Owner at TEEP: **Mike** (per Michelle's hand-off on the call).

Cygnet has documented integrations with IFS Merrick ProCount, so the Cygnet → ProCount → Carte stack at TEEP is well-trodden territory.

Internal CygNet API exists at TEEP today (the OASyS / CygNet platform has its own API). TEEP needs to expose a controlled subset behind the gateway.

### 2.2 What the agent needs

Read-only access to a specific set of tag values per asset, with both **current** and **historical** modes. **The exact tags below are derived from real `how_cleared` notes across 168 alerts** — not guessed.

| Tag (logical) | Why needed | Real-note evidence |
|---|---|---|
| **Tubing pressure** | Pressure drop is the dominant office-clear signal | *"drop in tubing and line pressure via Cygnet"* (95/168) |
| **Line pressure** | Always paired with tubing | same as above |
| **Casing pressure** | Confirms compressor-down events | *"a casing pressure drop in cygnet"* (~14/168) |
| **Sales rate** | Confirms production halt | *"a drop in sales rates via Cygnet"* (~14/168) |
| **Compressor metrics** (status, suction P, discharge P) | Compressor-down classification | *"viewing the compressor metrics in Cygnet"* (~8/168) |
| **Liquids unloading event flag** (if Cygnet computes this) | Direct LU classification | *"liquids unloading event within Cygnet"* (1/168) — uncommon but useful |

The agent does **not** need raw tag scanning, time alignment, or full SCADA history — only a 4-hour window around each Sensirion event.

### 2.3 Proposed API — shapes Maxwell consumes

> **Proxy or transform — TEEP's choice.** The JSON shapes below describe what Maxwell consumes. TEEP may either **(a) proxy the internal CygNet API through the gateway** (with a curated tag subset enforced at the gateway) and rely on Taikun's adapter to conform, or **(b) transform on the gateway side** and return these shapes directly. Path (a) is the smaller TEEP lift; both keep the curated-tag-subset guarantee.

The curated tag subset is fixed at the gateway either way — Maxwell never sees raw SCADA tags it isn't authorised for.

#### `GET /v1/cygnet/assets/{asset_id}/state`

Latest snapshot for the requested logical tags. `asset_id` is a pad or well.

```json
{
  "asset_id": "BARN-PAD-17",
  "asset_path": "TEEP/Barnett/Pad-17",
  "ts": "2026-05-12T14:05:00Z",
  "tags": {
    "tubing_pressure_psi":  18.0,
    "line_pressure_psi":    35.0,
    "casing_pressure_psi":  120.0,
    "sales_rate_mcfd":      0.0,
    "compressor_status":    "down",
    "compressor_suction_p_psi":  12.0,
    "compressor_discharge_p_psi": 165.0
  }
}
```

#### `GET /v1/cygnet/assets/{asset_id}/series?fields={csv}&from={iso}&to={iso}&step=5m`

Time series for the requested fields. `step` server-honored at `1m`, `5m`, `15m` minimum. Agent calls this once per event for the 4-hour window around `emission_start`.

```json
{
  "asset_id": "BARN-PAD-17",
  "from": "2026-05-12T10:00:00Z",
  "to":   "2026-05-12T14:05:00Z",
  "step_seconds": 300,
  "series": {
    "tubing_pressure_psi":  [{"ts":"...","v":35.0}, {"ts":"...","v":18.0}, ...],
    "line_pressure_psi":    [{"ts":"...","v":35.0}, {"ts":"...","v":22.0}, ...],
    "casing_pressure_psi":  [{"ts":"...","v":120.0}, ...],
    "sales_rate_mcfd":      [{"ts":"...","v":42.0},  {"ts":"...","v":4.0},  ...]
  }
}
```

#### `GET /v1/cygnet/liquids-unloading?asset_id={id}&since={iso}`

Optional. If Cygnet computes a "liquids unloading" detection event, expose it. Used for the 1-in-168 LU-specific resolution.

```json
{
  "events": [
    {"ts": "2026-05-12T13:20:00Z", "well_id": "BARN-W-103", "duration_min": 22}
  ]
}
```

#### `GET /v1/cygnet/assets?parent={pad_id}`

List wells under a pad, with logical→physical tag mapping.

### 2.4 Cadence & limits

- Agent calls `state` once per event (point-in-time at `start_ts`).
- Agent calls `series` once per event for the 4-hour pre-event window.
- Expected request volume: roughly **2× the daily Sensirion event count** per asset → low single digits/day per pad. Burst on incident.

### 2.5 Open items

- Logical tag → physical Cygnet point mapping: who owns this in TEEP? Suggest a YAML/JSON config maintained by Mike.
- For pads with multiple wells, do we aggregate at pad level or sum at well level? Default: serve both.
- Confirm Cygnet is the system name (not Signet) and confirm Mike as owner.

---

## 2A. ProCount

### 2A.1 Background

ProCount — **IFS Merrick ProCount** (formerly P2 ProCount, originally Merrick Systems). 25+ year history as the production accounting / allocation engine in US oil & gas. Tracks oil/gas/water volumes per well, allocates to leases for royalty, captures **down/up codes**, **operator comments**, and **work orders**. Files Texas RRC Form PR reports.

Mentioned in dozens of `how_cleared` resolution notes: *"the alert was cleared by viewing... Codes and Comments within ProCount, and viewing the Lease Operator Notes in TaskHub"* and *"a drop in injection via ProCount/Carte"*. The call did not mention ProCount; it was discovered by mining the real resolution notes.

**Vendor advantage:** IFS publishes API/integration patterns for ProCount, and TEEP likely already has a support contract. "Build new API" effort here is closer to "expose existing API through the TEEP gateway."

### 2A.2 What the agent needs

| Need | Why |
|---|---|
| Production codes (down/up reasons) by well & time window | Distinguish planned shutdowns from unplanned events |
| Operator comments associated with codes | Free-text context for classification |
| Work orders submitted from the field | Confirm planned maintenance / venting |

### 2A.3 Proposed API — shapes Maxwell consumes

> **Proxy or transform — TEEP's choice.** The JSON shapes below describe what Maxwell consumes. TEEP may either **(a) proxy the IFS Foundation REST / OData endpoint through the gateway** and rely on Taikun's adapter to map OData responses into these shapes, or **(b) transform on the gateway side** and return these shapes directly. Path (a) is the smaller TEEP lift.

#### `GET /v1/procount/wells/{well_id}/codes?from={iso}&to={iso}`

```json
{
  "well_id": "BARN-W-103",
  "codes": [
    {
      "code": "COMP_DOWN",
      "code_human": "Compressor down",
      "start_ts": "2026-01-22T08:00:00Z",
      "end_ts": "2026-01-22T16:00:00Z",
      "comment": "3rd stage scrubber liquid level. Waiting on mechanic.",
      "submitted_by": "BARN-LO-7",
      "work_order_id": "PC-WO-44812"
    }
  ]
}
```

#### `GET /v1/procount/work-orders?pad={pad}&since={iso}`

Returns work orders submitted by the lease operator in a given window.

### 2A.4 Cadence & limits

Per Sensirion event: 1 call for codes within `[event_start - 24h, event_start + 4h]`. Low volume.

### 2A.5 Open items

- Confirm ProCount is exposed to the same API gateway. Who owns the integration on TEEP side?
- Code taxonomy — list of valid `code` values; which are "planned-emission" types.

---

## 2B. Carte

### 2B.1 Background

Carte — **IFS Merrick Carte**. Sister product to ProCount in the IFS Merrick suite. Per IFS: *"Carte enables customers, from the field through the C-suite, to visualize, graph, and analyze production data by a single well, field, or across all assets. Monitor and analyze allocated data against daily and monthly production targets."*

So Carte is **the reporting/visualization layer on top of ProCount's volume data.** When Devin's resolution notes say "a drop in injection via ProCount/Carte" he is looking at Carte's chart of ProCount's allocation output.

**Practical implication for our API design:** Carte and ProCount share the same underlying data store. We could simplify the integration by reading the **fields we need from ProCount's API directly** and skipping a separate Carte API entirely. The §2B endpoints below are kept as a "logical interface" — physical implementation can route them through ProCount if that's easier for IFS to expose. **Open question for Darko/IFS:** confirm we can satisfy Carte's data needs from ProCount alone.

### 2B.2 What the agent needs

| Need | Why |
|---|---|
| Injection rate at well & time | Detect drop = compressor / well shut-in signature |
| Sales rate at meter & time | Detect drop = production interruption |

### 2B.3 Proposed API — shapes Maxwell consumes

> **Proxy or transform — TEEP's choice.** Same proxy-or-transform model as §2A. Because Carte shares ProCount's IFS Foundation REST / OData store, the lightest path is to serve Carte's data **through the ProCount adapter** — TEEP exposes one IFS endpoint, Taikun routes by domain. Decision deferred to the working session (see §2B.4 + slide-12 Q3).

#### `GET /v1/carte/wells/{well_id}/series?fields=injection_rate,sales_rate&from={iso}&to={iso}&step=15m`

Same shape as the Cygnet series endpoint.

### 2B.4 Open items

- **Q: Is Carte just a viewer for ProCount data?** If so we can drop the separate API and pull these fields from ProCount. Needs TEEP confirmation.
- If a separate system, what's the underlying data store?

---

## 3. FMP / TaskHub

### 3.1 Background

FMP is TEEP's home-built field management portal. TaskHub is the micro-app for dispatched work / tasks. Owner: **Michelle**. From the call: *"FMP is basically our portal with the micro applications, and TaskHub is one of those micro apps."* and *"90% of the information is there."*

Lease operators add free-text notes to tasks. **Free-text reconciliation is the hardest classification problem** for Phase 1 and is the primary use case for LLM-assisted classification in Phase 2.

No external API today.

### 3.2 What the agent needs

| Need | Why |
|---|---|
| Scheduled / active tasks by pad and time window | Match Sensirion events to known process emissions |
| Task type / category (liquids unloading, maintenance, compression work, etc.) | Filter to planned-emission types |
| Assigned operator + contact info | So agent can route dispatch task to correct LO |
| Free-text notes from lease operators | Maxwell context + final close-the-loop findings |
| Task status (scheduled / dispatched / in progress / closed) | Distinguish planned vs. completed |
| **Write: create / update / close tasks (Phase 1 — required)** | **Close-the-loop on `Unexpected` events: dispatch → wait → close** |
| **Webhook in: task.updated** | **Drive close-out phase without polling** |

### 3.3 Proposed API (TEEP-builds)

#### `GET /v1/fmp/tasks?pad={pad_id}&from={iso}&to={iso}&type={csv}`

Returns tasks on a pad within a time window. `type` filter accepts comma-separated task categories.

```json
{
  "tasks": [
    {
      "task_id": "FMP-T-99124",
      "pad_id": "BARN-PAD-17",
      "well_id": "BARN-W-103",
      "type": "liquids_unloading",
      "type_human": "Liquids unloading",
      "status": "in_progress",
      "scheduled_start_ts": "2026-05-12T13:30:00Z",
      "scheduled_end_ts":   "2026-05-12T15:00:00Z",
      "dispatched_to": "BARN-OP-7",
      "dispatched_to_name": "Joe Smith",
      "dispatched_to_phone": "+1...",
      "dispatched_to_email": "joe.smith@...",
      "notes_freetext": "blowing down tank thief hatch per Devin",
      "created_by": "michelle.x@...",
      "created_ts": "2026-05-12T11:00:00Z",
      "url": "https://fmp.teep.../tasks/99124"
    }
  ]
}
```

#### `GET /v1/fmp/tasks/{task_id}`

Single-task detail; same shape as above plus full update history (`updates: [{ts, actor, status, note}, ...]`).

#### `GET /v1/fmp/task-types`

Returns the canonical list of task categories and which ones are "planned emission" types. Configuration; rarely changes.

```json
{
  "types": [
    {"key": "liquids_unloading", "planned_emission": true},
    {"key": "compression_maintenance", "planned_emission": true},
    {"key": "tank_thief_hatch_inspection", "planned_emission": true},
    {"key": "well_pump_repair", "planned_emission": false},
    ...
  ]
}
```

#### `POST /v1/fmp/tasks`  *(write — required for close-the-loop dispatch)*

Creates a dispatch task when Maxwell classifies an alert as `real_leak`, `thief_hatch`, or `equipment_issue`. The task is the agent's hand-off to the LO for field action. **Idempotent per §0.2** — agent supplies `Idempotency-Key` header; replays return the original task.

```json
{
  "pad_id": "BARN-PAD-17",
  "well_id": "BARN-W-103",
  "type": "emissions_dispatch",
  "priority": "high",
  "title": "Sensirion 148 kg/h on Bewley Pad · suspected 3rd-stage scrubber dump valve",
  "body_markdown": "**Alert:** TEEP-SEN-20260512-00042\n**kg/h peak:** 148.4\n**Maxwell classification:** equipment_issue (confidence 0.89)\n\n**Evidence Maxwell collected:**\n- Cygnet: tubing pressure 35→18 psi at 07:58, sales rate ↓62%, casing pressure drop confirmed\n- ProCount: COMP_DOWN code at 07:58 — operator note '3rd stage scrubber liquid level'\n- Carte: injection rate ↓91% at 07:58\n- TaskHub: no scheduled task at start\n\n**Maxwell's recommendation:** dispatch_crew · check 3rd-stage scrubber dump valve\n\n**Similar prior:** Bewley Pad 2026-01-08 (162 kg/h, same pattern, dump valve hung open)",
  "linked_emission_id": "4b8e2c1a-606b-4548-92f2-f4bbed83d1e7",
  "linked_alert_url": "https://taikun.../emissions.html?alert=168",
  "assignee_role": "lease_operator_on_pad",
  "callback_webhook": "{taikun_webhook}/taskhub/events"
}
```

Response: `201 Created` with `task_id`, `url`, and `assigned_to` populated.

#### `PATCH /v1/fmp/tasks/{task_id}`  *(write — agent updates during monitoring)*

Used during the **monitor phase** to attach intermediate notes (e.g. "sensor still elevated, escalating"). Body is partial JSON; only included fields are updated. **Idempotent per §0.2**.

```json
{
  "agent_note": "Sensor still 12 kg/h at 11:00 — escalating to MRO",
  "status": "in_progress"
}
```

#### `PATCH /v1/fmp/tasks/{task_id}` to close  *(write — agent closes after LO confirms)*

Used in the **close-out phase** when the LO has marked the field work done AND the Sensirion sensor has returned below threshold. The agent reads the LO's final notes from the task before closing. **Idempotent per §0.2**.

```json
{
  "status": "closed",
  "agent_resolution_summary": "LO found thief hatch unlatched on tank 2. Re-latched, sensor returned to 1.8 kg/h by 13:42. emissions.alerts updated: equipment=Tank, equipment_component=Thief Hatch, thief_hatches_open=1.",
  "closed_by": "Maxwell AI",
  "closed_ts": "2026-05-12T13:55:00Z"
}
```

Alternative — TEEP may prefer the agent NOT auto-close; instead the LO closes via the TaskHub UI and a webhook fires. Either pattern works; agent listens via `callback_webhook`.

#### Webhook from TaskHub  *(read — task-updated events)*

```
POST {taikun_webhook}/taskhub/events
```

Fired by TaskHub whenever an LO updates a task created by the agent. Agent uses this to drive phase-5 (close-the-loop) without polling. **Signed per §0.3** (`X-TEEP-Signature` + `X-TEEP-Timestamp` headers required).

```json
{
  "event": "task.updated",
  "task_id": "FMP-T-99204",
  "linked_emission_id": "4b8e2c1a-606b-4548-92f2-f4bbed83d1e7",
  "status": "closed",
  "lo_notes_added": [
    {"ts": "2026-05-12T13:42:00Z", "user": "joe.smith", "note": "Found tank 2 thief hatch unlatched. Re-latched. Sensor down."}
  ],
  "work_order_id": "PC-WO-44812"
}
```

### 3.4 Cadence & limits

- Per Sensirion event: 1 list call (`GET /v1/fmp/tasks?...`) for enrichment + 1 `POST` if dispatch + 1 `PATCH` on close.
- Write rate: ≤ 5/min globally; agent will not retry a `POST`/`PATCH` that returned 4xx.
- Webhook delivery: at-least-once; agent dedupes by `task_id` + `event` + timestamp.

### 3.5 Open items

- Q3 from PRD: confirm **TaskHub write (POST/PATCH) is in Phase-1 scope** — required for close-the-loop dispatch. Bootstrap fallback: email MRO with evidence pack if write isn't ready by week 4.
- Confirm task type enum with Michelle — exact values, naming convention.
- Free-text language(s) — confirm English-only (yes per call, but confirm).

---

## ~~4. WellView (Peloton)~~ — dropped from Phase 1

Removed after analyzing real data. **0 mentions in 168 `how_cleared` notes** and **0 mentions in Sierra's xlsx**. The call surfaced WellView as a possible system Devin checks, but actual resolution notes show it's never used for emissions triage.

If a `third_party` (e.g. pipeline-third-party leak) event arises, today's process is a Field visit — the LO physically identifies the issue. We mirror that with `Unexpected` classification + dispatch task. No WellView lookup needed.

WellView remains an option for **Phase 3** if pre-event anomaly detection requires workover-schedule correlation.

---

## 5. Cross-system identifiers — two build paths

The single hardest engineering issue across these five systems is **asset identity**. Sensirion knows devices, Cygnet knows assets, ProCount/Carte know wells, TaskHub knows pads. The agent needs one canonical identifier per pad/well/lease that maps to each system's native ID.

There are two ways to build this. **We recommend Path A (Taikun-side ingest) because we already operate it in production for another customer.** Path B (TEEP-side resolver API) is the fallback if IT-security policy prevents sharing source data.

### 5.1 Path A · Taikun ingests + builds the registry — **recommended (default)**

TEEP shares its per-system asset/well/pad catalog as source data (via the bootstrap S3 dumps in §6 during weeks 1-2, then via live reads through the gateway from week 4). Taikun builds and maintains the cross-system registry on our side using the same schema that runs in production for our R2Q customer today:

```
asset_metadata.assets           -- canonical registry
  asset_id (PK) · display_name · asset_type · parent_asset_id · tenant_id
  aliases JSONB · external_ids JSONB · cohort fields (prod_band, age_band, …)

asset_metadata.asset_aliases    -- O(1) normalized name lookup
  (tenant_id, alias_norm) PK → asset_id · source · confidence · seen_count
  handles "Burns A 35" / "BURNS_A_35" / "burns-a-35" as one canonical name

asset_metadata.asset_bindings   -- cross-system ID map
  asset_id · system ('sensirion' | 'cygnet' | 'procount' | 'carte' | 'fmp')
  external_id · confidence · source

asset_metadata.asset_resolution_audit  -- replayable audit trail
```

Resolution is fuzzy with number-awareness (trigram on `display_name` + `SequenceMatcher` ≥ 0.5 threshold, exact match enforced on trailing numbers so *"Bradley Ranch 11"* never collides with *"Bradley Ranch 12"*). Hierarchy expansion handles lease → wells via `AssetHierarchyService`.

**What TEEP provides under Path A:**

| Per system | What to share | Shape |
|---|---|---|
| Sensirion | device list | `device_id · pad_id · asset_path · deployed_at · last_seen_ts` |
| Cygnet | asset catalog | `asset_id · asset_path · parent · display_name · aliases[]` |
| ProCount | well list | `well_id · pad · well_name · lease · operator` |
| Carte | well list (may dedup against ProCount) | same shape as ProCount |
| TaskHub / FMP | pad list | `pad_id · pad_name · lease · LO contact` |

A static JSON or CSV bundle per system is fine for bootstrap (week 1-2); after the gateway is live (week 4+), Taikun re-ingests on a schedule (e.g., nightly diff) so the registry stays current.

**TEEP-side build effort: zero.** Just expose the catalog data already in each system.

### 5.2 Path B · TEEP builds the registry + exposes it — fallback

If IT-security policy prevents sharing per-system catalogs as data, TEEP can build the cross-system registry on its side and expose a single resolver endpoint. Taikun becomes a thin consumer.

```
GET /v1/assets/{any_id}
→ {
    "canonical_id": "BARN-PAD-17",
    "asset_path": "TEEP/Barnett/Pad-17",
    "aliases": {
      "sensirion_device_ids": ["NUB-D-1234", "NUB-D-1235"],
      "cygnet_asset_ids": ["CYG-PAD-17"],
      "fmp_pad_ids": ["FMP-PAD-17"],
      "procount_well_ids": ["BARN-W-103", "BARN-W-104"]
    },
    "wells": [
      {"well_id": "BARN-W-103", "name": "Lease 17 #3H"},
      {"well_id": "BARN-W-104", "name": "Lease 17 #4H"}
    ],
    "lease_operator": {"name": "Joe Smith", "phone": "+1...", "email": "..."}
  }
```

Endpoint contract is in [`teep-api.yaml`](teep-api.yaml) (`GET /v1/assets/{id}`, mock-tested via Prism, smoke-tested via curl). Idempotency / auth / error envelope per §0.

**TEEP-side build effort: a new master cross-system list + 1 endpoint.** Higher lift than Path A.

### 5.3 Why Path A is the default

- **Proven runtime** — the same `asset_metadata.*` schema serves another Taikun customer's production triage today; we know it scales, the fuzzy match handles real-world name drift, and the audit table satisfies replay/regulatory requirements.
- **Decoupled from gateway delivery** — Taikun can begin ingest in week 1-2 from bootstrap data; doesn't wait for the gateway.
- **Lower TEEP lift** — TEEP just exposes existing catalog data; no new master-list curation or extra endpoint to build.
- **No vendor lock-in** — Taikun's adapter pattern means if TEEP later wants Path B, we plug the resolver in front of the same `asset_bindings` table.

Path B remains supported (the endpoint is fully specced) for environments where data-sharing isn't allowed.

---

## 6. Bootstrap phase (before gateway is ready)

Per Darko 2026-05-14, TEEP has no API gateway today and "will have to be creative in the beginning."

To unblock Taikun development against real data, we propose any of these three bootstrap options. **TEEP picks one:**

| Option | What TEEP provides | What Taikun does | Pros | Cons |
|---|---|---|---|---|
| **A. Signed S3 file drops** | TEEP places JSON dumps (Sensirion events, TaskHub tasks, Cygnet snapshots, ProCount codes & comments, Carte injection series) into an S3 bucket on a schedule. Taikun reads via signed URLs. | Polls bucket; builds against same JSON shape as future REST | Zero new infra on TEEP side; works with any auth | Latency = drop cadence; not real-time |
| **B. SFTP / managed file transfer** | TEEP exports CSV/JSON to a managed transfer location; Taikun pulls | Same as A | Familiar pattern; secure | Same as A |
| **C. Direct read of a TEEP-owned read replica** | TEEP grants IP-allowlisted read-only access to a read replica with views matching API shapes | Reads via SQL adapter that emits same JSON | Closer to real-time | Requires DB access (Darko has stated this is not permitted long-term — temporary bridge only) |

**Recommendation: Option A**. It's the lowest-risk for TEEP security-wise and gives Taikun a realistic dev environment.

The agent reads the same JSON shape regardless of source (bootstrap or production gateway), so cutover is a config change, not a rewrite.

---

## 7. Summary — what TEEP needs to provide

### 7.1 Cross-cutting (one-time, applies to every endpoint)

| Item | Reference | Owner | Required by |
|---|---|---|---|
| OAuth2 client-credentials grant + Taikun client provisioning | §0.1 | Darko | Pilot week 4 |
| `Idempotency-Key` storage + 24 h replay handling | §0.2 | Each system owner | Pilot week 4 |
| HMAC-SHA256 webhook signing (`X-TEEP-Signature` + `X-TEEP-Timestamp`) + shared-secret provisioning | §0.3 | Darko + Michelle | Pilot week 4 |
| RFC 7807 `application/problem+json` error envelope | §0.4 | Each system owner | Pilot week 4 |

### 7.2 Per-system work — grouped by effort type

Bucketed by **what TEEP actually does** rather than by endpoint count. There are **5 buckets of work** total; only one (TaskHub) is a net-new API build.

#### Bucket 1 · Proxy + curate existing vendor APIs

Vendor APIs already exist. TEEP's job is auth + gateway routing + (for Cygnet) picking a curated subset of tags. The response shapes Maxwell consumes (§1–§2B) can come from either a proxy + Taikun-side adapter, or a TEEP-side transform — TEEP's choice (see the note at the top of each section).

| System | What TEEP exposes through the gateway | Owner | Required by |
|---|---|---|---|
| Sensirion / Nubo | `POST {taikun}/sensirion/events` webhook (default) + `GET /v1/sensirion/events/{id}` + `GET /v1/sensirion/devices/{id}`. Poll fallback `GET /v1/sensirion/events?since=…` is optional backup, not both. | Michelle | Pilot week 4 |
| Cygnet (SCADA) | `GET /v1/cygnet/assets/{id}/state` · `…/series` · `…/liquids-unloading`. **Curated 6-tag subset** (tubing/line/casing pressure · sales rate · compressor metrics · LU flag). Confirm subset with Mike per slide-12 Q2. | Mike | Pilot week 4 |
| ProCount (IFS) | `GET /v1/procount/wells/{id}/codes` + `/v1/procount/work-orders`. Built on IFS Foundation REST / OData. | TBD (slide-12 Q3) | Pilot week 4 |
| Carte (IFS) | `GET /v1/carte/wells/{id}/series`. **Defer go / no-go decision to the working session** — Carte's data may be fully satisfiable through ProCount's OData. | TBD (slide-12 Q3) | Pilot week 4 |

#### Bucket 2 · Net-new API build — TaskHub / FMP

The only system without an external API today. Real build work; all other buckets are routing or proxying.

| What TEEP builds | Owner | Required by |
|---|---|---|
| `GET /v1/fmp/tasks` (list) · `GET /v1/fmp/tasks/{id}` · `POST /v1/fmp/tasks` (create) · `PATCH /v1/fmp/tasks/{id}` (notes) · `PATCH /v1/fmp/tasks/{id}` (close) · `POST {taikun}/taskhub/events` outbound webhook | Michelle | Pilot week 4 |

#### Bucket 3 · Gateway platform — one-time foundation

See **§7.1** above. Standard gateway plumbing applied to every endpoint: OAuth2, Idempotency-Key store, HMAC webhook signing, RFC 7807 errors. Not Maxwell-specific.

#### Bucket 4 · Asset registry

Default is **Path A** (Taikun ingests source data → builds the registry on our side using the proven `asset_metadata.*` schema). Path A requires zero TEEP API build — just expose per-system catalog data in the bootstrap dumps (Bucket 5).

| Path | TEEP deliverable | Owner | Required by |
|---|---|---|---|
| **Path A · default** | Include per-system asset/well/pad catalogs in the bootstrap S3 dumps (§6). No new endpoint. | Darko + each system owner | Bootstrap (weeks 1-2) |
| **Path B · conditional fallback** | `GET /v1/assets/{id}` cross-system resolver — **only required if IT-security policy blocks Path A's data sharing.** Contract in `teep-api.yaml` and §5.2. | Darko | Pilot week 4 *(if elected)* |

Confirm path with Darko per slide-12 Q4.

#### Bucket 5 · Bootstrap — before the gateway is ready

Unblocks development against real data while the gateway is built in parallel. TEEP picks one option (§6); Option A (S3 dumps) is recommended.

| What TEEP provides | Owner | Required by |
|---|---|---|
| S3 bucket + scheduled JSON dumps for the 5 systems (Sensirion, Cygnet, TaskHub/FMP, ProCount, Carte) matching §1–§4 shapes. Per-system asset catalogs included for Path A registry build. | Darko + each system owner | 2 weeks after pilot kickoff |

#### Phase-2 nice-to-haves (deferred, not Phase-1 blockers)

| Item | Note | Owner |
|---|---|---|
| `GET /v1/cygnet/assets?parent={pad_id}` | Pad-asset listing. Agent works around via the asset registry in Phase 1. | Mike |
| `GET /v1/fmp/task-types` | Task taxonomy lookup. Agent uses a fixed list in Phase 1. | Michelle |
| 6–12 month historical data | For seasonal KPI tuning. | Darko |
