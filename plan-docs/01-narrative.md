# TEEP Barnett — Gas Release Triage Agent

**Audience:** Internal Taikun review before customer hand-off
**Status:** Draft — pending Steve review
**Date:** 2026-05-18
**Source:** "Total Call" workshop transcript (2026-05-12) + Darko email (2026-05-14) + actual TEEP alert data already loaded into `emissions.*` schema on demo VM (168 alerts, Jan 1–23 2026)

> **Data check:** The TEEP Sensirion data is already in our `scada` DB under the `emissions` schema (already on `main` — `schema/118_emissions.sql`, runs on the VM). 168 alerts, 1,677 daily field notes, 32 pad baselines, 64 alert-linked notes. All numbers below are grounded in that real data, not estimates.
>
> **Build check:** Two big pieces already exist on `main` (running on the demo VM) and shouldn't be rebuilt:
> - **Reporting UI** — `scada/hmi-enhanced-clean/emissions.html` has a full reporting tab: KPIs, pad heatmap, root-cause chart, thief-hatch summary, route performance, timeline.
> - **AI advisor (Maxwell)** — `actionengine/engine/api/emissions_api.py` already exposes `POST /advisor/triage/{alert_id}`, `GET /advisor/queue`, `GET /advisor/insights`. It runs LLM-based classification on `emissions.alerts` + daily notes + pad history + similar incidents + clean-day baselines. The Maxwell command-center UI tab is already wired up too.
>
> Phase 1 is **not** "design and build from scratch" — it's "close the loop": connect Maxwell's enrichment fan-out to the live TEEP APIs (currently it only sees the imported historical xlsx).

## Scope — what we automate

The 2026-05-12 call surfaced **two parallel manual processes**, and both are in scope for this pilot:

| Manual process today | Owner | What we automate |
|---|---|---|
| **Track A — Alarm triage**: MRO team opens 5+ systems per Sensirion alert to figure out cause, then either closes from office or dispatches | MRO team (Kolby/Billy/Lance/Kaleb/Devin) | Maxwell AI does the cross-system fan-out in seconds; auto-closes `Process Emissions`; surfaces `Unexpected` events with full evidence; creates TaskHub dispatch tasks |
| **Track B — Reporting**: Sierra reads resolution emails out of Outlook and hand-types each event into Excel for monthly HSE / EPA reporting | Sierra / HSE | Live reporting view (already built on `main`) that pulls directly from `emissions.alerts`. Monthly report becomes one click instead of hours of transcription. |

These tracks share the same data model (`emissions.alerts`) and the same dashboard shell. They are **not separate products** — Track A writes the records that Track B reads.

---

## What we heard

TEEP Barnett currently triages methane release events by hand. The flow today:

1. Nubo Sensirion sensors detect a methane plume, run a confirmation algorithm, then send an alert email **after 4 hours** to validate multiple readings and reduce false positives. Based off the data we analyzed, the **median delay is 5.2 hours** between `emission_start` and `email_received` across 168 events (higher than the "~4 hours" cited on the call). This is probably because once an email is sent, no one reads or reacts to it instantly.

2. Devin (or another MRO team member) opens the Sensirion dashboard and starts hunting through other systems to figure out *why* the leak is occurring.

3. He cross-references the systems below — counts are **mentions in real `how_cleared` notes across 168 alerts**:

   | System | Mentions | Role |
   |---|---|---|
   | **Cygnet (SCADA)** — Weatherford CygNet | **95** | Tubing/line/casing pressure drops, sales rate, compressor metrics |
   | **TaskHub / FMP** — TEEP internal portal/apps | **93** | Lease-operator free-text notes + work orders |
   | **ProCount** — IFS Merrick prod accounting | **56** | Down/up codes, operator comments, work orders |
   | **Carte** — IFS Merrick (on top of ProCount) | **22** | Injection-rate drop confirmation |
   | **WellView** — Peloton | **0** | **Not used in emissions triage.** Raised on the call but data shows zero usage. Dropped from scope. |

   When nothing matches, user calls the lease operator.

4. Sierra receives the resolution emails and **manually transcribes and categorizes** each event into an Excel spreadsheet for monthly HSE / EPA reporting. She maps free-text notes ("tank thief hatch was open") into standardized categories (equipment, EPA identifier, HSE category).

5. Relevant teams review the spreadsheet quarterly as well as send to various groups for regulatory reporting.

Three event categories operationally (and what the data shows):

| Category | Real distribution (168 alerts) | What it is | AI handling |
|---|---|---|---|
| **Process Emissions** | 70.2% (118) | Planned activity (venting, compressor maintenance, liquids unloading) | Auto-classify, auto-close after confirmation |
| **Unexpected** | 27.4% (46) | Fugitive — real leak (separator crank-case, cooler leaks, hatch issues) | Auto-dispatch + repair task |
| **Undetected** | 2.4% (4) | Unknown — needs investigation | Escalate to Devin |

Plus a notable secondary axis from the data: **cleared_location** is *Office* in 56.5% of cases (95/168) and *Field* in 43.5% (73/168). The Office-cleared events are exactly the ones where Devin determined the cause by cross-system lookup alone — **these are the events the agent can fully automate without anyone visiting the pad.**

Third-party activity was an edge case discussed on the call. In the real data only 2 events are tagged equipment "Pipeline- Third Party" so it's <2% of volume.

## What's broken

| Pain point | Today (real data) | Cost |
|---|---|---|
| **Lag between time Sensirion detects leak and event is cleared** | Median delay from `emission_start` to `email_received` is 312 minutes across 168 events; median resolution latency after email is another 16.5 hours | Emissions accumulate during the blind window |
| **Manual cross-system lookup** | Devin opens Sensirion, Cygnet, TaskHub, ProCount, Carte — *and* often just calls the operator anyway | Engineer time, slow MTTR. ~7.6 events/day to triage |
| **Manual reporting** | Sierra hand-keys events from Outlook into Excel each month | ~230 events/month based on Jan rate |
| **No proactive detection** | Field staff are "scrolling through Sensirion to find issues" | Misses events between alerts |

Quote from Brent on the call: *"They shouldn't have to scroll endlessly to try to find issues. We should be able to have some sort of internal alert based on how we set this."*

## What we're proposing — close the loop

A **Gas Release Triage Agent** running on Taikun ActionEngine that doesn't just *recommend* — it *does the work*. Maxwell is our name for the AI triage concept (already running on `main` as a read-only advisor at `/advisor/triage/{alert_id}`); the production agent extends Maxwell to close the loop end-to-end: investigate via API → decide → act → monitor → close → prep monthly report.

### Per-alert lifecycle (what the agent writes back at each step)

| Phase | What the agent does | Where it writes |
|---|---|---|
| **1. Detect** | Sensirion webhook fires the moment kg/hr crosses threshold — skip the 5.2-hr email | `INSERT emissions.alerts` (Open, emission_id, pad, kg/hr, start) |
| **2. Investigate** | Parallel API calls to Cygnet + TaskHub + ProCount + Carte | `INSERT emissions.event_audit` per call |
| **3. Reason** | Maxwell LLM prompt over the collected evidence (existing pattern, extended with live API data) | `UPDATE emissions.alerts` (problem_identified, classification_rationale, resolution_personnel='Maxwell AI') |
| **4. Decide** | Map Maxwell's 6-class output to one of 3 paths: **solve** / **dispatch** / **escalate** | (in memory) |
| **5a. SOLVE (office)** — confident `process_emission` / `false_alarm` | Write Sierra's `how_cleared` template, mark Closed, send closeout email — same flow today, just instant | `UPDATE emissions.alerts` (cleared_location=Office, how_cleared, resolution_date, status=Closed) + SMTP |
| **5b. DISPATCH** — `real_leak` / `thief_hatch` / `equipment_issue` | **POST TaskHub task** with full evidence pack ("Pad X · 148 kg/h · likely 3rd-stage scrubber dump valve") | TaskHub API + `UPDATE emissions.alerts` (cleared_location=Field, status=Open) |
| **5c. ESCALATE** — `needs_inspection` or low confidence | Surface in MRO advisor queue with decision trace | `UPDATE emissions.alerts.status=In Review` |
| **6. Monitor** (dispatch path) | Subscribe to TaskHub task-updated OR poll every 5 min; also poll Sensirion sensor | (read-only) |
| **7. CLOSE THE LOOP** — LO marks task done + sensor back to baseline OR 24-hour timeout | Read final LO notes, map findings to Sierra's columns, **PATCH TaskHub task to closed** | `UPDATE emissions.alerts` (problem_identified ← LO note, equipment, equipment_component, thief_hatches_*, how_cleared, resolution_date, resolution_personnel=LO, status=Closed) |
| **8. Monthly report** | No action needed | Sierra's HSE/EPA export = `SELECT FROM emissions.alerts WHERE month=X` — all 22 columns pre-populated |

**The bottom line:** by month-end, every column is already filled in. Sierra clicks Export Excel. No transcription, no email forwarding, no manual category mapping. The agent solves it if it can, dispatches if it can't, waits for the human if needed, then closes the loop and writes the findings.

### Investigation stack (4 systems Devin actually uses today)

Per the real `how_cleared` notes across 168 alerts:

1. **Cygnet** (tubing/line/casing pressure drops, sales rate, compressor metrics — 95/168 mentions)
2. **TaskHub** (lease-operator free-text notes + work orders — 93/168 — **also the write target for dispatch tickets**)
3. **ProCount** (down/up codes + operator comments — 56/168)
4. **Carte** (injection-rate drop confirmation — 22/168)
5. **WellView is NOT used** (0/168 mentions). It was confirmed on the call this is for Wellbore Diagrams, not needed in this workflow. Nor do I see anyone using this system for triage in the data provided.

### Maxwell's classification → Sierra's `resolution_type`

Maxwell's existing 6-value `TriageClassification` (from `actionengine/engine/api/emissions_api.py` on `main`) maps to Sierra's 3-value `resolution_type` enum:

| Maxwell `TriageClassification` | Sierra `resolution_type` | Action |
|---|---|---|
| `process_emission`, `false_alarm` | `Process Emissions` | Solve from office, write Sierra template, close |
| `real_leak`, `thief_hatch`, `equipment_issue` | `Unexpected` | Open TaskHub task, wait for LO, close when sensor returns |
| `needs_inspection` | `Undetected` | Escalate to MRO with full trace |

Sierra's monthly report continues to use her existing 3-value enum (drop-in compatible). Maxwell's 6-value taxonomy is internal — it gives the agent finer-grained reasoning + action selection without changing her report format.

## The 5.2-hour win

The single biggest, easiest win is replacing the Sensirion email with a direct API poll. The actual median delay across 168 events is **312 minutes (5.2 hours)** between `emission_start` and `email_received` — worse than the "four hours" cited on the call. From the call:

> *"That four-hour delay and the stuff coming in in the evening that occurred at two o'clock in the afternoon — the guys are watching the dashboard and a lot of times they've already responded, 'no email yet' is what they'll say."*

Even before classification logic, just having a live feed cuts measurable hours off response time. We should position this as the **Phase 1 minimum**.

## KPIs we'll commit to

All baselines below come from the real 168-alert sample (Jan 1–23, 2026) sitting in `emissions.alerts` on the demo VM. Targets are now grounded:

| Metric | Real baseline | Phase 1 target | Phase 2 target |
|---|---|---|---|
| Median time from event-start → event-acknowledged | **5.2 hours** (312 min email delay) | < 10 min | < 5 min |
| Median emission rate per event | 21.4 kg/h | (rate is sensor-driven, not agent-driven) | — |
| Median resolution time after email | 16.5 hours | < 2 hours | < 1 hour |
| % events resolved from office (no field visit) | **56.5%** | — | — |
| % events auto-closed without human touch | 0% today | **40%** (subset of Process Emissions where TaskHub/Cygnet/ProCount give a clean match) | **65%** (matches today's office-cleared rate) |
| Sierra's manual transcription time | ~230 events/month hand-keyed from email | 0 (live dashboard) | 0 |
| Daily triage volume | ~7.6 events/day | same volume, far less manual touch | same |

The 56.5% office-cleared rate is the **ceiling** on full auto-close in Phase 1 — those are exactly the events Devin resolves without going to the field. We target 40% of total volume (≈ 70% of office-cleared) to leave headroom for review-required edge cases.

## Why this fits in ActionEngine

The agent is a thin set of `ActionEngineToolBase` tools plus a decision-tree prompt:

- `sensirion.get_active_event(event_id)` — reads from Sensirion-wrapped API
- `cygnet.get_pressure_series(asset, time_window)` — tubing/line/casing pressure (95/168 use)
- `cygnet.get_compressor_metrics(asset, time_window)` — compressor status, sales rate (8/168 use)
- `cygnet.get_liquids_unloading(well, time_window)` — Cygnet-computed LU events (1/168 use, optional)
- `procount.get_codes_and_comments(well, time_window)` — down/up codes + operator notes (56/168 use)
- `procount.list_work_orders(well, time_window)` — work orders submitted by LO
- `carte.get_injection_rate(well, time_window)` — injection rate series (22/168 use)
- `taskhub.get_lo_notes(pad, time_window)` — lease-operator free-text notes (93/168 use)
- `taskhub.create_task(...)` — **Phase 1 write** — create dispatch task
- `taskhub.update_task(...)` — **Phase 1 write** — append agent monitor notes
- `taskhub.close_task(...)` — **Phase 1 write** — close task after LO confirms + sensor returns
- `emissions.alerts.write(...)` — appends to existing `emissions.alerts` table on `main`

Runs on the existing AWS demo VM, Postgres backend, **and the `emissions.*` schema is already loaded on `main`** (`schema/118_emissions.sql`, runs on the VM). Customer-side, this depends on TEEP exposing:
- **Read** access to four systems (Cygnet, ProCount, Carte, TaskHub) — for the investigation phase
- **Webhook in** from Sensirion — for live event ingestion
- **Write** access on **TaskHub only** (POST create, PATCH update, PATCH close) — for the dispatch / close-the-loop phase
- **Webhook in** from TaskHub — for `task.updated` events from the LO

No writes to Cygnet, ProCount, Carte, or Sensirion. Full per-endpoint spec is in [04-system-integrations.md](04-system-integrations.md).

## Flags worth raising

1. **Darko's gating constraint** (email 2026-05-14): *"At this point we do not have an API for this purpose nor do we have API gateway platform to expose an API in a secure way (to be built) so we will have to be creative in the beginning to get everything going."* — TEEP is building the API layer from scratch. We should expect a **bootstrap phase** (maybe IP-allowlisted direct reads or signed S3 file drops) before the proper gateway lands. The integrations doc (`04-system-integrations.md`) calls out what we need at each stage.

2. **Sensirion device poll rate is unknown** — Brent raised this on the call. *"The reason it's four hours is the algorithm confirmation, not the poll rate."* — but no one knew the actual sensor cadence. We need this from Sensirion/Nubo directly to size our own polling cadence sensibly. Don't poll faster than the source updates.

3. **Cygnet confirmed** (was "Signet" in the call transcript — a Whisper artifact). The system is Weatherford CygNet SCADA Platform. Used throughout these docs.

4. **Two systems the call missed entirely: ProCount and Carte.** Both appear repeatedly in real resolution notes ("dropped in injection via ProCount/Carte", "Codes and Comments within ProCount"). Identified via web research as **IFS Merrick** products — ProCount is hydrocarbon production accounting/allocation (down/up codes, operator comments, work orders), Carte is IFS's real-time production reporting/analytics layer. Both expose IFS Foundation REST APIs (OData-based, standard HTTP methods). Owner at TEEP is likely Michelle or production accounting.

## Out of scope for Phase 1

- Automated emission **quantification** (Sensirion already does this; we consume it)
- Direct outreach to lease operators via phone (Phase 2 — start with TaskHub task creation and emails)
- Multi-tenant rollout to other TotalEnergies regions (TEEP Barnett only)
- Replacing Sensirion alarms / dashboard (we sit *next to* it, not in front of it)

## Open questions for TEEP

These should land in the email reply:

1. Confirm the five systems (Sensirion, Cygnet, TaskHub/FMP, ProCount, Carte) are the complete set — already validated against 168 real `how_cleared` notes
2. Sensirion poll frequency — can Total get this from Nubo, or should we ask directly?
3. Confirm TaskHub write (POST/PATCH) is in Phase-1 scope — required for close-the-loop dispatch on the 43.5% of events that need a field visit. Bootstrap fallback: email MRO if write isn't ready by week 4.
4. Standardized reporting columns — Sierra to send the exact column list from her current Excel so the live dashboard matches her HSE/EPA report format day one.
5. Historical data — can we get 6–12 months of past events (Sensirion alerts + how they were classified) to validate KPI baselines and train classification?
