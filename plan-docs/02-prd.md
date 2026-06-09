# PRD — TEEP Barnett Gas Release Triage Agent

**Audience:** Internal Taikun review
**Version:** 0.1 (Draft)
**Owner:** Steve Ritter, Taikun
**Customer sponsor:** Clovis (operations), Darko Jankovic (engineering / API)
**Date:** 2026-05-18
**Status:** Pre-customer-review

---

## 1. Background

TotalEnergies E&P Barnett (TEEP Barnett) operates methane-leak detection across its Barnett shale assets using **Nubo Sensirion** sensors. Today, all event triage is manual: the 5-person MRO team (Kolby, Billy, Lance, Kaleb, Devin) reconciles each Sensirion alert against four other systems by hand, and a single HSE coordinator (Sierra) manually transcribes resolutions into Excel for monthly regulatory reporting.

Taikun's pilot scope is to deploy a **Gas Release Triage Agent** on ActionEngine that replaces this manual workflow with an autonomous AI pipeline.

## 2. Problem statement

**Engineering problem:** Reduce time-to-acknowledge and emissions-per-event for methane releases.

**Operational problem:** Eliminate manual cross-system lookup and manual report transcription.

## 3. Goals & non-goals

### 3.0 Two parallel automation tracks

The call surfaced two manual processes; **both are in Phase 1 scope**. They share the `emissions.alerts` data model.

| Track | What it automates | Today's pain | Current build state |
|---|---|---|---|
| **A — Alarm triage** | Cross-system enrichment of each Sensirion alert + classification + dispatch | Devin opens 5 systems per alert; calls operator when stuck | **Already on `main`** — Maxwell advisor (runs on demo VM). Needs live TEEP API connections. |
| **B — Reporting** | Live HSE/EPA dashboard fed by `emissions.alerts`; Sierra's Excel becomes one-click export | Sierra hand-keys ~230 events/month from Outlook to Excel | **Already on `main`** — `emissions.html` reporting tab (runs on demo VM). Needs Sierra's exact column mapping + one-click export. |

### 3.1 Goals (Phase 1)

| ID | Track | Goal |
|---|---|---|
| G1 | A | Replace 5.2-hr Sensirion email delay with direct webhook (≤10 min from kg/h threshold to classified) |
| G2 | A | Auto-classify every Sensirion event into one of: `Process Emissions`, `Unexpected`, `Undetected` — Sierra's exact 3-value enum from `emissions.alerts.resolution_type` |
| G3 | A | Auto-close `Process Emissions` events without human touch — target 40% of total volume in Phase 1 |
| G4 | A | When dispatch needed, create a TaskHub task that asks the lease operator to confirm/repair; track operator response and update classification |
| G5 | B | Live HSE/EPA reporting view sourced from `emissions.alerts` — replace Sierra's Excel transcription entirely |
| G6 | B | One-click monthly export matching Sierra's existing Excel template column-for-column (22 columns; all map cleanly to `emissions.alerts` — see [sierra-xlsx-analysis.md](sierra-xlsx-analysis.md)) |
| G7 | A+B | Single dashboard for Devin (Track A) and Sierra (Track B) — different tabs of the same app |
| G8 | A | Connect Maxwell's enrichment fan-out to live TEEP APIs (Cygnet, ProCount, Carte, TaskHub) — currently it only reads imported historical data. WellView is **not** in scope (0/168 mentions in real notes). |

### 3.2 Non-goals (Phase 1)

| ID | Non-goal | Why |
|---|---|---|
| NG1 | Automated phone/SMS outreach to lease operators | Phase 2. Phase 1 surfaces dispatch needs via TaskHub task + dashboard. |
| NG2 | Replacing Sensirion alarms or dashboards | We sit alongside, not in front. Sensirion remains source of truth for detection. |
| NG3 | Multi-region rollout (other TotalEnergies basins) | TEEP Barnett first. Multi-tenant features come from ActionEngine platform, not this PRD. |
| NG4 | Customer-facing emissions quantification model | Sensirion does this; we consume their kg/hr output. |
| NG5 | Predictive / pre-event detection (anomaly hunting in Cygnet) | Possible Phase 3. Out of scope for Phase 1. |
| NG6 | Rebuilding the reporting UI from scratch | Already exists on `main`. We extend it, not replace it. |
| NG7 | Replacing IFS Merrick (ProCount/Carte) or Cygnet | We read from them, not replace them. |

## 4. Users & personas

| Persona | Today's pain | What the agent delivers |
|---|---|---|
| **MRO team** (Kolby × 67 events, Billy × 38, Lance × 37, Kaleb × 32, Devin × 5) | Scrolls Sensirion dashboard, opens 4 systems per event, calls LO | Live triage dashboard; Maxwell has already done the lookups; they review and approve |
| **Sierra (HSE coordinator)** | Transcribes Outlook emails into Excel monthly | Live dashboard with her exact 22-column format; report is a click |
| **Clovis (ops lead)** | Reviews Sierra's spreadsheet quarterly | Real-time KPIs; quarterly review becomes operational dashboard |
| **Michelle (TaskHub / systems)** | Owns TaskHub + Sensirion integration | Provides TaskHub read API + write API (POST/PATCH dispatch tasks) — **required Phase 1 for close-the-loop** |
| **Mike (SCADA)** | Owns Cygnet | Provides Cygnet read API |
| **Owner TBD — production accounting** | Owns ProCount + Carte (IFS Merrick) | Provides ProCount API spec (and Carte if separate) |
| **Darko (engineering / governance)** | Owns API gateway and security policy | Receives concrete API specs; governs auth, logging, residency |
| **Lease Operators** (LO — field) | Drives to pad on field-clear events; submits TaskHub notes + work orders | Receives TaskHub dispatch tasks; same flow as today but agent-initiated |

## 5. Functional requirements

### 5.1 Event ingestion

| ID | Requirement |
|---|---|
| FR-1 | Agent SHALL ingest Sensirion events via webhook OR polling (whichever Total exposes). Webhook preferred. |
| FR-2 | Polling, if used, SHALL run no faster than Sensirion's underlying device cadence (TBD — TEEP to confirm with Nubo). |
| FR-3 | Trigger threshold: event qualifies for triage when Sensirion-calculated **kg/hr ≥ X** (X to be set by TEEP — default 1 kg/hr). |
| FR-4 | Agent SHALL deduplicate repeated alerts for the same active event (correlate by device + location + open time window). |

### 5.2 Cross-system enrichment

| ID | Requirement |
|---|---|
| FR-5 | For each event, agent SHALL query **Cygnet** for tubing/line/casing pressure, sales rate, and compressor metrics over `[event_start - 4h, event_start + 1h]`. |
| FR-6 | For each event, agent SHALL query **ProCount** for active down/up codes + operator comments on the affected well over `[event_start - 4h, event_start + 1h]`. |
| FR-7 | For each event, agent SHALL query **Carte** for injection-rate drops on the affected well over the same window. (If TEEP can serve injection data through the ProCount API, Carte API is not required.) |
| FR-8 | For each event, agent SHALL query **TaskHub** for lease-operator notes and scheduled tasks on the pad within `[event_start - 2h, event_start + 4h]`. |
| FR-9 | Agent SHALL record every enrichment lookup (system, endpoint, request/response hash, latency) in the `emissions.event_audit` table. |

### 5.3 Classification

The agent uses the **existing `emissions.alerts.resolution_type` enum** from Sierra's xlsx — no new classifications.

| ID | Requirement |
|---|---|
| FR-10 | Agent SHALL classify each event into exactly one of Sierra's existing types: `Process Emissions`, `Unexpected`, `Undetected`. |
| FR-11 | Classification SHALL be deterministic (rule cascade keyed off real `how_cleared` patterns) where possible. The existing Maxwell LLM is invoked when rule confidence < 0.70 and TaskHub free-text exists. |
| FR-12 | Agent SHALL assign a confidence score (0–1). Below 0.65 → `Undetected` → MRO review. |
| FR-13 | Agent SHALL populate `equipment` and `equipment_component` from evidence using Sierra's existing 7-value vocab (`Process Emissions`, `Tank`, `Compressor Scrubbers`, `Compressor Cooler`, `Separator`, `Wellhead`, `Pipeline- Third Party`). |
| FR-14 | Agent SHALL populate `epa_identifier` from Sierra's existing vocab (`Process Emissions`, `PRV`, `Valve-C`, `Other`, `Open Ended Line`, `Valve`). |
| FR-15 | Each classification SHALL include a human-readable rationale citing the specific data points (written to `emissions.alerts.classification_rationale` JSONB). |

### 5.4 Action — close the loop

The agent does not stop at classification. It **acts on the decision** and **prepares the alert for monthly reporting in real time**. See [03-architecture.md §0](03-architecture.md) for the full lifecycle.

| ID | Phase | Requirement |
|---|---|---|
| FR-16 | 1. Detect | On Sensirion webhook, agent SHALL `INSERT emissions.alerts` with `status='Open'`, emission_id, pad, kg/h, emission_start, `email_received=NOW` — skipping the 5.2-hr email delay. |
| FR-17 | 2. Investigate | Agent SHALL log every TEEP API call to `emissions.event_audit` (system, endpoint, request/response hash, latency, rationale_role). |
| FR-18 | 3. Reason | Agent SHALL `UPDATE emissions.alerts` with `problem_identified` (Maxwell's reasoning text), `classification_rationale` (JSONB with cited evidence), `resolution_personnel='Maxwell AI'`. |
| FR-19 | 4. Decide | Maxwell SHALL emit a `TriageClassification` (real_leak, false_alarm, thief_hatch, equipment_issue, needs_inspection, process_emission) + `recommended_action` (dispatch_crew, office_resolve, monitor, investigate). The agent SHALL map these to Sierra's `resolution_type` (Process Emissions / Unexpected / Undetected) — see [03-architecture.md §0.1](03-architecture.md). |
| FR-20 | 5a. Auto-close | When Maxwell returns `process_emission` or `false_alarm` with confidence ≥ 0.85, the agent SHALL `UPDATE emissions.alerts` with `how_cleared` (standardized template matching Sierra's actual Jan-2026 text), `resolution_date=NOW`, `cleared_location='Office'`, `status='Closed'`, `resolution='sent email to close out alert'`, and SHALL send Sierra's standard closeout email. **No human touch.** |
| FR-21 | 5b. Dispatch | When Maxwell returns `real_leak`, `thief_hatch`, or `equipment_issue` with `recommended_action='dispatch_crew'`, the agent SHALL `POST` a TaskHub task containing the full evidence pack (kg/h, Maxwell rationale, linked emission_id, pad/well, similar prior events). The agent SHALL `UPDATE emissions.alerts` with `cleared_location='Field'`, `status='Open'` (awaiting LO field action). |
| FR-22 | 5c. Escalate | When Maxwell returns `needs_inspection` or confidence < 0.65, the agent SHALL `UPDATE emissions.alerts.status='In Review'` and surface the alert in the MRO advisor queue with the full decision trace. |
| FR-23 | 6. Monitor | For dispatched alerts, the agent SHALL subscribe to TaskHub task-updated webhook (or poll every 5 min) AND poll Sensirion sensor for return to baseline. |
| FR-24 | 7. Close-out (field) | When the LO marks the TaskHub task done **AND** the Sensirion sensor has returned below threshold **OR** a 24-hour timeout fires: the agent SHALL read the final LO notes from TaskHub, map findings to Sierra's columns (`problem_identified` ← LO note, `equipment`, `equipment_component`, `thief_hatches_open/_repaired/_replaced`), set `how_cleared='The alerts was cleared with a visit to the field.'`, `resolution_date=NOW`, `resolution_personnel`=LO name, `status='Closed'`, AND `PATCH` the TaskHub task to closed. |
| FR-25 | 7. Close-out (escalated) | For `In Review` alerts manually classified by MRO, the agent SHALL accept the manual classification, populate any missing Sierra columns from the captured evidence, and close the alert. |
| FR-26 | Vocabularies | Agent SHALL use Sierra's existing controlled vocabularies when populating fields: `resolution_type` (3 values), `equipment` (7 values), `equipment_component` (10 values per real xlsx), `epa_identifier` (6 values). |

### 5.5 Reporting

The end-of-month report is **a by-product** of the close-the-loop work above — every column is already populated when the month ends. No transcription, no email forwarding, no manual mapping.

| ID | Requirement |
|---|---|
| FR-27 | Every closed event SHALL have all 22 Sierra columns populated in `emissions.alerts` by the close-the-loop steps (FR-16 through FR-25). See [sierra-xlsx-analysis.md](sierra-xlsx-analysis.md) for the column-by-column mapping. |
| FR-28 | Excel export SHALL match Sierra's exact column headers including her verbose names (`Emissions Rate per Email Notification (kg/h)`, `Was the Alert Cleared In Office or In Field?`, etc.) so it's a drop-in replacement for her current file. |
| FR-29 | Reporting dashboard SHALL allow filtering by date range, resolution_type, route, equipment, equipment_component, and cleared_location. |
| FR-30 | Monthly report SHALL be exportable as Excel **and** PDF. Excel format matches her existing template exactly (column names, order, controlled-vocab values). |
| FR-31 | Sierra SHALL be able to override / correct any auto-populated field via the UI; the override SHALL be audited in `emissions.event_audit`. |

### 5.6 Audit & observability

| ID | Requirement |
|---|---|
| FR-32 | Every external API call (read AND write) SHALL be logged to `emissions.event_audit` (system, endpoint, request/response hash, status code, latency, rationale_role, alert_id). |
| FR-33 | Decision-trace SHALL be queryable via the Maxwell Advisor UI: *"show me every API call and every state change the agent made for alert #168, including the TaskHub task it created and the LO note it consumed."* |
| FR-34 | API call failure SHALL emit an alert to a designated TEEP contact within 5 minutes (≥ 5 failures within 5 min for the same system). |
| FR-35 | Agent SHALL emit metrics: events-per-day, classification-mix, MTTA, MTTR, auto-close-rate, dispatch-to-close time, API-error-rate per system, TaskHub round-trip latency (open → close). |
| FR-36 | **Asset registry — Path A preferred.** The agent SHALL resolve any Sensirion device / Cygnet asset / ProCount well / Carte well / TaskHub pad to a canonical `asset_id` before enrichment. **Default Path A:** TEEP shares its per-system asset/well/pad catalog as source data (S3 bootstrap dumps in weeks 1-2; live reads after gateway cutover); Taikun ingests and maintains the cross-system registry server-side using the proven `asset_metadata.{assets, asset_aliases, asset_bindings}` schema (production for an existing customer). **Fallback Path B:** if IT security blocks data sharing, TEEP builds the master cross-system list and exposes `GET /v1/assets/{id}`; Taikun consumes. Contract in [`teep-api.yaml`](teep-api.yaml); details in [04-system-integrations.md §5](04-system-integrations.md). |

## 6. Non-functional requirements

| ID | NFR |
|---|---|
| NFR-1 | **Availability:** 99.5% during US Central business hours; 99.0% off-hours (Phase 1; matches existing TEEP coverage which is weekday-only). |
| NFR-2 | **Latency:** Median time from event-receipt to classification < 60 seconds. |
| NFR-3 | **Auth:** OAuth 2.0 client credentials OR mTLS; no static credentials, no shared accounts, no VPN tunnel. |
| NFR-4 | **Data residency:** Event payloads cached only for the duration of an open event (max 7 days). No long-term storage of raw upstream data outside Taikun-AWS us-east-1. Aggregated event records retained per TEEP retention policy. |
| NFR-5 | **Failure mode:** If any upstream API is unavailable, agent SHALL log the event as `Undetected` and notify Devin within 5 minutes — never silently drop. |
| NFR-6 | **Audit immutability:** Decision trace and lookup logs SHALL be append-only. |

## 7. KPIs

Baselines are grounded in the actual TEEP alert data loaded into `emissions.alerts` on the demo VM (168 alerts, 2026-01-01 → 2026-01-23):

| Metric | Real baseline (Jan 2026) | Phase 1 target | Phase 2 target |
|---|---|---|---|
| Median MTTA (event-start → acknowledged) | **5.2 hours** (312 min email delay) | < 10 min | < 5 min |
| Median emission rate | 21.4 kg/h | sensor-driven, not agent-controlled | — |
| Median resolution time after email | 16.5 hours | < 2 hours | < 1 hour |
| % office-cleared (no field visit) | **56.5%** | unchanged (ceiling) | unchanged |
| % auto-closed (no human touch) | 0% | **40%** of total events | **65%** of total events |
| Resolution-type mix (real data) | 70.2% Process Emissions / 27.4% Unexpected / 2.4% Undetected | classification accuracy ≥ 92% on labeled set | ≥ 95% |
| Daily volume | ~7.6 events/day; ~230/month | same input volume, far less manual touch | same |
| Sierra's manual transcription time | hours/month | 0 (live dashboard) | 0 |
| Devin's manual lookup time per event | ~10–20 min | < 2 min (review only) | < 1 min |

**Sample is 22 days only.** Targets above assume the Jan distribution is representative. A 12-month historical pull is in the open-items list (Q5) to validate seasonality and refine targets before customer-publication.

## 8. System dependencies

System list refined from real `how_cleared` notes across 168 alerts. **WellView dropped** (0 mentions). **ProCount + Carte added** (56 + 22 mentions). **Cygnet (not Signet)** confirmed.

| System | Vendor | Owner | Mentions in 168 notes | Phase 1 dependency |
|---|---|---|---|---|
| **Nubo Sensirion** | Sensirion AG | Michelle | 168 (origin) | Required — webhook |
| **Cygnet (SCADA)** | Weatherford | Mike | **95** | Required — read |
| **TaskHub / FMP** | TEEP internal | Michelle | **93** | Required — read + write (P1, close-the-loop dispatch); webhook from TaskHub for task-updated events |
| **ProCount** | IFS Merrick | Owner TBD (likely Michelle / production accounting) | **56** | Required — read |
| **Carte** | IFS Merrick (on ProCount) | Owner TBD | **22** | Required — read **(optional if ProCount API exposes injection data directly)** |
| ~~WellView~~ | ~~Peloton~~ | — | **0** | **Dropped from scope** |
| **Taikun ActionEngine** | Taikun | — | — | Already on `main`, running on demo VM |

### 8.1 API access — read vs write (the explicit ask to TEEP)

The agent is **not read-only**. To close the loop on the 43.5% of events that need a field visit, it must create + update + close TaskHub dispatch tasks. Write surface is scoped to one system; the other four are read-only.

| System | Read endpoints needed | Write endpoints needed | Webhook *in* (TEEP → Taikun) |
|---|---|---|---|
| Sensirion | `GET /v1/sensirion/events`, `GET /events/{id}`, `GET /devices/{id}` (poll fallback) | none — *optional* `POST /sensirion/acknowledge` if Nubo exposes it | **Yes** — `POST {taikun}/sensirion/events` when kg/h threshold crossed |
| Cygnet | `GET /v1/cygnet/assets/{id}/state`, `/series`, `/liquids-unloading` | **none** | no |
| **TaskHub / FMP** | `GET /v1/fmp/tasks`, `GET /v1/fmp/tasks/{id}` | **`POST /v1/fmp/tasks`** (create dispatch task) · **`PATCH /v1/fmp/tasks/{id}`** (add monitor notes) · **`PATCH /v1/fmp/tasks/{id}` (status=closed)** | **Yes** — `POST {taikun}/taskhub/events` on `task.updated` |
| ProCount | `GET /v1/procount/wells/{id}/codes`, `GET /v1/procount/work-orders` | **none** | no |
| Carte | `GET /v1/carte/wells/{id}/series` (optional — may route through ProCount) | **none** | no |

**Totals:** 4 system endpoints we *only read from*. **1 system** (TaskHub) where we write — **3 write endpoints + 1 inbound webhook**. No writes to Cygnet, ProCount, Carte, or Sensirion (excluding optional ack).

**Bootstrap fallback** if TaskHub write isn't ready by Phase-1 week 4: agent emails the MRO team with the evidence pack in place of creating a TaskHub task. Office-cleared 56.5% of events still benefit Day 1; field-cleared events get the email path until write turns on. Config-toggled per environment.

All writes are:
- **Idempotent** (client-supplied request IDs)
- **Audited** in `emissions.event_audit` (system, endpoint, payload hash, status, latency)
- **Rate-limited** (≤ 5/min globally — well under field-incident volume)
- **OAuth2-scoped** (separate write scope from read scope; can be granted independently)

## 9. Phasing

### Phase 0 (Already on `main` and running on the demo VM)

- `emissions.*` schema (`alerts`, `daily_notes`, `pad_baselines`, `linked_notes`) — `schema/118_emissions.sql`
- XLSX ingestion script — `scripts/ingest_emissions_xlsx.py`
- Reporting UI tab in `emissions.html` — KPIs, pad heatmap, root causes, thief hatches, route performance
- Maxwell AI advisor API — `POST /advisor/triage/{alert_id}`, `GET /advisor/queue`, `GET /advisor/insights`
- LLM context builder pulling alert + notes + pad history + similar incidents + baselines
- e2e tests — `tests/e2e/test_emissions_advisor.mjs`

No Phase-0 work required. This is the foundation for everything below.

### Phase 1 (Pilot, 6–8 weeks from API availability)

**Track A — Alarm triage automation**
- Sensirion live webhook → event ingestion (replaces 5.2-hr email delay)
- Extend Maxwell's context builder to call live TEEP APIs in parallel: Cygnet, ProCount, Carte, TaskHub
- Rule-based pre-classification (cheap, deterministic) with Maxwell LLM as fallback for ambiguous cases
- Auto-close `Process Emissions` events; surface `Unexpected` with full evidence pack

**Track B — Reporting automation**
- Extend existing `emissions.html` reporting tab with:
  - Sierra's exact HSE/EPA column set
  - One-click Excel export matching her current template
  - Live updates as events close
- Confirm column list with Sierra; map all `emissions.alerts` fields to her HSE/EPA categories

### Phase 2 (Production hardening, 4–6 weeks)

- (moved to Phase 1 — TaskHub write is required for close-the-loop and now in Phase 1 scope)
- Operator response handler — when a TaskHub task is updated, re-classify the event
- LLM-assisted free-text reconciliation (when rule-based confidence < 0.7)
- SMS / Teams notification channels for fugitive escalations

### Phase 3 (Future)

- Pre-event anomaly detection in Cygnet (predict emission before Sensirion fires)
- Multi-region rollout to other TotalEnergies basins

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| TEEP API delivery slips (no gateway yet) | High | High | Negotiate a bootstrap phase: read-only file drops (signed URLs from S3, scheduled SFTP, or sample data exports) to develop against while real APIs are built |
| Sensirion poll cadence too slow to deliver "minutes" target | Medium | High | Verify cadence with Nubo before publishing KPI commitments; renegotiate target if needed |
| Free-text TaskHub notes prevent clean classification | Medium | Medium | LLM fallback in Phase 2; until then, low-confidence → `Undetected` → human |
| TEEP security review blocks Phase 1 launch | Medium | High | Front-load security answers (doc #5); engage Darko early on auth model |
| Sierra's monthly report format changes mid-pilot | Low | Medium | Design columns as configuration, not hard-coded |
| Standardized HSE/EPA categories drift between operators | Medium | Low | Mapping table (free-text → category) reviewed by Sierra weekly during pilot |

## 11. Success criteria for pilot

The pilot is successful if, after 60 days of production traffic, all of the following are true:

1. ≥ 95% of Sensirion events are ingested without human intervention.
2. Median MTTA ≤ 15 min, measured from kg/hr threshold crossing.
3. ≥ 40% of events auto-closed as `Process Emissions` without human touch.
4. Sierra's monthly HSE report is generated entirely from the agent's data store (no Outlook transcription).
5. No API-related incident requires Darko's team to roll back access.
6. Devin and Sierra both confirm in writing they would not return to the manual process.

## 12. Open items for TEEP

| # | Question | Owner |
|---|---|---|
| Q1 | Confirm five-system list (Sensirion, Cygnet, TaskHub, ProCount, Carte) complete — already validated against 168 real notes | Devin / Michelle |
| Q2 | Confirm Sensirion device poll rate from Nubo | Michelle |
| Q3 | Confirm TaskHub write (POST/PATCH /v1/fmp/tasks) is in Phase-1 scope — required for close-the-loop (dispatch + auto-close after LO confirms). Bootstrap fallback: email MRO if write isn't ready by week 4. | Darko / Michelle |
| Q4 | Send Sierra's exact monthly report column list | Sierra |
| Q5 | Provide 6–12 months of historical events for KPI baseline | Sierra / Clovis |
| Q6 | Confirm auth model preference (OAuth2 client creds vs mTLS) | Darko |
| Q7 | Confirm where API call failure notifications should land (email? Teams?) | Darko |
| Q8 | Confirm bootstrap data delivery option if gateway is not ready in 4 weeks | Darko |
