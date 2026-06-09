# Draft Email Reply to Darko

**To:** Darko Jankovic <darko.jankovic@totalenergies.com>
**CC:** Clovis, Devin, Michelle, Sierra, Mike (the workshop attendees)
**Subject:** Re: Gas Release Triage Agent — Scope, APIs, and Security
**Status:** Draft for Steve's review — do not send

---

Hi Darko,

Thanks for the prompt note. Below are answers to each of your questions, plus pointers to the supporting documents we've prepared. Happy to walk through any of them on a call.

**Quick framing — close the loop, not just classify.** Looking at the 168 alerts you've shared and Sierra's monthly workflow, what we're building isn't a recommendation engine — it's an agent that **does the work the MRO team does today, end-to-end**, per alarm:

1. Detect (Sensirion webhook — skips the 5.2-hr email delay we see in your data)
2. Investigate via the 4 systems your team actually opens — Cygnet, TaskHub, ProCount, Carte
3. **Decide** — solve / dispatch / escalate
4. **Act** — if solvable from the office (56.5% of events today), write Sierra's standard closeout fields to our copy of `emissions.alerts` and send the closeout email — done in seconds. If it needs a field visit, **create a TaskHub dispatch task** with the full evidence pack and hand off to the LO.
5. **Monitor** the TaskHub task for LO updates + sensor return to baseline
6. **Close the loop** — read the LO's final notes from TaskHub, map them to Sierra's HSE/EPA columns, mark our alert closed, and close the TaskHub task
7. **End-of-month report** — Sierra clicks Export Excel. Every column already populated. No transcription, no email forwarding.

This means the agent needs **read + bounded write** API access, not read-only. Read on all five systems for investigation; write on **TaskHub only** to manage the dispatch task lifecycle (create / update / close). No writes to Cygnet, ProCount, Carte, or Sensirion. The full breakdown is in §2 of this email and §1 of the Security Answers doc.

The 5.2-hour median email delay we measured in your data is still the single biggest win we surface — eliminating it gets us from "event detected" to "classified and acknowledged" in minutes — but everything past step 3 above is what turns the agent from a smart helper into an actual operational replacement for the manual workflow.

**Where we are today.** Since the workshop we've moved from "API proposal" to a tested contract. The full **OpenAPI 3.1 spec** (`teep-api.yaml`, 17 endpoints + 2 webhooks across the 5 systems) is:

- **Lint-clean** under `@redocly/cli`
- **Mock-served** by Prism on our AWS test VM with all 17 smoke tests green
- **Property-tested** by Schemathesis across `examples`, `coverage`, and `fuzzing` phases
- **Driven by a 4-scenario agent simulator** (`auto-close`, `dispatch`, `monitor`, `timeout`) that replays the exact HTTP sequences the production agent will make — same code that will run against TEEP's gateway, just with the base URL pointed at the mock today

Everything below is grounded in that spec. When TEEP's gateway is ready, we point the same simulator at it and verify the contract holds in minutes — full proof of what's passed is in `TESTING-EVIDENCE.md`. This means TEEP isn't building against a moving target: the contract is written, machine-readable, and mockable today.

A few things changed in our scope after analyzing the data you sent — flagging them up front so we're aligned:

1. **The actual system stack is 5 systems, not 6.** After mining 168 real `how_cleared` resolution notes plus Sierra's full xlsx export, we found:
   - **Cygnet** (Weatherford SCADA) — 95/168 mentions. Confirmed; the call transcript said "Signet" which is a Whisper artifact.
   - **TaskHub / FMP** (TEEP internal) — 93/168 mentions. As described on the call.
   - **ProCount** (IFS Merrick · production accounting) — 56/168 mentions. Not surfaced on the call; found in resolution notes ("Codes and Comments within ProCount", "Work Orders submitted by the Lease Operator").
   - **Carte** (IFS Merrick · sits on ProCount) — 22/168 mentions. Not surfaced on the call. May be satisfiable through the ProCount API alone.
   - **WellView** — **0/168 mentions.** It came up on the call but the actual data shows it's never used for emissions triage. **Dropped from scope.**

2. **Classification and equipment vocabularies are already defined by Sierra** — we read them from her xlsx. We will use her existing values exactly:
   - `resolution_type`: `Process Emissions` (118), `Unexpected` (46), `Undetected` (4)
   - `equipment`: `Process Emissions` (catch-all), `Tank`, `Compressor Scrubbers`, `Compressor Cooler`, `Separator`, `Wellhead`, `Pipeline- Third Party`
   - `equipment_component`: `Process Emissions`, `Thief Hatch`, `Dump Valve Controller`, `PRV`, `PLM`, `Body`, `Gauge`, `Q exhaust`, `Casing Wing Valve`
   - `epa_identifier`: `Process Emissions`, `PRV`, `Valve-C`, `Other`, `Open Ended Line`, `Valve`

   No new fields needed. **All 22 columns in Sierra's xlsx map cleanly to our existing `emissions.alerts` schema** — see [sierra-xlsx-analysis.md](sierra-xlsx-analysis.md) for the column-by-column mapping.

3. **The triage workflow is also already defined by Sierra's templates.** Her "How was the Alert Cleared" text follows 14 distinct templates dominated by 4 patterns (e.g. *"The alert was cleared by viewing a drop in tubing and line pressure via Cygnet and viewing the Lease Operator Notes in TaskHub"* × 76). The agent's rule cascade is derived from those templates — it's deterministic, not LLM-dependent for the common cases.

4. **Office-cleared is the automation target** — 56.5% of events (95/168) are cleared from the office today, no field visit. These are the events fully automatable in Phase 1. Field-cleared events (43.5%) get a TaskHub dispatch task instead.

5. **Both manual processes are in scope.** Alarm triage (MRO team's manual cross-system lookup) AND reporting (Sierra's manual Excel transcription) — both addressed in Phase 1. Same `emissions.alerts` data model, same dashboard shell. Track A writes records; Track B reads them as Sierra's live HSE/EPA view.

We've put together the following package to support this conversation. Everything is attached and also in our shared workspace:

| File | What it is |
|---|---|
| **`teep-api.yaml`** | **The canonical contract** — OpenAPI 3.1 spec for all 17 endpoints + 2 webhooks. Lint-clean, mock-served, property-tested. Everything else in this package is narrative around this file. |
| **`04-system-integrations.md`** | Narrative API spec — §0 cross-cutting conventions (auth · idempotency · webhook signing · error envelope) + per-system endpoints with payload shapes. Same contract as the yaml, in English. |
| **`TESTING-EVIDENCE.md`** | Proof of what we've already tested against the spec (lint, mock, property tests, 4-scenario agent simulator) and what we flagged for review |
| **`02-prd.md`** | Phase-1 PRD — scope, KPIs, users, FR-1 to FR-35, explicit read/write API access table |
| **`03-architecture.md`** + **`architecture-overview.md`** | Full flowcharts + sequence views + a one-page overview of the system |
| **`05-security-answers.md`** | Long-form point-by-point answers to your questions below — this email is the summary, the .md goes deeper |
| **`mockups.pptx`** | 5-slide deck — title + the four UI screens, one per persona (Devin / Sierra / Darko + Mike) |
| **`01-narrative.md`** | Plain-English deep dive for non-technical readers |

## 1. Data Scope & Access Model

**Feedback on demo dataset:** Format and content are good. The xlsx imported cleanly into our `emissions.*` schema — 168 alerts, 1,677 daily notes, 32 pad baselines, 64 alert-linked notes — and the data is rich enough to ground the PRD KPIs in real numbers rather than estimates. Three observations:

- **Time window is narrow** — the imported set spans 2026-01-01 through 2026-01-23 (22 days). For seasonal pattern validation, a 6–12 month historical pull would let us refine targets confidently. Not a blocker.
- **The 4-hour Sensirion pre-event window from Cygnet is the most valuable missing context.** It's not in the current xlsx; we need it via the live API to detect signatures like "sep_p < line_p + flow=0" (liquids unloading) ahead of the email.
- **Cross-system asset identifiers are the biggest engineering pain point** — Sensirion device ↔ Cygnet asset ↔ TaskHub pad ↔ ProCount well. Sierra's `Pad Code` numeric field (e.g. `906003`) is likely the bridging key already. A TEEP-published asset registry would resolve this cleanly (see §5 of the Integrations doc).

**Read + bounded write (TaskHub only) — Phase 1.** The agent's value depends on **closing the loop**: investigate, decide, then act. For the 56% of events that resolve from the office, the agent writes Sierra's close-out fields directly to `emissions.alerts` and sends her standard closeout email — done. For the 43% that need a field visit, the agent must create a TaskHub dispatch task with the full evidence pack and later close it once the LO confirms. Without TaskHub write, the agent is reduced to a recommendation engine for the field-cleared half of events.

Phase-1 write surface is narrow:
- `POST /v1/fmp/tasks` — create dispatch task with evidence pack
- `PATCH /v1/fmp/tasks/{id}` — add monitor-phase notes
- `PATCH /v1/fmp/tasks/{id}` (status=closed) — close once LO confirms + sensor returns to baseline

No writes to Cygnet, ProCount, Carte, or Sensirion. All writes carry an `Idempotency-Key` header (UUIDv4, replayed on retry — see §4), are audited with a `trace_id` per call, and rate-limited (≤ 5/min globally).

**Bootstrap fallback** if TaskHub write isn't ready by week 4: agent emails MRO instead of creating a TaskHub task, with the same evidence pack in the email body. We lose automatic close-out and monthly-report population for field events until write is enabled, but the office-cleared 56.5% benefits fully on Day 1.

**Phase-1 minimum dataset:** Defined in detail in the Integrations doc. Five systems:

- **Sensirion**: event-level kg/hr crossings + per-event time series + device→pad mapping (webhook preferred)
- **Cygnet**: tubing/line/casing pressure, sales rate, compressor metrics (latest + 4h pre-event window) — these are the exact tags cited in 95 of 168 real resolution notes
- **TaskHub / FMP**: lease-operator notes around `emission_start` + scheduled tasks + work orders
- **ProCount**: down/up codes + operator comments + work orders submitted by LO
- **Carte**: injection-rate drops (optional — may be served through ProCount API)

## 2. API Access — read AND write (TaskHub only)

Your original note assumed read-only. After working through the close-the-loop flow above we need a small amount of write access, scoped tightly. Here's the explicit ask per system:

| System | Read | Write | Why write is needed |
|---|---|---|---|
| **Nubo Sensirion** | webhook in (event-level kg/h, PPM, kg/h series) + poll fallback | (none — optional `acknowledge_alert` if Nubo supports it) | We consume events; we don't write back to the sensor platform |
| **Cygnet (SCADA)** | tubing / line / casing pressure, sales rate, compressor metrics, optional LU events | **none** | We only read SCADA for evidence |
| **TaskHub / FMP** | LO notes, scheduled tasks, work orders | **`POST` create dispatch task · `PATCH` add monitor notes · `PATCH` close task · webhook in for task-updated** | This is where we close the loop — the agent hands off to the LO via TaskHub, watches the task, then closes it when the LO confirms field work done. Without write, the agent can't act on the 43.5% of events that need a field visit. |
| **ProCount** | down/up codes, operator comments, work orders | **none** | We only read codes/comments for evidence |
| **Carte** | injection rate series | **none** | Read-only (and optional — may be satisfiable through ProCount API) |

**Total write surface:** **3 endpoints on TaskHub** (`POST`, `PATCH` notes, `PATCH` close) + **1 inbound webhook** to Taikun for task-updated events. No writes to four of the five systems.

**Bootstrap fallback** if TaskHub write isn't ready by week 4: the agent emails the MRO team with the evidence pack instead of creating a TaskHub task. We lose automatic close-out and monthly-report population for field events until write is enabled, but the 56.5% office-cleared volume still benefits Day 1. Email-fallback can be configured per environment.

Full per-endpoint spec — body shapes, idempotency keys, rate limits — is in §3 of the Integrations doc.

## 3. API Design

**Curated service layer, not DB-as-API.** Strongly agree with your position. The endpoint specs we're proposing are domain-oriented (e.g. `GET /v1/cygnet/assets/{id}/state`), not SQL passthroughs. TEEP is free to back each one with whatever storage technology fits.

**Asset registry — two build paths, default is the lower-lift one for TEEP.** The hardest cross-system problem (Sensirion device ↔ Cygnet asset ↔ ProCount well ↔ TaskHub pad) has two implementation options, and we'd default to **Path A** because we already operate it in production for another customer. Under Path A, TEEP shares per-system asset/well/pad catalogs as source data (S3 dumps during bootstrap, live reads after gateway cutover) and Taikun ingests + maintains the registry on our side using the proven `asset_metadata.{assets, asset_aliases, asset_bindings}` schema (trigram fuzzy match + number-aware exact tail). **Path B** — TEEP builds the master cross-system list and exposes `GET /v1/assets/{id}` — is the fallback if IT security policy blocks sharing per-system catalogs as data. Path A is zero net-new build for TEEP; Path B adds one endpoint + the curation work behind it. Full detail in §5 of `04-system-integrations.md`.

**Detailed specification:** The canonical contract is **`teep-api.yaml`** — an OpenAPI 3.1 spec we've already lint-validated, mock-served (Prism), property-tested (Schemathesis · examples + coverage + fuzz phases, all 17 endpoints), and driven through a 4-scenario agent simulator. The narrative version is `04-system-integrations.md` — same contract, English prose, with §0 cross-cutting conventions pulled out as one-time work. Each per-system section also calls out the TEEP-side owner we believe is closest (Michelle for Sensirion + TaskHub, Mike for Cygnet, production accounting / TBD for ProCount + Carte, you for the gateway). Treat the spec as a strong proposal designed to be pushed back on — please mark up anything that conflicts with TEEP standards and we'll iterate against the yaml.

**Persistent vs. polling:** Hybrid.

- **Webhook (push) for Sensirion events** — TEEP POSTs to our HMAC-signed endpoint when a kg/hr threshold is crossed. This is the latency-critical path.
- **Polling (pull) for Cygnet, TaskHub, ProCount, Carte** — these are queried per-event when the agent is enriching a Sensirion alert, so volume is naturally proportional to incident rate. At the observed ~7.6 events/day across the Jan 2026 sample, that's roughly **30–40 enrichment calls/day** total across all four systems.

No long-lived connections (no WebSockets, MQTT) required.

## 4. Security & Authentication

**Auth: OAuth 2.0 client credentials grant** is our preferred mechanism. **mTLS** is equally acceptable. API key + IP allowlist is acceptable as a temporary bootstrap-phase fallback but not for production — fully aligned with your "no static credentials" directive.

**Gateway-only consumption:** Yes. All Taikun outbound calls target your gateway. We do not request any direct DB or system endpoints.

**No static creds, no shared accounts, no VPN:** Acknowledged and accepted.

**API conventions baked into the spec.** Beyond the auth question, here's the operational machinery the OpenAPI contract encodes — one-time work for TEEP, applies to every endpoint. Full detail in §0 of `04-system-integrations.md`:

| Convention | What it is | Why it matters |
|---|---|---|
| **`Idempotency-Key`** header on every write | UUIDv4 generated by Taikun, replayed verbatim on retry; TEEP returns the original response for 24 h | Prevents duplicate TaskHub dispatch tasks if a write retries on a transient 5xx |
| **HMAC-SHA256 webhook signing** — `X-TEEP-Signature: sha256=<hex>` + `X-TEEP-Timestamp: <iso>` | Signed body, ±5 min skew window | Tamper-evident and replay-protected delivery for Sensirion + TaskHub webhooks |
| **RFC 7807 `application/problem+json`** error envelope | `type` · `title` · `status` · `detail` · `instance` · `trace_id` on every non-2xx | Machine-readable failure modes; one `trace_id` per call correlates against your gateway logs |
| **Standard status codes** | 400 / 401 / 403 / 422 / 429 / 5xx | Retry policy is deterministic — no guessing what "500-ish" means |

These four conventions are the difference between an OpenAPI doc that says "GET /v1/cygnet" and a contract you can actually operate. They're also why the spec is testable end-to-end before TEEP writes a single line of gateway code.

## 5. Data Protection & Residency

**Caching:** Yes, time-bounded and minimal. Sensirion event series cached for the event lifetime + 7 days. Cygnet snapshots, TaskHub LO notes, ProCount codes, and Carte injection series cached 90 days for decision-trace audit. Aggregated event records retained 3 years (configurable to match your retention policy). Full detail in the Security Answers doc.

**Caching can be disabled** — every TTL is config; we can run zero-cache if you require, with ~5× the gateway request volume as a consequence.

**Logs:** Metadata only — endpoint, status, latency, request/response hash. **No raw response bodies** are written to logs. 90-day live retention, then 2 years in Glacier.

**Residency:** All processing stays in AWS us-east-1, Taikun-owned account. No multi-region replication. No third-party analytics platform receives TEEP data.

## 6. Observability & Incident Handling

**Tracking:** Every API call emits a metric (`teep_api_calls_total`, `teep_api_latency_ms`, `teep_api_errors_total`) and writes an immutable audit row tagged with the `trace_id` returned in the response envelope — so any failure can be correlated 1:1 with TEEP's gateway logs by a single ID. Per-event decision traces are searchable in the agent dashboard (Screen B in `mockups.pptx` shows the full read/reason/write trace for one event).

**Failure notification:** When ≥ 5 failures hit the same system within 5 minutes, the designated TEEP contact is alerted via email (default) or PagerDuty / Opsgenie / Teams if you provide an integration key. Open question: who's the right contact and channel on your side?

For single transient failures, we retry with jittered backoff and don't notify. For auth failures (401/403), we stop, rotate, retry once, and alert on persistent failure.

If a system is unavailable for >15 minutes, in-flight events are flagged `Undetected`, MRO is notified that automated triage is degraded, and the reporting dashboard stays available so Sierra's work isn't blocked. **We never silently drop events or invent classifications.**

---

## One ask back: bootstrap phase

You called out that there's no API gateway yet and we'll need to "be creative in the beginning." We'd suggest the following bootstrap approach so development isn't blocked by gateway delivery:

**TEEP places scheduled JSON dumps in an S3 bucket** (Sensirion events, Cygnet snapshots, TaskHub tasks + LO notes, ProCount codes & work orders, Carte injection series) shaped exactly like the proposed REST responses. Taikun reads via signed URLs and develops against the same data shapes that the production gateway will eventually serve. Cutover from bootstrap to gateway is a config change for us — no rewrite. Note: we already have a working baseline from the xlsx export you sent — that's effectively a Bootstrap-0 mode for Sensirion alerts. Extending to the other systems on the same xlsx-or-S3 cadence would unblock end-to-end dev work immediately.

This gets us building against real data on Day 1 while your gateway team builds the proper interface in parallel. Two other options (SFTP, read-replica with views) are in §6 of the Integrations doc if S3 doesn't fit.

---

## Open items we'd like to close in the next call

A short list of items where we need a TEEP decision or confirmation. Happy to schedule 30 minutes for whoever's involved:

1. **Owners for the IFS Merrick stack (ProCount + Carte)** — both are IFS Foundation products with REST APIs. We need the TEEP-side owner (likely production accounting, not Michelle/Mike). Also confirm whether Carte's injection-rate data can be served through ProCount's API — if so, Carte API is not required.
2. **Sensirion device poll cadence** — Michelle to confirm with Nubo. Sets our event time-series sample rate.
3. **Asset registry** — who owns the cross-system ID map (Sensirion device ↔ Cygnet asset ↔ ProCount well ↔ TaskHub pad)? Static JSON is fine for bootstrap. Sierra's xlsx has the `Pad Code` numeric field (e.g. `906003`) — this is likely the bridging key already.
4. **Auth model decision** — OAuth2 client-credentials vs. mTLS preference.
5. **Failure notification contact + channel** — who gets paged when APIs degrade?
6. ✓ **Sierra's monthly Excel — received and analyzed.** All 22 columns map cleanly to `emissions.alerts`. Resolution types, equipment vocab, and EPA identifiers are all controlled — see [sierra-xlsx-analysis.md](sierra-xlsx-analysis.md) for the full mapping. *Item now closed.*
7. **Historical data access** — 6–12 months of past events for seasonal KPI baselining (current sample is 22 days from January 2026).
8. **Bootstrap mode choice** — S3 JSON drops vs. SFTP vs. read-replica while the gateway is being built. The xlsx export Sierra sent is effectively a Bootstrap-0 mode for Sensirion data; extending the pattern to the other 4 systems would unblock end-to-end dev work immediately.
9. **Confirm TaskHub write is in Phase-1 scope** — `POST` and `PATCH /v1/fmp/tasks` are required for close-the-loop dispatch (43% of events need field action). Bootstrap fallback if not ready by week 4: agent emails MRO instead. We'd prefer write ready Day 1.
10. **Confirm webhook from TaskHub** — `task.updated` events fired to Taikun so the agent can react to LO updates without polling.

Looking forward to your thoughts, and very happy to do a working session with whoever on your side is closest to each integration.

Thanks,
Steve

Steve Ritter
Taikun
steve@taikunai.com
