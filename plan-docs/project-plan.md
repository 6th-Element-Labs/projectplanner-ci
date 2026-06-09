# Project Maxwell — TEEP Barnett · Phase-1 Pilot Project Plan

**Customer:** TotalEnergies E&P Barnett (TEEP Barnett)  
**Project:** Maxwell — AI Gas Release Triage Agent  
**Prepared by:** Taikun (Steve Ridder)  
**Date:** 2026-06-09  
**Status:** Draft for internal review → customer working session  

> **How to read this plan.** It is a *master plan covering both sides* — Taikun build work **and** the TEEP-side enablement that gates it — so it doubles as the "what we need from you" tracker. Timeline is expressed in **relative weeks (Week 0 = kickoff)** because no calendar kickoff date is fixed yet. Owners are split across **Taikun / TEEP / Sensirion-Nubo / IFS Merrick / Joint**. The companion [`project-plan.json`](project-plan.json) holds the same content as structured data — it is the backing store for the monday.com-style tracker we'll build once the content is approved.

---

## 1 · Executive summary

Project Maxwell is the close-the-loop autonomous gas-release triage agent for TEEP Barnett (TotalEnergies E&P). The strategic posture is unusually strong for a pilot: the core agent already exists and runs read-only on Taikun's AWS demo VM (emissions.* schema with 168 real Jan-2026 alerts, the Maxwell advisor API, 4 UI screens, the asset_metadata.* registry proven in R2Q production), and the integration contract (teep-api.yaml, 17 endpoints + 2 webhooks) is lint-clean, Prism-mock-served, Schemathesis property-tested, and driven by a 4-scenario agent simulator. Phase 1 is therefore not a build-from-scratch — it is "wire Maxwell to live TEEP systems and close the loop": add the write/dispatch path, federate identity, route inference through TEEP Bedrock, and source SCADA through an approved intermediary. The load-bearing architectural bet across all 12 workstreams is the signed-S3-JSON-drop bootstrap: TEEP drops data shaped byte-identically to the future REST responses, so Taikun develops and tests against real data immediately and cutover to the live gateway is a config-only base-URL flip, never a rewrite.

The work decomposes into a TEEP-build track and a Taikun-build track that are deliberately decoupled. On the TEEP side the heavy lifts are: standing up an API gateway from zero (Darko — none exists today), building the net-new FMP/TaskHub read+write API plus task.updated webhook (Sebastian — the only write target and the only system without an existing API), federating Microsoft Entra ID into the Taikun app (Sahir), enabling cross-account AWS Bedrock with Claude model access (Sahir/Darko), choosing an existing intermediary for Cygnet/SCADA since TEEP will not expose CygNet directly (Mike/Darko), and producing per-system catalog + data drops. On the Taikun side almost everything is adapter wiring and validation against the frozen contract: source-agnostic adapters, the parallel enrichment fan-out, the deterministic rule-cascade-first classifier with single-call LLM fallback, the three act paths (auto-close ~70% / TaskHub dispatch ~27% / MRO escalate ~3%) behind a shadow→human-confirm→autonomous auto-close gate, the durable 24h monitor/timeout, Sierra's one-click 22-column HSE/EPA export, and KPI/scorecard instrumentation. Two pre-built platform assets sharply de-risk two of the new asks: the hardened OIDC EntraID provider already ships (SSO is config + one net-new group→role mapping, not a SAML build), and the LiteLLM inference gateway is already live on both VMs with per-tenant routing (Bedrock is a routing-table + cross-account-IAM change plus one logical-model edit to de-hardcode gpt-4o, not a Maxwell rewrite).

The honest schedule reality is that the deck's aggressive "Phase 1 in 6-8 weeks from API availability" describes the read-systems-plus-office-auto-close slice, not full production. The new dependencies — Entra app-registration/admin-consent latency in a large enterprise tenant (1-3+ weeks, possibly gated by central TotalEnergies group IT), Bedrock per-account model-access approval, the net-new TaskHub write API (~26 Sebastian dev-days), the Cygnet-intermediary decision, and the still-unnamed IFS Merrick/ProCount owner — push realistic full-production cutover to roughly 11-15 weeks from kickoff. Crucially, none of these block starting: Taikun builds and simulator-proves the entire agent against bootstrap S3 drops and the Prism mock independently of every TEEP-side gate, and the email-to-MRO fallback keeps the field-dispatch population flowing if TaskHub write slips. The office-cleared ~56.5% of value can go live early; the field-cleared and fully-governed production posture trail behind the SSO/Bedrock/TaskHub-write gates.

The critical path runs through the gateway/bootstrap foundation (S3 bucket + drop spec + first drops), the ProCount canonical asset spine that every other system binds onto, the Sensirion live event path (the MTTA KPI lives or dies here), the TaskHub write API + idempotency soak (the one place a bug creates a duplicate real-world field dispatch), and the SSO/Bedrock production gates. The single biggest measurement caveat is that all headline KPIs (MTTA 5.2h→<10min, 0→40% auto-close, ≥92% accuracy, ~70t CH4 avoided) are extrapolated from a 22-day, 168-alert sample; the 6-12 month historical pull and the 60-day operate window are what convert aspiration into evidence. Success is gated on six signed criteria measured after 60 days of production traffic, culminating in a Phase-1→Phase-2 go/no-go with written sign-off from Devin and Sierra.

Net: the engineering is largely de-risked and front-loadable; the schedule risk is almost entirely TEEP-side enterprise process latency (Entra consent, Bedrock enablement, gateway stand-up, an unnamed IFS owner) plus the one net-new API. The program management priority is to front-load every cross-org decision and long-lead approval into the kickoff week, run the two tracks in parallel against the bootstrap, and protect the config-only-cutover invariant so the live wire-up is verification, not construction.

### Plan at a glance

| | |
|---|---|
| Workstreams | **12** |
| Discrete tasks | **171** |
| Estimated effort (all parties) | **~485 person-days** |
| Phases | Kickoff → Bootstrap → Build → Cutover → Operate |
| Milestones (gated) | 8 |
| Critical-path tasks | 15 |
| Kickoff decisions to close | 11 |

**Effort by owner** (person-days):

| Owner | Days |
|---|--:|
| Taikun | 252.5 |
| Joint | 140.5 |
| TEEP | 88 |
| Sensirion/Nubo | 2 |
| IFS Merrick | 2 |

**Effort by phase** (person-days):

| Phase | Days |
|---|--:|
| Kickoff | 34.5 |
| Bootstrap | 81.5 |
| Build | 250.5 |
| Cutover | 53.5 |
| Operate | 65.0 |

---

## 2 · Timeline & realistic duration

The deck's "Phase 1 in 6-8 weeks from API availability" is optimistic and, more importantly, mis-scoped against the post-call asks. It is achievable only for the narrow slice "read systems live + office auto-close path running on bootstrap/early-gateway data via the Taikun-managed login and the default direct-Anthropic LLM path." Full, governed production cutover realistically lands at ~11-15 weeks from kickoff. Honest drivers of the slip, all NEW since the deck: (1) Microsoft Entra enterprise app registration + admin consent in the TotalEnergies tenant routinely takes 1-3+ weeks and may be gated by central group IT rather than the Barnett BU — this is the top schedule risk for the human-access go-live gate; the SSO CODE is mostly already built (the OIDC EntraID provider ships today), so the latency is governance, not engineering. (2) AWS Bedrock per-account/per-region Claude model-access approval (hours-to-days plus possible TotalEnergies procurement review) gates the production-LLM cutover; again the gateway code exists, so the long-pole is the TEEP AWS approval, mitigated by keeping the direct-Anthropic route live as fallback. (3) The FMP/TaskHub API is genuinely net-new (Sebastian, ~26 dev-days for read+write+webhook) and is the only write target — its write path is the tail of the schedule, de-risked by the email-to-MRO fallback so it never blocks the office-cleared launch. (4) The Cygnet-via-existing-intermediary decision (FMP ~30-min poll / ProCount integration / historian export) must be made early with Mike+Darko and may degrade pressure-series fidelity vs the desired 4h@5m window. (5) The IFS Merrick/ProCount owner is still TBD — and ProCount is the canonical asset spine everything binds onto, so a late owner cascades into the registry and every downstream fan-out. What actually gates go-live: the gateway foundation + first bootstrap drops (unblocks all Taikun build), the ProCount spine (unblocks all binding), the Sensirion live path (gates the MTTA KPI), the TaskHub write idempotency soak (gates field-dispatch close-the-loop), and the SSO + Bedrock production gates (gate full governed production cutover). The 60-day operate clock should start only once the office-cleared auto-close path is live end-to-end on real data — counting bootstrap/dev time against the pilot window would understate the measured KPIs. Recommend communicating to the customer as: read-systems + office auto-close at ~6-8 weeks (best case), full governed production at ~11-15 weeks, then 60 days of measured operate, with the critical caveat that the start date floats on TEEP-side approval lead times that Taikun cannot accelerate.

### Phase plan

#### Kickoff & Decisions · Week 0-1

*Goal:* Close every cross-org decision and fire every long-lead TEEP-side approval (Entra app-reg, Bedrock model-access, gateway tech choice, intermediary path, IT-security data-sharing sign-off, IFS owner) so latency clocks start day one, and freeze the contracts Taikun builds against.

Key activities:
- Run the consolidated kickoff decision sessions: gateway tech + auth method + bootstrap transport + Path A/B (GW-1, REG-1); Sensirion webhook-vs-poll + cadence + well_ids (SEN-1); TaskHub write-scope + auto-close policy + webhook-vs-poll (FMP-1); Cygnet intermediary path (SCADA-1/2); SSO OIDC-vs-SAML + tenant/consent owner (SSO-1); Bedrock account/region/models (BEDROCK-1)
- File the Entra Enterprise App registration request and submit the Bedrock Claude model-access request immediately (SSO-2, BEDROCK-2) — longest enterprise lead times
- IT-security sign-off on cross-account S3 data sharing + us-east-1 residency (GW-2), determining Path A vs Path B
- Identify and confirm the IFS Merrick/ProCount business + technical owner (IFS-1)
- Freeze the per-system wire contracts and catalog-dump contracts (SEN-3, FMP-2, REG-2); issue the 6-12 month historical data request (DATA-1)

**Exit gate:** All four GW-1 foundational decisions signed; Entra app-reg and Bedrock model-access requests submitted with named TEEP owners; IT-security data-sharing decision (Path A/B) in writing; IFS owner named and accepted; Sensirion/FMP/catalog contracts frozen and lint-clean; historical-data request TEEP-signed with a delivery date.

#### Bootstrap · Week 1-4

*Goal:* Unblock all Taikun build against real, contract-shaped data before any gateway exists: stand up the signed S3 bucket, land first per-system drops, build the canonical ProCount asset spine, and prove the config-only-cutover invariant on bootstrap data.

Key activities:
- Provision the signed S3 bootstrap bucket + cross-account read + folder layout (GW-3); publish the bootstrap drop spec with manifest/schema_version (GW-4)
- TEEP produces first real drops + per-system catalogs for all 5 systems (GW-5, SEN-5, FMP-4, IFS-5, REG-3/4); ProCount well catalog first as the spine (REG-3, IFS-3 Carte go/no-go)
- Build the canonical TEEP asset registry: connector-driven binding ingest + generalized resolver + number-aware merge, then ProCount spine and bind Cygnet/Sensirion/Carte/FMP (REG-5/6/7/8/9/10/11)
- Build source-agnostic Taikun adapters + S3 poller + enrichment fan-out against bootstrap/mock (GW-6, SEN-6/7, FMP-5, SCADA-7, IFS-6, AGENT-4/5)
- Recompute KPI baselines + seasonality on the historical pull; land event_audit + KPI instrumentation schema; define + freeze the labeled validation set (DATA-2/3/4/7)

**Exit gate:** S3 bucket live with first real drops for all 5 systems validating against schema; ProCount canonical spine built with Cygnet/Sensirion/Carte/FMP bound and 168-alert fan-out validated (REG-12); Taikun read adapters + fan-out green against bootstrap; SSO group→role mapping merged; Bedrock route configured; historical baselines + frozen validation set ready.

#### Build · Week 3-9 (overlaps Bootstrap)

*Goal:* Build the complete close-the-loop agent and all integration code against the frozen contract + bootstrap, in parallel with TEEP building the gateway, FMP API, SSO config, and Bedrock access — so cutover is verification not construction.

Key activities:
- Taikun agent: rule-cascade pre-classifier + LLM-fallback triage + decide/branch + auto-close (gated) + idempotent TaskHub create/patch/close + MRO escalation + durable 24h monitor + close-the-loop finalize, composed into the durable emissions_triage_close_loop JSON workflow (AGENT-6..14); evidence-pack builder + email-to-MRO fallback (FMP-6/10)
- TEEP gateway platform: shell + OAuth2 client-credentials + 24h Idempotency-Key store + HMAC webhook signing + RFC7807 envelope (GW-7/8/10/12/14); FMP read+write endpoints + outbound webhook (FMP-11/12/13); ProCount/Carte OData behind gateway (IFS-8)
- Taikun security halves: OAuth token client + idempotency retry policy + inbound HMAC webhook receiver (GW-9/11/13); SEN-8/9 receiver + reconciler
- SSO: implement group→role mapping, deploy to test VM, CA/MFA review, end-to-end persona login test (SSO-5/7/8/9); Bedrock: route + de-hardcode model + parity + latency/cost + governance sign-off + fallback (BEDROCK-4..9)
- Reporting: Sierra export tool (xlsx+pdf) + canonicalization + filters + override-with-audit + 4 screens (REPORT-2..11); KPI metrics layer + scorecard + methane estimator + accuracy measurement (DATA-5/6/8/10)
- SCADA: tag-coverage probe + contract adaptation + tag_map + 95-alert resolution-impact replay (SCADA-3/4/5/9)

**Exit gate:** All agent stage tools pass unit tests and the 4-scenario simulator green against mock/bootstrap (AGENT-16); FMP read+write endpoints Schemathesis-conformant with idempotency proven; SSO acceptance passed on test VM for all 4 personas; Bedrock parity ≥92% + within latency budget + governance sign-off; Sierra export validated against her real file (REPORT-12); classification accuracy measured vs the held-out labeled set; SCADA replay go/no-go on the intermediary.

#### Cutover · Week 9-13

*Goal:* Flip from bootstrap/mock to live TEEP infrastructure system-by-system (read systems first, TaskHub write last), pass the SSO and Bedrock production gates, and run a full end-to-end production rehearsal.

Key activities:
- Per-system gateway conformance tests as endpoints land (CUTOVER-4); config-only base-URL flips for read systems with verified S3 rollback (CUTOVER-8, SEN-12, IFS-9, SCADA-11, REG-14)
- Sensirion live webhook + poll-fallback verification (CUTOVER-5); Cygnet-via-intermediary signal-fidelity validation (CUTOVER-6); ProCount/Carte read validation (CUTOVER-7)
- TaskHub write go-live behind the duplicate-dispatch idempotency soak (CUTOVER-9, FMP-14); email-fallback armed if write slips (CUTOVER-10)
- SSO production cutover gate (CUTOVER-13, SSO-10) and Bedrock production-LLM cutover gate (CUTOVER-14, BEDROCK-10) verified
- Production monitoring (Prometheus→CloudWatch + Integration Health) + failure-notification wiring (CUTOVER-11/12, SEN-11); full 4-scenario production rehearsal (CUTOVER-15)

**Exit gate:** All read systems serving from the gateway via config-only flip with rollback proven; TaskHub write soak shows zero duplicates across 50+ replayed writes (or email-fallback armed); SSO corporate login role-mapped per persona; Bedrock decision-parity + residency confirmed; monitoring + alerting demonstrated; production rehearsal of all 4 scenarios green with Sierra's 22 columns auto-populating; signed GO decision (CUTOVER-15).

#### Operate · Week 13-21+ (60-day window)

*Goal:* Run Maxwell in production for 60 days, measure against the six signed success criteria, hold weekly mapping-table reviews with Sierra, recalibrate KPIs on real data, and decide Phase-1→Phase-2.

Key activities:
- Production go-live + start the 60-day clock; daily Integration Health glance + on-call ownership of the alert channel (CUTOVER-16)
- Weekly free-text→category mapping review with Sierra (config not code) re-confirming the 22-column export each cycle (CUTOVER-17)
- Graduate the auto-close gate shadow→human-confirm→autonomous for Process Emissions after Devin reviews divergences (AGENT-18); live-vs-historical reconciliation + cadence/KPI true-up (SEN-13)
- Operate-window KPI monitoring + incident handling against the success criteria; shadow-accuracy spot-checks; live scorecard (CUTOVER-18, DATA-12, BEDROCK-11, SCADA-12, REG-15, FMP-15)
- Pilot review + Phase-2 go/no-go with written Devin + Sierra sign-off + scoped Phase-2 proposal (CUTOVER-19, DATA-13)

**Exit gate:** 60 days of production traffic processed; all six PRD §11 success criteria assessed with evidence; no API incident forced Darko to roll back access; Devin + Sierra written confirmation they would not return to manual; realized methane-avoided + live accuracy reported; Phase-1→Phase-2 go/no-go recorded with Phase-2 scope.

### Milestones

| Milestone | Target | Gate criteria |
|---|---|---|
| Kickoff decisions locked & long-lead approvals fired | Week 1 | GW-1 four foundational decisions signed; Entra app-reg (SSO-2) and Bedrock model-access (BEDROCK-2) requests submitted with named owners; IT-security data-sharing/Path-A decision in writing (GW-2); IFS Merrick/ProCount owner named (IFS-1); Sensirion/FMP/catalog contracts frozen and lint-clean; historical-data request TEEP-signed (DATA-1). |
| Bootstrap live — first real drops flowing & adapters building | Week 3 | Signed S3 bucket reachable by Taikun with first contract-valid drops for all 5 systems + per-system catalogs (GW-3/4/5); Taikun S3 poller + read adapters green against bootstrap (GW-6); drop shapes validate against teep-api.yaml with zero drift defects open. |
| Canonical asset spine + cross-system binding validated | Week 5 | ProCount spine built and Cygnet/Sensirion/Carte/FMP bound onto it (REG-8/9/10/11); end-to-end 168-alert fan-out coverage meets per-system mention rates with zero number collisions and ≥80% auto-link on fuzzy systems (REG-12); review queue worked with SME sign-off (REG-13). |
| Agent feature-complete & simulator-green on mock/bootstrap | Week 9 | emissions_triage_close_loop durable workflow loads in the engine + ReactFlow editor and drives all three branches end-to-end (AGENT-14); 4-scenario simulator (auto-close/dispatch/monitor/timeout) passes with idempotency + per-system failure isolation (AGENT-16); Sierra export validated against her real file (REPORT-12); classification accuracy measured vs the held-out labeled set (DATA-10). |
| Read systems live via config-only gateway cutover | Week 11 | Sensirion live webhook + poll-fallback verified (CUTOVER-5/SEN-12); Cygnet-via-intermediary signal fidelity validated (CUTOVER-6); ProCount/Carte read validated (CUTOVER-7); all read systems flipped to the gateway base URL via config with verified S3 rollback (CUTOVER-8); office-cleared auto-close path runnable on real data. |
| Governance + write gates passed (SSO, Bedrock, TaskHub-write) | Week 13 | SSO corporate login role-mapped per persona on test then prod (SSO-9/10, CUTOVER-13); Bedrock decision-parity ≥92% + within latency + governance sign-off + production cutover (BEDROCK-6/8/10, CUTOVER-14); TaskHub write idempotency soak shows zero duplicates across 50+ replayed writes or email-fallback armed (CUTOVER-9, FMP-14); monitoring + alerting demonstrated (CUTOVER-11/12). |
| Production GO & 60-day operate clock starts | Week 13-14 | Full 4-scenario production rehearsal green against the real stack with Sierra's 22 columns auto-populating (CUTOVER-15); signed GO decision; live events processing end-to-end; 60-day clock started with recorded start date and on-call ownership assigned (CUTOVER-16). |
| Phase-1 → Phase-2 go/no-go | Week 21-22 | 60 days of production traffic processed; all six PRD §11 success criteria assessed with evidence (≥95% auto-ingested, median MTTA ≤15min, ≥40% auto-closed, Sierra's report from the agent store, no roll-back-forcing API incident, Devin+Sierra written sign-off); realized methane-avoided + live accuracy reported; Phase-2 scope proposal accepted (CUTOVER-19, DATA-13). |

### Critical path

The chain that determines the go-live date. Slip any of these and the end date slips.

| # | Task | Workstream | Why it's on the critical path |
|--:|---|---|---|
| 1 | `GW-1` | GW | Locks the four foundational decisions (gateway tech, auth method, bootstrap transport, Path A/B). Until these are made nothing on either side can build; it is the literal first gate of the whole program. |
| 2 | `GW-2` | GW | IT-security sign-off on cross-account S3 data sharing determines Path A vs Path B for the asset registry and whether the bootstrap track (which unblocks all Taikun build) is even permitted. Sahir-owned, enterprise lead time. |
| 3 | `GW-3` | GW | The signed S3 bootstrap bucket is the physical channel for every per-system drop; no bucket means no bootstrap data means Taikun is blocked on the gateway, collapsing the parallelism the whole plan depends on. |
| 4 | `GW-4` | GW | The frozen drop spec (shapes byte-identical to teep-api.yaml) is what makes cutover config-only. If shapes drift the config-only-cutover invariant breaks and every adapter needs rework at cutover. |
| 5 | `REG-3` | REG | The ProCount well catalog is the canonical asset spine every other system (Cygnet/Sensirion/Carte/FMP) binds onto. A late spine cascades into all binding and every cross-system enrichment fan-out; gated by the still-TBD IFS owner. |
| 6 | `REG-8` | REG | Building the spine (with API#-based Strategy A binding) is the prerequisite for binding all other systems; the canonical asset_id resolution gates the agent's enrichment fan-out for every alert. |
| 7 | `REG-10` | REG | Sensirion device→pad/well binding (reference-only) is the keystone: Sensirion is the 168/168 alert origin, so a wrong or missing binding breaks every downstream fan-out. Go-live gate for the triage path. |
| 8 | `AGENT-14` | AGENT | Composing the durable emissions_triage_close_loop workflow is where all stage tools become a working end-to-end agent; nothing can be cutover or rehearsed until this exists and runs through the engine + gateway. |
| 9 | `AGENT-16` | AGENT | The 4-scenario simulator run on the test VM is the proof the agent is correct and idempotent before any live traffic; it is the verification gate that makes the per-system cutovers low-risk. |
| 10 | `FMP-12` | FMP | Sebastian's net-new TaskHub write endpoints + 24h Idempotency-Key store are the highest-effort, highest-risk net-new piece and the only write target; the field-dispatch close-the-loop (~27% of events) cannot go live without it. Email-fallback de-risks but this is the true tail. |
| 11 | `CUTOVER-9` | CUTOVER | The TaskHub write idempotency/duplicate-dispatch soak is the one cutover where a bug puts a duplicate real field job in front of a lease operator; it gates enabling write in production. |
| 12 | `CUTOVER-13` | CUTOVER | SSO production cutover (gated by Sahir/TEEP IT Entra app-reg + admin-consent latency) is a hard prerequisite for full governed production access — no shared/manual credentials may reach prod. Enterprise-process long-pole. |
| 13 | `CUTOVER-14` | CUTOVER | Bedrock production-LLM cutover gates the auto-close/classification path for governed production (every classification is an LLM call); gated by TEEP AWS model-access approval. Direct-Anthropic fallback de-risks but production posture requires it. |
| 14 | `CUTOVER-15` | CUTOVER | The full end-to-end production rehearsal of all 4 scenarios against the real stack is the single GO/no-go before declaring production; it is the convergence point of every other critical-path task. |
| 15 | `CUTOVER-16` | CUTOVER | Production go-live starts the 60-day operate clock; the entire success-criteria measurement window (and thus the end date and Phase-2 decision) cannot begin until this lands. |

---

## 3 · Workstreams

| ID | Workstream | Lead | Tasks | Days |
|---|---|---|--:|--:|
| **SEN** | Sensirion / Nubo Event Integration | Joint | 13 | 29.0 |
| **FMP** | FMP / TaskHub Net-New API + Dispatch Write Path | TEEP | 15 | 61 |
| **SCADA** | Cygnet SCADA Access via Existing Systems (not direct) | Joint | 12 | 24.0 |
| **IFS** | ProCount + Carte (IFS Merrick) Integration — production codes, comments, work orders, injection-rate enrichment + canonical asset spine | Taikun | 11 | 31 |
| **SSO** | Identity & SSO — Microsoft Entra ID Federation into the Taikun Platform | Joint | 12 | 15.5 |
| **BEDROCK** | Bedrock — LLM Inference via TEEP AWS Bedrock | Taikun | 11 | 22 |
| **GW** | API Gateway Platform, Security Conventions & Bootstrap | TEEP | 17 | 53 |
| **REG** | Cross-System Asset Registry & Identity Binding (Path A) | Taikun | 16 | 38.0 |
| **AGENT** | Maxwell Close-the-Loop Agent Build | Taikun | 19 | 70 |
| **REPORT** | Reporting & UI — Sierra HSE/EPA Export + 4 Screens | Taikun | 13 | 25.5 |
| **DATA** | Data Baseline, KPIs & Pilot Success Criteria | Taikun | 13 | 49 |
| **CUTOVER** | Integration Testing, Cutover, Go-Live and Operate | Taikun | 19 | 67 |

Task tables below are sorted by phase. **Blk** = blocking (gates other work or go-live). Full descriptions, entry/exit criteria, and deliverables for every task are in [`project-plan.json`](project-plan.json).

### SEN · Sensirion / Nubo Event Integration

**Lead:** Joint  ·  **Effort:** ~29.0 person-days  ·  **Tasks:** 13

**Objective.** Get live Sensirion/Nubo methane event data into Maxwell at event speed — replacing the 5.2h email delay — via a signed webhook-in (preferred) with a GET poll reconciliation fallback, per-event PPM and kg/hr time series, and a deterministic device->pad/well binding into asset_metadata.*. Progress the source through three stages without rewriting Taikun's adapter: bootstrap-0 (Sierra xlsx, already ingested), signed S3 JSON drops shaped exactly like the future REST/webhook payloads, then the live gateway webhook + poll. Nail the unknown device poll cadence (with Nubo, via Michelle) so time-series sample resolution and the poll-fallback interval are correct.

Sensirion is the origin of every alert (168/168) and the single most time-critical integration in Maxwell: the entire MTTA-5.2h-to-under-10-min KPI lives or dies on getting the kg/hr threshold-cross event the moment Nubo computes it, not when the confirmation email lands. The contract is already designed and tested — teep-api.yaml defines the SensirionEvent/SensirionDevice schemas, the GET /v1/sensirion/events poll fallback, the GET /v1/sensirion/events/{id} time-series detail, and the signed POST /webhooks/sensirion/events inbound webhook (HMAC-SHA256, X-TEEP-Signature + X-TEEP-Timestamp, +/-5min skew). What does NOT exist yet is the live wire on both sides: Taikun's advisor API today is read-only over the historical xlsx import (emissions.alerts), with no webhook receiver, no poll reconciler, and no per-event series storage; and on the TEEP side Nubo's three native APIs (PPM, location, kg/hr) have to be wrapped/proxied by Sebastian and re-emitted as the gateway webhook. So this workstream is genuinely joint and net-new ASK #1.

The chosen approach is the brief's bootstrap progression, which keeps Taikun unblocked while the gateway is built in parallel. Stage 0 is already done — Sierra's xlsx is loaded (168 alerts, 32 pad baselines), proving the column mapping (pad, pad_code, route, emission_id, emission_rate_kgh) and giving us a replay corpus. Stage 1 is signed S3 JSON drops shaped EXACTLY like the SensirionEvent webhook payload and the events-detail series response, so Taikun builds and tests the real adapter, time-series ingest, and device binding against real January data before any live API exists. Because the JSON shape is identical at the wire, Stage 2 (live gateway webhook + poll) is a config flip, not a rewrite. We build the webhook receiver and the poll reconciler from day one and feed both from the S3 corpus first, then repoint to the gateway. Device->pad/well binding is deterministic Strategy B (bind by the pad_id/well_ids/asset_path the device-metadata endpoint already carries — NEVER fuzzy, because NUB-D-#### IDs have zero name overlap with wells), running on the proven asset_metadata.* schema; the only genuinely foreign-system risk is whether Sensirion device metadata reliably carries well_ids vs only pad_id, which sets well-level vs pad-level resolution.

The one hard unknown is the device poll cadence. Michelle confirmed it is shorter than 4h but the exact number is unknown to TEEP and must come from Nubo. It drives two real parameters: the kg/hr and PPM time-series sample resolution we request/store, and the poll-fallback interval (currently assumed 5 min). We treat this as a blocking open decision with a recommended interim default so engineering is not gated, but flag that a wrong assumption degrades the "PPM spiked then cleared in 3 min -> false positive" decision-trace evidence.

What is genuinely uncertain: (1) whether Nubo offers a webhook to TEEP at all, or whether Sebastian must poll Nubo and re-emit — this determines whether end-to-end latency is truly event-speed or capped at TEEP's own poll cadence (FMP reportedly polls device data on ~30-min cadence, which would gut the MTTA KPI if it became the path); (2) the exact device poll cadence (above); (3) whether device metadata carries well_ids; and (4) the per-event-vs-clock cost of detail polling (every 30s while open) against whatever rate limits Nubo/gateway impose. None are blockers to starting on the S3 corpus, but all must close before live cutover and are recorded as open decisions or explicit task assumptions.

**Key people**

| Name | Org | Role |
|---|---|---|
| Michelle | TEEP | Owns the Sensirion/Nubo relationship and FMP/TaskHub; primary channel to Nubo for cadence + API questions |
| Sebastian | TEEP | TEEP engineer connecting TEEP to the Sensirion server; will wrap/proxy Nubo's native APIs (PPM/location/kg-hr) and emit the gateway webhook |
| Darko Jankovic | TEEP | Engineering / API gateway / security; owns OAuth2, HMAC webhook signing, S3 bootstrap drops |
| Sahir | TEEP | Cyber & Field Ops; data-sharing / IT-security sign-off on device catalog + lat/lon egress |
| Sierra | TEEP | HSE reporting coordinator; owns the xlsx export that is bootstrap-0 and the 22 monthly-report columns the event series feeds |
| Nubo / Sensirion AG support | Sensirion/Nubo | Vendor; authoritative source for device poll cadence, native webhook availability, and PPM/kg-hr API semantics — owner contact TBD |
| Taikun engineering | Taikun | Builds the Sensirion adapter, webhook receiver (API GW + Lambda), poll-fallback reconciler, time-series ingest, and device->pad/well binding via asset_metadata.* |
| Steve Ridder | Taikun | Founder/CEO; joint-session driver, escalation owner for the Nubo cadence ask |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `SEN-1` | Joint Sensirion/Nubo technical kickoff + cadence + webhook-availability ask | Joint · Steve Ridder (Taikun) + Michelle (TEEP) + Sebastian (TEEP); Nubo support via Michelle | Kickoff | 1.5 | 🔴 | — |
| `SEN-2` | Confirm device poll cadence with Nubo (Q2) | Sensirion/Nubo · Nubo support (authoritative) via Michelle (TEEP) | Kickoff | 2 | 🔴 | `SEN-1` |
| `SEN-3` | Freeze Sensirion event + series + device contract against teep-api.yaml | Joint · Taikun engineering + Sebastian (TEEP); Darko reviews | Bootstrap | 1.5 | 🔴 | `SEN-1` |
| `SEN-4` | Bootstrap-0 audit: map Sierra xlsx fields to the live event contract | Taikun · Taikun engineering | Bootstrap | 1 |  | `SEN-3` |
| `SEN-5` | TEEP S3 Sensirion drops: events + per-event series + device catalog | TEEP · Sebastian (TEEP) builds the export; Darko (TEEP) provisions/signs the S3 bucket | Bootstrap | 4 | 🔴 | `SEN-3`, `SEN-4` |
| `SEN-10` | Device->pad/well binding via asset_metadata.* (Strategy B, deterministic) | Taikun · Taikun engineering | Build | 2.5 | 🔴 | `SEN-5` |
| `SEN-11` | Sensirion observability + failure alerting | Taikun · Taikun engineering | Build | 1.5 |  | `SEN-8`, `SEN-9` |
| `SEN-6` | Sensirion adapter — normalize wire payload to emissions.* (source-agnostic) | Taikun · Taikun engineering | Build | 3 | 🔴 | `SEN-3`, `SEN-4` |
| `SEN-7` | Per-event PPM & kg/hr time-series ingest + storage | Taikun · Taikun engineering | Build | 2.5 |  | `SEN-2`, `SEN-6` |
| `SEN-8` | Webhook receiver — POST /webhooks/sensirion/events (HMAC-verified) | Taikun · Taikun engineering | Build | 3 | 🔴 | `SEN-3`, `SEN-6` |
| `SEN-9` | Poll-fallback reconciler — GET /v1/sensirion/events?since= + detail polling | Taikun · Taikun engineering | Build | 2.5 |  | `SEN-3`, `SEN-6`, `SEN-7` |
| `SEN-12` | Live gateway cutover — repoint webhook + poll from S3 to gateway | Joint · Sebastian (TEEP) + Darko (TEEP) + Taikun engineering | Cutover | 2 | 🔴 | `SEN-8`, `SEN-9`, `SEN-10`, `SEN-11` |
| `SEN-13` | Operate: live-vs-historical reconciliation + cadence/KPI true-up | Joint · Taikun engineering + Michelle (TEEP) + Sierra (TEEP) | Operate | 2 |  | `SEN-12` |

**Deliverables:** Sensirion integration decision memo *(Joint)*; Frozen Sensirion v1 wire contract *(Joint)*; Signed S3 Sensirion bootstrap drops *(TEEP)*; Source-agnostic Sensirion adapter + emissions migration *(Taikun)*; Per-event time-series store + ingest *(Taikun)*; HMAC-verified Sensirion webhook receiver *(Taikun)*; Poll-fallback reconciler *(Taikun)*; Sensirion device->pad/well bindings *(Taikun)*; Sensirion observability + alerting *(Taikun)*; Live cutover + validation report *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Does Nubo offer a native webhook to TEEP, or must Sebastian poll Nubo and re-emit as the gateway webhook? (Determines whether MTTA is truly event-speed or capped by TEEP's poll cadence.) | Sensirion/Nubo | If no native webhook, TEEP polls Nubo at the confirmed device cadence (not the 30-min FMP cadence) and re-emits on threshold-cross immediately. | Kickoff (SEN-1) — before contract freeze |
| What is the Sensirion device poll cadence (device_poll_seconds) and native PPM/kg-hr sample resolution? (Q2) | Sensirion/Nubo | Proceed on 60s interim default (per the SensirionDevice example) and stamp series with the assumption; backfill once Nubo confirms. | Before time-series ingest go-live (SEN-7) and final KPI sign-off |
| Does Sensirion device metadata reliably carry well_ids, or only pad_id? | Sensirion/Nubo | Bind at pad level via Strategy B and expand to wells through AssetHierarchyService; request well-level metadata for Phase 2. | Before device binding (SEN-10) |
| Poll-fallback interval for GET /v1/sensirion/events?since= — keep 5min or align to device cadence? | Taikun | 5 minutes as a backstop (webhook is primary); tighten only if cadence confirms a faster meaningful resolution. | Before reconciler build (SEN-9) |
| Are nubo_confirmation_status='dismissed' events suppressed by TEEP or still emitted to Taikun for audit? | TEEP | Emit all events including dismissed; Maxwell records dismissed in the audit trail but does not triage them. | Contract freeze (SEN-3) |
| Which TEEP contact/channel receives Sensirion API-failure alerts (email vs PagerDuty/Opsgenie vs Teams webhook)? | TEEP | Email to a TEEP-designated HSE/ops alias as primary; add Teams webhook if TEEP provides a URL. | Before observability go-live (SEN-11) |
| Does IT-security (Sahir) approve egress of the Sensirion device catalog + lat/lon as Path A source data, or must we use Path B resolver? | TEEP | Path A (data-only, us-east-1, metadata-only logs) — fall back to Path B GET /v1/assets/{id} only if blocked. | Before S3 device catalog drop (SEN-5) |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Nubo->TEEP path is poll-based (no native Nubo webhook), so TEEP re-emits on its own cadence — reportedly FMP polls device data ~30 min — capping end-to-end latency and gutting the MTTA <10min KPI even though Taikun's webhook is instant. | M | H | Resolve in SEN-1 explicitly; if Nubo has no webhook, push TEEP to poll Nubo at the true device cadence (SEN-2) not the 30-min FMP cadence, and re-emit immediately on threshold-cross. Measure real MTTA at cutover (SEN-12) and restate the KPI honestly if the path caps it. | Joint |
| Device poll cadence (Q2) stays unknown — wrong assumed sample resolution corrupts the PPM/kg-hr decision-trace evidence ('spiked then cleared in 3 min') and cumulative-emissions math. | M | M | Adopt an interim default (60s per the SensirionDevice example) so engineering is unblocked; stamp series with assumed resolution and backfill/re-stamp once Nubo confirms (SEN-2, SEN-13). Treat as a blocking open decision before final KPI sign-off. | Sensirion/Nubo |
| Sensirion device metadata carries only pad_id, not well_ids, forcing pad-level binding and coarser cross-system fan-out (can't isolate the specific well for Cygnet/ProCount lookups). | M | M | Confirm in SEN-1; if well_ids absent, bind at pad level (Strategy B) and expand to wells via AssetHierarchyService; accept pad-level resolution for Phase 1 and request well-level enrichment for Phase 2. | Sensirion/Nubo |
| S3 bootstrap drops drift from the frozen contract (extra/missing fields, different casing), breaking the config-only cutover promise and forcing adapter rework. | M | M | Validate every S3 drop against the Prism/Schemathesis contract in CI before Taikun consumes it (SEN-5); fail the drop loudly on schema drift rather than silently coercing. | TEEP |
| Detail-polling every 30s per open event hits Nubo/gateway rate limits during an incident burst (multiple simultaneous open events), starving series ingest. | L | M | Cap concurrent detail polls, honor 429 retry-after with backoff (SEN-9), prefer webhook-pushed series where Nubo supports it; size against observed ~7.6 events/day with incident-burst headroom. | Taikun |
| HMAC shared-secret provisioning slips with the gateway (no gateway exists today), blocking the signed webhook path at cutover. | M | H | Build + test the receiver against the S3 corpus replayed as signed POSTs before the gateway exists (SEN-8); make secret rotation per-environment config; gateway workstream (GW-*) owns secret delivery on the critical path. | TEEP |
| IT-security (Sahir) restricts egress of device lat/lon + catalog as source data (Path A), forcing Path B resolver and delaying binding. | L | M | Path A is data-only (no new endpoint) and us-east-1 resident with metadata-only logs; if blocked, fall back to the already-specced Path B GET /v1/assets/{id}. Get Sahir sign-off in SEN-1. | TEEP |

---

### FMP · FMP / TaskHub Net-New API + Dispatch Write Path

**Lead:** TEEP  ·  **Effort:** ~61 person-days  ·  **Tasks:** 15

**Objective.** Stand up the only net-new TEEP API — the FMP/TaskHub read+write surface (GET list/detail/task-types, POST create dispatch, PATCH notes, PATCH close) plus the outbound task.updated webhook — and wire Maxwell's agent-side adapter to it, so the agent can close the loop on the ~27% dispatch / ~3% escalate events: post a full evidence pack to the lease operator, monitor until field-confirmed + sensor-baseline (24h timeout), and PATCH the task closed while populating Sierra's 22 reporting columns. Ship an email-to-MRO bootstrap fallback so field-cleared events keep flowing if TaskHub write is not ready by week 4.

TaskHub/FMP is the single net-new API build in the entire Maxwell integration (the other four systems — Sensirion, Cygnet, ProCount, Carte — are vendor products TEEP proxies/transforms). It is also the only WRITE target: every other system is read-only. That makes this workstream the critical path for "close the loop." Maxwell auto-closes ~70% of alerts from the office without TaskHub write, but the ~27% dispatch + ~3% escalate population (the field-cleared 43.5% in the Jan data) cannot be closed end-to-end until the three write endpoints (POST create, PATCH notes, PATCH close) and the inbound task.updated webhook exist. The contract is already specced and tested: teep-api.yaml §/v1/fmp/* is lint-clean (@redocly), mock-served (Prism), property-tested (Schemathesis), and exercised by the 4-scenario agent simulator (auto-close / dispatch / monitor / timeout) on the AWS test VM. So Taikun's adapter is already proven against the mock — the open work is (a) Sebastian building the real API behind it, and (b) joining the two over the gateway.

The chosen approach decouples the two sides so neither blocks the other. Sebastian builds the FMP API to the frozen teep-api.yaml contract; Taikun keeps developing its adapter against the Prism mock + S3 bootstrap drops shaped exactly like the future REST responses. Because the bootstrap JSON shape is byte-identical to the production response shape, cutover is config-only, not a rewrite. The agent-side dispatch/monitor/close machinery is structurally the platform's existing human-interlock-and-timeout pattern (tank_overflow_protection_with_servicenow, ring_energy_ai_traffic_cop_v3) specialized for emissions — Maxwell is the ai.analyze step, the TaskHub create/monitor/close steps are the close-the-loop machinery. We do not rebuild that orchestration; we wire new FQTN stage tools (taskhub.create_task, taskhub.patch_task, taskhub.close_task) wrapping the adapter into the JSON workflow.

What is genuinely uncertain and load-bearing: (1) whether TEEP wants the agent to AUTO-CLOSE tasks or only ever have the LO close via the TaskHub UI with a webhook firing (the contract supports both; this is a Clovis/Michelle policy call). (2) The exact TaskHub task-type enum and naming convention (which types are 'planned emission') — confirmed only by mining 168 notes, must be ratified by Michelle. (3) Whether TaskHub can emit a real outbound webhook at all, or whether the agent must poll GET /v1/fmp/tasks/{id} every 5 min — TaskHub is home-built and may have no webhook infra. (4) Whether the LO contact data (phone/email for dispatch routing) lives in TaskHub or must be joined from another system. (5) The hardest classification surface — LO free-text notes reconciliation — is a Phase-2 problem; Phase-1 captures the free text verbatim into the close-out and uses low-confidence → Undetected → MRO.

Realistically, even though the contract is frozen and the Taikun adapter is mock-proven, the net-new API plus gateway dependency plus webhook-vs-poll uncertainty plus the policy decisions push the dispatch write path to land at the tail of the deck's aggressive 8-week window. The mitigation is the email-to-MRO bootstrap fallback (FR-21 fallback in 02-prd.md §8.1): if write isn't live by week 4, the agent emails Devin's MRO team the same evidence pack it would have POSTed, config-toggled per environment. Office-cleared events benefit Day 1 regardless; field events degrade gracefully to email until write turns on, then flip to TaskHub by config.

**Key people**

| Name | Org | Role |
|---|---|---|
| Sebastian | TEEP | TEEP-side engineer — builds the net-new FMP/TaskHub read+write API and the task.updated outbound webhook |
| Michelle | TEEP | Owns TaskHub/FMP product + the Sensirion/Nubo relationship; signs off task-type enum, LO contact data, dispatch routing |
| Darko Jankovic | TEEP | Engineering / API gateway / security; owns OAuth2, Idempotency-Key store, HMAC webhook signing, RFC7807 envelope that wrap the FMP endpoints |
| Devin | TEEP | MRO triage engineer — recipient of the email-to-MRO bootstrap fallback; validates evidence-pack content + dispatch realism |
| Clovis | TEEP | Operations lead — approves that the agent may write/close TaskHub tasks (auto-close vs LO-closes-only policy) |
| Steve Ridder | Taikun | Founder/CEO — owns teep-api.yaml contract, agent-side TaskHub adapter + dispatch/monitor/close orchestration |
| Taikun engineering | Taikun | Builds the TaskHub adapter (taskhub.create_task / patch / close tools), the inbound task.updated webhook receiver, evidence-pack builder, email-fallback path |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `FMP-1` | Kickoff: confirm TaskHub write is in Phase-1 scope + auto-close policy | Joint · Clovis (ops sign-off) + Michelle (TaskHub) + Darko (gateway) + Steve Ridder (Taikun) | Kickoff | 1 | 🔴 | — |
| `FMP-2` | Freeze the FMP/TaskHub API contract (teep-api.yaml §/v1/fmp/*) | Joint · Sebastian (TEEP impl feasibility) + Steve Ridder (Taikun contract owner) | Kickoff | 2 | 🔴 | `FMP-1` |
| `FMP-3` | Ratify TaskHub task-type enum + planned-emission flags with Michelle | TEEP · Michelle (authoritative source) + Taikun eng (records into catalog config) | Kickoff | 1 |  | `FMP-1` |
| `FMP-4` | Bootstrap: TaskHub S3 JSON drops shaped to the contract | TEEP · Sebastian (export) + Michelle (TaskHub data access) | Bootstrap | 3 | 🔴 | `FMP-2` |
| `FMP-5` | Taikun TaskHub READ adapter (list/detail/task-types) against mock + S3 | Taikun · Taikun engineering | Bootstrap | 4 |  | `FMP-2`, `FMP-3` |
| `FMP-10` | Email-to-MRO bootstrap fallback (write-not-ready path) | Taikun · Taikun engineering | Build | 2 |  | `FMP-6` |
| `FMP-11` | Sebastian builds FMP/TaskHub READ endpoints (list, detail, task-types) | TEEP · Sebastian | Build | 8 | 🔴 | `FMP-2`, `FMP-3` |
| `FMP-12` | Sebastian builds FMP/TaskHub WRITE endpoints + Idempotency-Key store | TEEP · Sebastian (with Darko on the Idempotency-Key store / gateway) | Build | 12 | 🔴 | `FMP-2`, `FMP-11`, `FMP-3` |
| `FMP-13` | Sebastian builds outbound task.updated webhook (HMAC-signed) | TEEP · Sebastian (with Darko + Michelle on shared-secret provisioning) | Build | 6 |  | `FMP-12` |
| `FMP-6` | Taikun evidence-pack builder for dispatch POST body | Taikun · Taikun engineering | Build | 3 |  | `FMP-5` |
| `FMP-7` | Taikun TaskHub WRITE adapter — POST create + Idempotency-Key handling | Taikun · Taikun engineering | Build | 4 | 🔴 | `FMP-6` |
| `FMP-8` | Taikun TaskHub WRITE adapter — PATCH notes + PATCH close | Taikun · Taikun engineering | Build | 3 | 🔴 | `FMP-7` |
| `FMP-9` | Taikun inbound task.updated webhook receiver (HMAC verify + dedupe) | Taikun · Taikun engineering | Build | 4 | 🔴 | `FMP-8` |
| `FMP-14` | Cutover: point Taikun adapter at live FMP gateway (config-only) | Joint · Taikun engineering (config + run) + Sebastian (live API standby) + Darko (OAuth2 client) | Cutover | 3 | 🔴 | `FMP-7`, `FMP-8`, `FMP-9`, `FMP-11`, `FMP-12` |
| `FMP-15` | Operate: pilot dispatch monitoring + round-trip KPI instrumentation | Joint · Taikun engineering (instrumentation) + Devin (dispatch realism) + Michelle (TaskHub) | Operate | 5 |  | `FMP-14` |

**Deliverables:** Frozen FMP API contract (fmp-frozen-v1) *(Joint)*; Ratified TaskHub task-type config *(TEEP)*; TaskHub S3 bootstrap drops + pad catalog *(TEEP)*; Taikun TaskHub adapter (read + write) *(Taikun)*; Evidence-pack builder *(Taikun)*; task.updated webhook receiver + 5-min poll backstop *(Taikun)*; Email-to-MRO fallback (config-toggled) *(Taikun)*; Live FMP read endpoints *(TEEP)*; Live FMP write endpoints + 24h Idempotency-Key store *(TEEP)*; Outbound task.updated webhook *(TEEP)*; Live dispatch close-the-loop + KPI panel *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Is TaskHub write (POST/PATCH) confirmed in Phase-1 scope (PRD Q3)? | TEEP (Clovis + Michelle + Darko) | Yes — write in Phase-1 scope; required for close-the-loop on the ~27% dispatch population. Email-to-MRO fallback if not live by week 4. | Kickoff (FMP-1) — gates the whole write build. |
| Does Maxwell AUTO-CLOSE TaskHub tasks, or does the LO always close via the TaskHub UI with a webhook firing? | TEEP (Clovis ops + Michelle) | LO closes via the TaskHub UI; the task.updated webhook records the close in emissions.alerts. Lower autonomy/trust barrier; contract supports both. | Kickoff (FMP-1) — shapes FMP-8 close path. |
| Can TaskHub (home-built) emit an outbound task.updated webhook, or must Taikun poll? | TEEP (Sebastian + Michelle) | If feasible, build the HMAC-signed webhook; otherwise rely on Taikun's 5-min poll backstop. Build the poll path regardless. | Kickoff (FMP-1) — determines whether FMP-13 is in scope. |
| What is the authoritative TaskHub task-type enum + which types are planned_emission? | TEEP (Michelle) | Ratify the §3.3 inferred list (liquids_unloading, compression_maintenance, tank_thief_hatch_inspection planned=true; well_pump_repair planned=false; emissions_dispatch for agent-created) — but capture Michelle's exact values; do not ship inferred. | Kickoff/Bootstrap (FMP-3) — gates read adapter + GET /task-types. |
| Where does LO dispatch-routing contact data (assignee, phone, email) live — in TaskHub or another system? | TEEP (Michelle + Sebastian) | Source from TaskHub if present (dispatched_to_* fields); else join from the Path-A asset registry pad→LO-contact catalog. | Build (FMP-11/12) — affects TaskHubTask population + dispatch routing. |
| Where should dispatch + API-failure notifications land (PRD Q7) — email distro, Teams? | TEEP (Darko) | Email to Devin's MRO distro for the bootstrap fallback (FMP-10) and FR-34 failure alerts; Teams deferred to Phase 2. | Build (FMP-10) — gates email fallback target. |
| What is the kg/hr trigger threshold X for an event to qualify for dispatch (PRD FR-3)? | TEEP (Clovis + HSE) | Default 1 kg/hr until TEEP confirms; affects dispatch volume sizing against the ~7.6 events/day baseline. | Build/Cutover — affects dispatch volume but not the API shape. |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| TaskHub is the ONLY net-new API and has no external API today — Sebastian's build (read+write+webhook, ~26 dev-days) is the critical path and likely overruns the deck's 8-week window. | H | H | Decouple sides: Taikun develops fully against Prism mock + S3 bootstrap so it is cutover-ready early; email-to-MRO fallback (FMP-10) keeps field events flowing if write slips past week 4; phase write after read so the lower-risk read path lands first. | TEEP |
| TaskHub (home-built) may have no infrastructure to emit an outbound webhook, breaking the no-poll close-out design. | M | M | Build the 5-min GET /v1/fmp/tasks/{id} poll backstop (FMP-9) unconditionally; treat the webhook (FMP-13) as a latency optimization, not a go-live blocker. Decide capability at FMP-1 kickoff. | Taikun |
| Missing Idempotency-Key store on TEEP's write endpoints causes duplicate dispatch tasks to LOs on transient retries — erodes field-operator trust fast. | M | H | Idempotency-Key is contract-mandatory (§0.2); Sebastian + Darko build the 24h replay store (FMP-12); Taikun replays a stable key per logical write; verify dedup explicitly in cutover (FMP-14) before any real LO receives tasks. | TEEP |
| Agent auto-closing TaskHub tasks (PATCH status=closed) may be rejected by ops as too much autonomy on field work. | M | M | Build both close paths and toggle by config (FMP-8); default to LO-closes-via-UI + webhook-records-it (lower trust barrier); decision owned by Clovis/Michelle at FMP-1. | Joint |
| LO free-text notes are noisy/inconsistent, so close-out field mapping (problem_identified, equipment_component) is wrong — the hardest classification problem. | M | M | Phase 1 captures LO free text verbatim and low-confidence → Undetected → MRO review; LLM-assisted reconciliation is explicitly Phase 2; weekly Devin review (FMP-15) tunes the mapping. | Taikun |
| LO dispatch-routing contact data (phone/email/assignee) may not live in TaskHub, leaving dispatched_to fields unpopulated. | M | M | Confirm LO-contact source at FMP-3/open-decision; if not in TaskHub, join from the Path-A asset registry pad→LO-contact catalog (FMP-4) rather than blocking the API. | TEEP |
| Inferred task-type enum (from mining 168 notes) diverges from TaskHub's real categories, breaking planned-emission matching. | L | M | Michelle ratifies the authoritative enum (FMP-3) before build; stored as config so changes are data, not code, per the no-hardcoding rule. | TEEP |
| Gateway (OAuth2 + HMAC shared-secret) not ready, blocking live read/write cutover even after endpoints are built. | M | H | Hard dependency on GW workstream; Taikun stays mock/S3-ready so cutover (FMP-14) is config-only the moment OAuth2 + secrets land; bootstrap path covers the gap. | TEEP |

---

### SCADA · Cygnet SCADA Access via Existing Systems (not direct)

**Lead:** Joint  ·  **Effort:** ~24.0 person-days  ·  **Tasks:** 12

**Objective.** Source the 6 curated SCADA signals Maxwell needs for pressure-drop / compressor / sales-rate classification (tubing pressure, line pressure, casing pressure, sales rate, compressor metrics, liquids-unloading flag) through an EXISTING TEEP integration/intermediary instead of a direct CygNet API — because TEEP will NOT expose CygNet directly — while preserving the already-tested teep-api.yaml Cygnet data contract so cutover stays config-only, and honestly characterizing the latency/resolution tradeoffs of each intermediary versus the original 4-hour pre-event window requirement.

This workstream exists because of a material change from the docs. 04-system-integrations.md §2 was written assuming "Internal CygNet API exists at TEEP today... TEEP needs to expose a controlled subset behind the gateway" (i.e. wrap-and-expose the Weatherford CygNet platform API). On the customer call TEEP reversed this: they do NOT want a direct CygNet/SCADA API exposed. SCADA is the most-cited evidence system in the real data (95 of 168 how_cleared notes mention Cygnet — pressure drops are "the dominant office-clear signal"), so losing it is not an option; we must instead source the same 6 logical signals through an EXISTING TEEP pipe. The good news, and the load-bearing design principle of the whole Maxwell integration, is that Maxwell consumes a fixed JSON contract (CygnetState / CygnetSeries / liquids-unloading) that is already lint-clean, Prism-mock-served, and Schemathesis property-tested. Whatever intermediary TEEP picks, Taikun's adapter conforms the source data into that contract, so the agent code and the downstream classifier (the deterministic rule cascade in 03-architecture.md §8, which keys on tubing/line pressure drop, casing drop, sales-rate drop, compressor anomaly) do not change.

The central uncertainty is genuinely the intermediary choice, and it is owned by Mike (Cygnet) + Darko (gateway/security) jointly. There are three credible candidate pipes, each with different latency/resolution/coverage tradeoffs: (1) FMP/TaskHub, which the call established already polls device data on a ~30-minute cadence — lowest TEEP lift if those polled values include the pressures/sales/compressor tags, but 30-min resolution is coarse against the original 4-hour pre-event window that wanted 1m/5m/15m steps, and it is unclear FMP captures casing pressure or compressor suction/discharge; (2) ProCount, which per the docs has a documented Cygnet integration and an IFS Foundation REST/OData API TEEP likely already licenses — but ProCount is production-accounting-grade (daily/allocation cadence), so it is good for sales-rate and code corroboration but probably too coarse and too lagged for intra-event pressure transients; (3) a historian / read-replica / curated export (e.g. a CygNet historian read endpoint, a SQL read-replica of the historian, or a scheduled curated tag export to S3) — closest to the original native resolution and the cleanest fit to the existing series contract, but the highest governance bar and most TEEP build/sign-off.

The honest tradeoff to surface to the customer: the original architecture wanted a 4-hour pre-event pressure series at 5-minute steps so Maxwell can see "tubing 35->18 psi at 07:58." A ~30-min FMP cadence degrades that to a coarse snapshot — still enough to confirm a sustained pressure drop and corroborate ~76 of the pressure+LO-note pattern events, but it weakens fine-grained transient detection and pushes some borderline events toward Undetected/MRO review. We recommend a hybrid default: use whichever existing pipe gives current state at the best available cadence for the live classify decision, and accept reduced series resolution as a documented Phase-1 limitation, with a Phase-2 path to a historian feed if the customer wants the full-resolution series back. Because SCADA confidence directly affects classification accuracy (the >=92% Phase-1 KPI) and auto-close rate (the 40% Phase-1 target), the intermediary decision is on the critical path and must be made early — it gates the adapter build and feeds the KPI-grounding work. This plan front-loads a decision working session, then runs the adapter build against bootstrap S3 drops (so Taikun is unblocked regardless of when the gateway/intermediary is production-ready), then validates resolution against the real Jan-2026 events before cutover.

**Key people**

| Name | Org | Role |
|---|---|---|
| Mike | TEEP | Owner of Cygnet/CygNet SCADA (Weatherford); owns logical->physical tag mapping and the curated 6-tag subset decision |
| Darko Jankovic | TEEP | Engineering / API gateway / security-governance; owns intermediary-pipe feasibility, S3 bootstrap drops, OAuth2/HMAC gateway plumbing |
| Sebastian | TEEP | TEEP engineer building the FMP/TaskHub API; relevant if the ~30-min FMP device-poll cache becomes the SCADA intermediary |
| Michelle | TEEP | Owns FMP/TaskHub + Sensirion relationship; production-accounting/ProCount owner-by-proxy if ProCount is chosen as the Cygnet pipe |
| ProCount/Carte owner (TBD) | IFS Merrick | Production accounting; owns the documented ProCount<->Cygnet integration if ProCount is the chosen intermediary |
| Taikun engineering | Taikun | Builds the Cygnet Taikun adapter + the 4 cygnet read tools against the tested contract; runs latency/resolution validation |
| Steve Ridder | Taikun | Founder/CEO; runs the joint working session, owns the data-contract decision record |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `SCADA-1` | Confirm the change: no direct CygNet API — restate the requirement | Taikun · Taikun eng (Steve Ridder co-author) | Kickoff | 0.5 | 🔴 | — |
| `SCADA-2` | Joint working session with Mike + Darko to pick the intermediary pipe | Joint · Mike (Cygnet) + Darko (gateway/security) + Taikun eng; Sebastian on call if FMP is a candidate | Kickoff | 1 | 🔴 | `SCADA-1` |
| `SCADA-3` | Tag-coverage probe: verify the chosen pipe actually carries all 6 signals | Joint · Mike + (Sebastian if FMP / IFS Merrick owner if ProCount) + Taikun eng | Kickoff | 1.5 | 🔴 | `SCADA-2` |
| `SCADA-4` | Adapt the Cygnet data contract to the chosen pipe (keep wire shape stable) | Taikun · Taikun eng | Bootstrap | 2 | 🔴 | `SCADA-3` |
| `SCADA-5` | Define logical->physical (->intermediary) tag map as Mike-owned config | TEEP · Mike (authors mapping); Taikun eng (provides config schema + loader) | Bootstrap | 2 |  | `SCADA-3` |
| `SCADA-6` | Bootstrap SCADA data: signed S3 JSON drops shaped as the contract | TEEP · Darko + Mike (Cygnet data export) | Bootstrap | 2 | 🔴 | `SCADA-4` |
| `SCADA-10` | Conditional historian/read-replica fallback design (if intermediary resolution insufficient) | Joint · Mike + Darko (source/governance) + Taikun eng (adapter) | Build | 2 |  | `SCADA-9` |
| `SCADA-7` | Build the Taikun Cygnet adapter + the 4 cygnet read tools | Taikun · Taikun eng | Build | 4 | 🔴 | `SCADA-4`, `SCADA-6` |
| `SCADA-8` | Wire Cygnet tools into Maxwell's parallel enrichment + classify cascade | Taikun · Taikun eng | Build | 2 |  | `SCADA-7` |
| `SCADA-9` | Replay 95 Cygnet-cited Jan-2026 alerts: resolution + accuracy validation | Taikun · Taikun eng | Build | 3 | 🔴 | `SCADA-8` |
| `SCADA-11` | Production cutover: point Cygnet adapter from bootstrap to the live pipe | Joint · Taikun eng + Darko (gateway/auth) + Mike (live pipe ready) | Cutover | 2 | 🔴 | `SCADA-7`, `SCADA-9`, `GW-OAUTH2` |
| `SCADA-12` | Operate: SCADA health monitoring, degradation alerts, tag-map drift watch | Taikun · Taikun eng (Mike consulted on tag-map drift) | Operate | 2 |  | `SCADA-11` |

**Deliverables:** SCADA intermediary decision record (ADR-style) *(Joint)*; 6-tag availability matrix *(Joint)*; Revised teep-api.yaml Cygnet contract + bootstrap exemplar *(Taikun)*; Mike-owned tag_map.yaml + Taikun loader *(TEEP)*; Signed-S3 Cygnet bootstrap drops *(TEEP)*; teep.cygnet.* tool suite + intermediary->contract adapter *(Taikun)*; SCADA resolution-impact replay report (95-alert) *(Taikun)*; Live Cygnet integration (config-only cutover) *(Joint)*; SCADA operate runbook + health/degradation alerts *(Taikun)*; Conditional historian/replica/export fallback design *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Which existing intermediary pipe sources the 6 SCADA signals — FMP/TaskHub (~30-min device poll), ProCount (IFS OData, documented Cygnet integration), or a CygNet historian / read-replica / curated S3 export? | Joint (Mike + Darko) | Use a CygNet historian/curated-export feed if one exists (closest to the original 4h@5m fidelity, cleanest contract fit); fall back to FMP's ~30-min cache for state corroboration only if the historian is not exposable, and accept reduced series resolution as a documented Phase-1 limitation. Decide in the SCADA-2 working session. | Kickoff week 0 (gates the adapter build and KPI grounding). |
| Does the chosen pipe carry casing pressure and compressor suction/discharge pressure, or only tubing/line pressure + a compressor status flag? | TEEP (Mike; Sebastian if FMP; IFS Merrick owner if ProCount) | Assume tubing/line pressure + sales rate + a compressor status flag are present; treat casing pressure and compressor suction/discharge as at-risk and verify in SCADA-3 — if absent, mark nullable and accept the compressor-specific (~8/168) and full-multi-system (~14/168) pattern weakening for Phase 1. | Kickoff (SCADA-3, before contract revision SCADA-4). |
| Is the ~30-min FMP/TaskHub resolution acceptable for Phase 1 given the original requirement was a 4-hour pre-event series at 5-minute steps? | Joint (Taikun recommends; TEEP/customer accepts) | Accept coarse current-state for the live classify decision, accept reduced series resolution as a documented Phase-1 limitation, and put full-resolution historian series on the Phase-2 roadmap — but only if the SCADA-9 replay shows accuracy/auto-close still clear Phase-1 targets. | After SCADA-9 replay, before cutover (SCADA-11). |
| Pad-level vs well-level aggregation for the SCADA tags served through the intermediary? | TEEP (Mike) | Serve both (per 04-system-integrations.md §2.5); default Maxwell to pad-level for the alert's pad and drill to well-level when the alert names a well. | Bootstrap (SCADA-5 tag-map / SCADA-6 drops). |
| Is the liquids-unloading event flag computed anywhere outside CygNet, or is it lost when CygNet is not directly exposed (it is cited in only 1/168 notes)? | TEEP (Mike) | Treat liquids-unloading as optional/best-effort for Phase 1 (1/168 evidence); drop it if no intermediary computes it and let those rare events flow through the standard pressure/LO-note path; revisit in Phase 2. | Kickoff (SCADA-3). |
| Which native ID does the chosen pipe key on (FMP pad/device IDs vs ProCount well IDs vs historian point/asset IDs), and how does it bind to the canonical asset registry? | Joint (Taikun asset-registry + Mike) | Feed the pipe's native IDs into the asset-registry workstream's per-system binding strategy for the 'cygnet' system (Path A ingest); bind via pad/well FK where the pipe exposes pad/well, fuzzy+number-aware match otherwise. | Bootstrap (SCADA-4/6), coordinated with the asset-binding workstream. |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Chosen intermediary's cadence (esp. FMP ~30-min poll) is too coarse to reproduce the intra-event pressure transients the original 4h@5m series needed, degrading classification accuracy and auto-close rate below Phase-1 targets (>=92% accuracy, 40% auto-close). | H | H | Quantify the degradation early via the 95-alert replay (SCADA-9) before committing; default to a hybrid that uses best-available current-state for the live decision and accepts reduced series resolution as a documented Phase-1 limitation; hold the historian/replica fallback (SCADA-10) ready; renegotiate the SCADA-attributable slice of the KPIs honestly with the customer if needed. | Taikun |
| The chosen pipe simply does not carry all 6 tags — casing pressure and compressor suction/discharge are the most at risk in FMP/ProCount — silently weakening the compressor-specific (~8/168) and full-multi-system (~14/168) classification patterns. | H | M | SCADA-3 tag-coverage probe verifies presence per-tag against real pipe data before any build; mark absent tags nullable in the contract (no faked values); accept the specific pattern weakening explicitly or source the missing tags from the historian fallback. | Joint |
| Intermediary decision (SCADA-2) stalls on TEEP availability (Mike + Darko + possibly Sebastian/IFS owner must align), pushing the whole SCADA critical path and the 6-8 week Phase-1 target. | M | H | Front-load SCADA-1/2 in Kickoff with a fixed working-session date; let Taikun proceed against the existing tested contract via bootstrap S3 drops so adapter/tool dev (SCADA-7) is not fully blocked on the final pipe; pre-circulate the candidate-pipe agenda so the session is a decision, not a discovery. | Joint |
| ProCount, if chosen, is production-accounting-grade (daily/allocation cadence + lag) — good for sales-rate and code corroboration but unsuitable for live intra-event pressure confirmation, leading to a pipe that technically 'works' but doesn't deliver the timeliness Maxwell needs. | M | M | In SCADA-2, evaluate cadence/latency explicitly, not just tag presence; prefer ProCount only as a corroborating source layered on a fresher pipe for pressures; document that ProCount alone cannot satisfy the live pressure-drop signal. | Joint |
| Read-replica/historian bootstrap or fallback hits TEEP IT-security policy (Darko stated direct DB access is temporary-bridge-only / not permitted long-term), blocking the highest-fidelity option. | M | M | Default to the signed-S3 curated-export form of the historian feed (no DB grant) which Darko's security posture already accepts for bootstrap; treat any direct replica as an explicitly time-boxed bridge; keep the same wire contract so the source can be swapped without code change. | TEEP |
| Tag-map drift: when TEEP replaces field equipment the underlying CygNet point changes; if the mapping lives anywhere but Mike's config, SCADA reads silently break (a per-CLAUDE.md 'never hardcode SCADA tag names' violation). | M | M | Keep logical->physical->intermediary mapping in Mike-owned tag_map.yaml (SCADA-5); add a tag-map drift watch in the operate runbook (SCADA-12) that alerts on a tag going persistently null; Taikun loader validates config rather than embedding tag names. | TEEP |
| KPI grounding rests on only 22 days (Jan-2026) of data; the 95-alert replay may over- or under-state the intermediary's adequacy, so a Phase-1 go decision could be miscalibrated. | M | M | Flag the 22-day limitation in the SCADA-9 report; request the 6-12mo historical SCADA pull (a Phase-2 nice-to-have in §7) to firm the numbers; re-validate the resolution-impact metrics against live data in operate (SCADA-12) before hardening KPI commitments. | Taikun |

---

### IFS · ProCount + Carte (IFS Merrick) Integration — production codes, comments, work orders, injection-rate enrichment + canonical asset spine

**Lead:** Taikun  ·  **Effort:** ~31 person-days  ·  **Tasks:** 11

**Objective.** Give Maxwell read access to the enrichment signals that appear in the real resolution notes — ProCount down/up codes, operator comments, and field work orders (56/168 alerts) plus injection-rate drops surfaced via Carte (22/168) — through the IFS Foundation REST/OData layer, and establish ProCount as the canonical asset spine that WS-REG binds Cygnet/Sensirion/Carte/TaskHub onto. Decide go/no-go on a separate Carte API (default: drop it, serve injection_rate from ProCount). Deliver a Taikun-side adapter that emits the exact teep-api.yaml ProCount/Carte shapes whether sourced from bootstrap S3 drops or the live gateway, so cutover is config-only.

ProCount and Carte were not on the original call — they were discovered by mining the 168 real January-2026 resolution notes, where Devin repeatedly cites "Codes and Comments within ProCount" and "a drop in injection via ProCount/Carte." That makes this workstream evidence-driven rather than speculative: ProCount appears in 56/168 alerts and Carte in 22/168, so we are sizing the integration to actual usage. The agent needs three things from ProCount per Sensirion event — down/up codes (to separate planned shutdowns from unplanned releases), the operator free-text comments attached to those codes (classification context), and field work orders (confirm planned maintenance/venting) — pulled for the window [event_start - 24h, event_start + 4h]. From Carte it needs injection-rate (and optionally sales-rate) series to detect the compressor/shut-in drop signature. Volume is low (one or two calls per event, low single digits per pad per day).

The structural advantage here is that ProCount and Carte are both IFS Merrick products sharing one underlying data store, exposed via IFS Foundation REST / OData. This is "expose an existing vendor API through the gateway," not a net-new build, and TEEP very likely already holds an IFS support contract. The first hard decision is whether Carte even needs a separate pipe: Carte is the reporting/visualization layer on top of ProCount's allocation output, so the recommended default is to drop the separate Carte API and serve injection_rate and sales_rate from ProCount's OData directly, keeping the §2B /v1/carte/... endpoint only as a logical interface that physically routes through ProCount. We keep the contract slot so we can flip to a real Carte source if IFS says the injection fields genuinely live only in Carte.

The second, higher-leverage role for this workstream is the asset spine. Per 07-asset-binding-integration.md §8, ProCount is the best candidate for the canonical well+lease structure because it is the production-accounting master and likely carries the 14-digit API number — which means ProCount, Cygnet, and Carte can all bind by Strategy A (deterministic exact-key join on API number) rather than fuzzy name matching. So the recommended build order across the whole pilot is: build the canonical spine from ProCount first, then bind everything else onto it. WS-REG depends on the ProCount catalog dump landing early; this workstream is therefore on the critical path for asset resolution, even though its enrichment endpoints are not themselves go-live blockers in the same way the Sensirion detection path is.

What is genuinely uncertain: (1) the TEEP business owner for ProCount/Carte is literally TBD in the docs — nobody on the call owned it — and identifying that person is the gating first task. (2) The IFS Foundation OData catalog, entity names, and auth flow are vendor-specific and not yet in hand; we know it is OData but not which entities map to codes/comments/work-orders/injection, nor whether the OData auth is OAuth2-compatible with the TEEP gateway or needs a separate IFS service account. (3) The down/up code taxonomy is unknown — we do not yet have the list of valid code values nor which are "planned-emission" types, and that taxonomy is what drives the Process-Emissions vs Unexpected classification. (4) Whether ProCount reliably exposes the API number per well (deterministic binding) or only a well name (fuzzy). These are recorded as open_decisions with recommended defaults rather than fabricated.

**Key people**

| Name | Org | Role |
|---|---|---|
| Owner TBD (production accounting) | TEEP | ProCount/Carte business owner — must be identified at kickoff (likely Michelle or the production-accounting / regulatory-reporting team that files RRC Form PR) |
| Darko Jankovic | TEEP | Engineering / API gateway / security — owns IFS Foundation REST/OData exposure through the gateway and the bootstrap S3 catalog drops |
| Michelle | TEEP | Likely ProCount owner per 04-§ table; owns Sensirion/TaskHub; confirm whether she also owns the IFS Merrick relationship |
| IFS Merrick support / integration contact | IFS Merrick | Vendor — confirms IFS Foundation REST/OData catalog, auth flow, and whether Carte is satisfiable from ProCount alone; engaged via TEEP's existing support contract |
| Devin | TEEP | MRO triage engineer — source of the real how_cleared notes referencing 'Codes and Comments within ProCount' and 'drop in injection via ProCount/Carte'; validates code taxonomy |
| Mike | TEEP | Cygnet/SCADA owner — relevant because ProCount has a documented Cygnet integration and may be the intermediary path for the Cygnet-via-existing-systems ask |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `IFS-1` | Identify and confirm the TEEP ProCount/Carte business + technical owner | TEEP · Clovis (ops lead) to assign; Michelle to confirm if she owns it | Kickoff | 1 | 🔴 | — |
| `IFS-2` | Confirm IFS Foundation REST/OData catalog, entity model, and auth flow | Joint · TEEP ProCount owner + IFS Merrick support; Taikun eng (Steve Ridder) co-leads | Kickoff | 3 | 🔴 | `IFS-1` |
| `IFS-3` | Carte go/no-go decision — drop separate API, serve injection_rate from ProCount | Joint · TEEP ProCount owner + IFS Merrick support + Taikun eng + Darko | Kickoff | 1 | 🔴 | `IFS-2` |
| `IFS-4` | Capture and confirm the ProCount down/up code taxonomy + planned-emission flags | Joint · Devin (TEEP MRO) + TEEP ProCount owner provide the taxonomy; Taikun eng maps to classification config | Bootstrap | 2 |  | `IFS-2` |
| `IFS-5` | TEEP delivers bootstrap S3 ProCount catalog + sample data drops (Path A unblock) | TEEP · Darko (S3 bucket + signing) + TEEP ProCount owner (extract) | Bootstrap | 4 | 🔴 | `IFS-2`, `IFS-3` |
| `IFS-7` | Provide ProCount catalog as canonical asset spine to WS-REG | Joint · Taikun eng (binding) + TEEP ProCount owner (catalog completeness, API# confirmation) | Bootstrap | 2 | 🔴 | `IFS-5` |
| `IFS-6` | Build the Taikun ProCount/Carte Atlas adapter (bootstrap + gateway-ready) | Taikun · Taikun eng | Build | 6 |  | `IFS-4`, `IFS-5` |
| `IFS-8` | TEEP exposes ProCount (+Carte routing) OData behind the gateway | TEEP · Darko (gateway) + TEEP ProCount owner (IFS routing) + IFS Merrick support | Build | 5 | 🔴 | `IFS-2`, `IFS-3` |
| `IFS-9` | Cutover the ProCount/Carte adapter from bootstrap S3 to the live gateway | Taikun · Taikun eng | Cutover | 2 | 🔴 | `IFS-6`, `IFS-8` |
| `IFS-10` | Validate ProCount/Carte enrichment in end-to-end triage on real events | Joint · Taikun eng + Devin (TEEP) validates classification correctness | Operate | 3 |  | `IFS-9` |
| `IFS-11` | Schedule nightly ProCount catalog diff to keep the registry current | Taikun · Taikun eng | Operate | 2 |  | `IFS-7`, `IFS-8` |

**Deliverables:** IFS Foundation OData discovery note *(Joint)*; Carte go/no-go decision memo *(Joint)*; procount_code_taxonomy.yaml *(Joint)*; Bootstrap S3 ProCount catalog + sample enrichment drops *(TEEP)*; Taikun ProCount/Carte Atlas adapter + FQTN tools *(Taikun)*; ProCount canonical-spine catalog + binding-strategy verdict *(Joint)*; Live gateway ProCount/Carte endpoints *(TEEP)*; Cutover + enrichment validation reports *(Joint)*; Nightly ProCount catalog diff job *(Taikun)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Who is the accountable TEEP owner for ProCount/Carte (IFS Merrick)? The docs say TBD — likely production accounting / the RRC Form-PR-filing team, possibly Michelle. | TEEP | Assign the production-accounting / regulatory-reporting lead as business owner with Michelle confirming; engage IFS Merrick support as the technical contact. | Kickoff (week 0) — gates all IFS discovery tasks |
| Drop the separate Carte API and serve injection_rate/sales_rate from ProCount OData (slide-12 Q3)? | Joint | Yes — drop separate Carte API; route teep-api.yaml /v1/carte/... through the ProCount adapter. Carte is a viewer on ProCount's store and is only 22/168. Flip only if IFS confirms injection lives solely in Carte. | Kickoff working session (before adapter build, IFS-6) |
| Does ProCount expose the 14-digit API number per well (enables deterministic Strategy A binding for ProCount/Cygnet/Carte)? | TEEP | Confirm API# presence in the catalog extract (IFS-7). If present, bind by Strategy A (exact key). If only well_name, use Strategy C fuzzy with the number-aware guard plus per-system normalization. | Bootstrap (weeks 1-2) — gates WS-REG spine binding strategy |
| Is ProCount confirmed as the canonical asset spine for the TEEP fleet (07-§8/§10)? | Joint | Yes — ProCount is the production-accounting master with the most authoritative well+lease structure; build the spine from ProCount first, then bind Cygnet/Sensirion/Carte/TaskHub onto it. | Bootstrap (weeks 1-2) — gates WS-REG build order |
| Does the connector->binding ingest of the ProCount catalog run on a nightly diff or on-demand per sync (07-§10)? | Taikun | Nightly diff — keeps the spine current with low operational overhead; on-demand only for manual re-syncs after large fleet changes. | Operate (post-cutover) — for IFS-11 scheduling |
| Which IFS environment does TEEP expose — production ProCount or a read replica/curated export — and is the OData service reachable from the TEEP gateway network? | TEEP | Expose a read-only path (replica or curated OData service) to keep production accounting isolated; confirm network reachability in IFS-2. | Kickoff (week 0) — gates IFS-2 reachability test |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| ProCount/Carte has no named TEEP owner — it was discovered by mining notes, nobody on the call owned it. Without an owner, IFS-2 (OData discovery), IFS-4 (taxonomy), and IFS-5 (catalog) all stall. | H | H | Make owner assignment the first kickoff action (IFS-1); escalate to Clovis as ops lead if production-accounting ownership is contested; have IFS Merrick support fill the technical gap interim while TEEP names a business owner. | TEEP |
| IFS Foundation OData auth is not OAuth2-compatible with the TEEP gateway (no gateway exists today; OData may want an IFS service account/token), forcing a credential bridge. | M | H | Confirm auth flow early in IFS-2; if incompatible, bridge credentials at the gateway (token exchange) — Darko owns; bootstrap S3 drops (IFS-5) keep Taikun dev unblocked regardless of the live-auth resolution. | TEEP |
| ProCount does NOT expose the 14-digit API number per well, so the canonical spine can only bind other systems by fuzzy name (Strategy C). The dry-run showed ~75-80% auto-link with a ~19% review band on same-fleet names — slower and noisier than deterministic binding. | M | M | Confirm API# presence in IFS-7; if absent, add per-system input normalization (strip formation suffixes / markers) to push the review band toward 90%+, and staff the human review queue early in Bootstrap so binding doesn't gate go-live. | Taikun |
| Carte injection-rate is genuinely sourced only from Carte (not queryable from ProCount entities), invalidating the default drop-Carte decision and adding a second IFS integration. | L | M | Resolve in IFS-3 against the real OData $metadata before committing; keep the teep-api.yaml /v1/carte/... contract slot so flipping to a real Carte source is additive, not a redesign. Carte is only 22/168 (optional) — pilot can proceed without it if needed. | Joint |
| Down/up code taxonomy is incomplete or the planned-emission flags are wrong, causing Maxwell to misclassify Process Emissions vs Unexpected and either over-auto-close (missing real leaks) or under-auto-close (defeating the KPI). | M | H | Reconcile the taxonomy against real January how_cleared notes with Devin in IFS-4; validate end-to-end on >=20 real events in IFS-10 with Devin sign-off; keep classification accuracy under the >=92% P1 KPI gate before auto-close is enabled. | Joint |
| IFS Merrick vendor engagement is slow (separate support contract, change-request cycle) and the live OData exposure (IFS-8) slips past the deck's aggressive week-4 cutover. | M | M | Engage IFS Merrick support in parallel with IFS-1/IFS-2; lean on bootstrap S3 drops (IFS-5) so the full pilot runs on real data while the live gateway lags — cutover (IFS-9) is config-only, so a late gateway delays go-live, not development. | TEEP |

---

### SSO · Identity & SSO — Microsoft Entra ID Federation into the Taikun Platform

**Lead:** Joint  ·  **Effort:** ~15.5 person-days  ·  **Tasks:** 12

**Objective.** Federate TEEP's Microsoft Entra ID into the Taikun (Maxwell) platform so TEEP users sign in with their TotalEnergies corporate credentials via OIDC, with Entra security-group membership mapping to the four Maxwell personas (MRO/operator, HSE-reporting, admin, read-only), MFA/conditional access enforced by TEEP, full login audit, and a clean cutover from the interim username/password auth — with SSO live before TEEP go-live so no shared/manual credentials ever reach production.

This workstream federates TEEP's Microsoft Entra ID into the Taikun platform so the seven named TEEP users (Devin, Sierra, Clovis, Michelle, Mike, Darko, Sahir) sign in to the Maxwell UI — Triage Live, Event Detail, Monthly Report, Integration Health — with their TotalEnergies corporate credentials rather than any shared or manually-issued Taikun account. The brief frames this as net-new, but a major grounding fact reshapes the plan: the platform ALREADY ships a working OIDC EntraID provider (actionengine/modules/core/auth/providers/entraid.py) built on Microsoft's v2.0 endpoints with msal code-exchange, PyJWT ID-token signature/issuer/audience/nonce verification, just-in-time user provisioning (find_or_create_sso_user), JWT session-cookie issuance, and login audit (record_login -> user_audit_log). The SSO router (actionengine/modules/core/auth/api.py) even mounts the customer-friendly /api/auth/microsoft/callback path Microsoft's tutorials assume, and supports per-deployment single-tenant binding plus a configurable post-login default role. So OIDC vs SAML is effectively decided: OIDC/OAuth2 authorization-code flow is built, hardened, and is the recommended path; SAML would be net-new code and is out of scope unless TEEP IT mandates it.

The real engineering gap is role granularity. The platform's UserRole enum has only three values — admin / operator / viewer — and SSO logins are provisioned with a single static default role (SSO_DEFAULT_ROLE / IDP_ENTRAID_DEFAULT_ROLE). There is NO Entra-group-to-role mapping today; the only group feature is IDP_ENTRAID_ALLOWED_GROUPS, which is a coarse allow/deny gate, not a role mapper. The brief's four personas (MRO, HSE-reporting, admin, read-only) therefore require net-new work: define how the personas map onto the three platform roles (recommended: MRO/Clovis -> operator, Sierra -> a reporting persona realised as viewer + reporting/export entitlement, Darko/Sahir/admin -> admin, Mike -> viewer), then implement a deterministic Entra-group-object-ID -> UserRole mapping that runs in the SSO callback, reading the groups claim that TEEP must configure Entra to emit. This is the one piece of code we genuinely build; everything else is configuration, registration, and verification.

Execution is a two-sided handshake. On the TEEP side (Sahir, with Darko's security sign-off) we need an Enterprise Application / app registration in the TEEP Entra tenant, the exact redirect/reply URI registered byte-for-byte (https://<taikun-host>/api/auth/microsoft/callback), a confidential client secret (or certificate) issued to Taikun, a groups claim added to the ID token, four Entra security groups created and the seven pilot users assigned, admin consent granted for the openid/profile/email/User.Read scopes, and a conditional-access/MFA policy decision for these external-app sign-ins. On the Taikun side we set IDP_ENTRAID_TENANT_ID / CLIENT_ID / CLIENT_SECRET, IDENTITY_PROVIDERS=entraid, the group->role map, flip INTERNAL_LOGIN_ENABLED to false at cutover, and restart the runtime. The genuinely uncertain inputs are recorded as open decisions rather than fabricated: the TEEP Entra tenant GUID, who owns admin-consent in the TotalEnergies tenant (Entra app registrations are frequently gated centrally at TotalEnergies group IT, not at the Barnett BU — a real schedule risk), the production Taikun hostname TEEP will whitelist, whether Entra emits group GUIDs vs names (and the >200-group overage caveat), and whether SCIM auto-provisioning is required for the 7-user pilot (recommended default: no — JIT is sufficient at this scale; defer SCIM to Phase 2).

This workstream is largely independent of the data-integration workstreams (Sensirion, FMP/TaskHub, Bedrock, Cygnet) — it gates human access to the UI, not agent-to-API data flow — but it IS a go-live gate: TEEP users cannot be given production access on shared/manual credentials, so SSO cutover must complete before the pilot opens to TEEP users. It can and should run in parallel with the gateway and integration builds. The honest schedule risk is not Taikun engineering (the provider exists) but TotalEnergies-side Entra governance latency: app-registration approval and admin consent in a large enterprise tenant routinely take 1-3 weeks of lead time, which is why the kickoff task to file the registration request is marked blocking and front-loaded into week 0-1.

**Key people**

| Name | Org | Role |
|---|---|---|
| Sahir | TEEP | Cyber & Field Ops (IT/security) — Entra tenant admin counterpart, conditional-access / MFA owner, Enterprise App registration approver |
| Darko Jankovic | TEEP | Engineering / API gateway / security & governance — reviews redirect URLs, claims/data-sharing, security sign-off |
| Devin | TEEP | MRO engineer (triage) — test user / persona: operator-class access to triage + dispatch |
| Sierra | TEEP | HSE reporting coordinator — test user / persona: HSE-reporting access to Monthly Report + export |
| Clovis | TEEP | Operations lead — test user / persona: operator/admin-class oversight |
| Michelle | TEEP | TaskHub/FMP + Sensirion owner — test user / persona: integration-health visibility |
| Mike | TEEP | Cygnet/SCADA owner — test user / persona: read-only / integration-health |
| Steve Ridder | Taikun | Founder/CEO — Taikun-side approver, account/redirect-URL owner |
| Taikun engineering | Taikun | Implements group->role mapping, deploys IDP_ENTRAID_* config, runs cutover |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `SSO-1` | SSO discovery + OIDC-vs-SAML decision with TEEP IT | Joint · Taikun eng + Sahir (TEEP IT) + Darko (TEEP) | Kickoff | 1 | 🔴 | — |
| `SSO-2` | File Enterprise Application / app-registration request in TEEP Entra tenant | TEEP · Sahir (Cyber & Field Ops) with Darko sign-off | Kickoff | 2 | 🔴 | `SSO-1` |
| `SSO-3` | Define persona -> platform-role mapping (MRO / HSE-reporting / admin / read-only) | Joint · Taikun eng + Clovis (ops) + Sierra (HSE) + Sahir | Bootstrap | 1 |  | `SSO-1` |
| `SSO-4` | Create Entra security groups + emit groups claim + assign pilot users | TEEP · Sahir (Cyber & Field Ops) | Bootstrap | 1 |  | `SSO-2`, `SSO-3` |
| `SSO-5` | Implement Entra group-object-ID -> UserRole mapping in the SSO callback | Taikun · Taikun engineering | Build | 3 | 🔴 | `SSO-3` |
| `SSO-6` | Decide & register production redirect/reply URL and Taikun hostname | Joint · Steve Ridder (Taikun) + Sahir (TEEP) | Build | 1 | 🔴 | `SSO-1` |
| `SSO-7` | Deploy IDP_ENTRAID_* config to Taikun runtime (non-prod / test VM) | Taikun · Taikun engineering | Build | 1 |  | `SSO-2`, `SSO-4`, `SSO-5`, `SSO-6` |
| `SSO-8` | Conditional Access / MFA review for the Taikun enterprise app | TEEP · Sahir (Cyber & Field Ops) + Darko | Build | 1 |  | `SSO-2` |
| `SSO-9` | End-to-end SSO login test with all 4 personas (test users) | Joint · Taikun eng + Sahir; persona testers Devin, Sierra, Clovis, Michelle, Mike | Build | 2 | 🔴 | `SSO-7`, `SSO-8` |
| `SSO-10` | Production cutover: enable EntraID SSO + disable interim auth | Taikun · Taikun engineering (with Steve approval) | Cutover | 1 | 🔴 | `SSO-9` |
| `SSO-11` | SSO operations runbook, offboarding, and group-membership handoff to TEEP | Joint · Taikun eng + Sahir | Operate | 1 |  | `SSO-10` |
| `SSO-12` | (Conditional) SCIM auto-provisioning evaluation — defer to Phase 2 | Joint · Taikun eng + Sahir | Operate | 0.5 |  | `SSO-1` |

**Deliverables:** SSO approach decision memo *(Joint)*; Entra Enterprise Application registration + credentials *(TEEP)*; Persona-to-role mapping spec *(Joint)*; Entra security groups + groups-claim configuration *(TEEP)*; Group-object-ID -> UserRole mapping implementation *(Taikun)*; Applied Conditional Access / MFA policy *(TEEP)*; SSO acceptance test report *(Joint)*; Production SSO cutover + rollback runbook *(Taikun)*; SSO operations / offboarding / secret-rotation runbook *(Joint)*; SCIM decision note *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| What is the TEEP/TotalEnergies Entra (Azure AD) tenant GUID that the Taikun deployment binds to? | TEEP | Obtain the single-tenant GUID from Sahir during SSO-1; do not proceed to SSO-7 config without it. Do not fabricate — it is the IDP_ENTRAID_TENANT_ID value. | Before SSO-2 (registration) / SSO-7 (runtime config) — week 1. |
| Who owns Enterprise Application registration and admin consent in the TotalEnergies Entra tenant — the Barnett BU (Sahir) or central TotalEnergies group IT? | TEEP | Assume Sahir can drive it within the BU; if central group IT owns it, escalate via Darko in week 0 because approval lead time is the top schedule risk. | SSO-1 / start of SSO-2 — week 0-1. |
| What is the production Taikun hostname TEEP users will sign in at, and therefore the exact registered redirect URI? | Joint | Default to the existing customer-facing demo.taikunai.com host (callback https://demo.taikunai.com/api/auth/microsoft/callback) unless TEEP prefers a TEEP-branded host like taikun.totalenergies.us; set AUTH_PUBLIC_BASE_URL to match given the CloudFront origin split. | SSO-6, before SSO-2 registration finalises — week 1-2. |
| Does Entra emit group object-IDs (GUIDs) or names in the groups claim, and are all pilot users under the ~200-group overage threshold? | TEEP | Use group GUIDs (immutable) as the mapping key; confirm pilot users are well under the overage limit; if not, switch to app-role assignments or a Graph membership lookup. | SSO-4 / SSO-5 — week 2-3. |
| Is SCIM auto-provisioning required, or is JIT provisioning sufficient for the Phase-1 pilot? | Joint | JIT-only for Phase 1 (no SCIM endpoint exists today; 7 users; role re-evaluated each login). Defer SCIM to Phase 2 unless TEEP requires deprovisioning faster than session expiry. | SSO-1 (decision) — week 1; revisit before Operate. |
| Is the platform's 3-role model extended with a dedicated HSE-reporting role, or is Sierra's persona realised as viewer + reporting/export entitlement? | Taikun | Realise HSE-reporting as viewer + reporting/export entitlement to avoid a schema change, unless SSO-3 review shows a 4th role is cleaner. | SSO-3, before SSO-5 implementation — week 2. |
| What Conditional Access / MFA posture applies to the Taikun enterprise app, and is it compatible with the OIDC authorization-code flow + groups claim? | TEEP | Require MFA (TotalEnergies standard for corporate apps) plus standard device/location policy; verify the policy does not strip the groups claim or block the token endpoint. | SSO-8, before SSO-9 acceptance test — week 3-4. |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| TotalEnergies-side Entra app-registration + admin consent is owned/gated by central group IT (not the Barnett BU) and takes 1-3+ weeks, blocking the go-live gate and pushing the pilot past the deck's aggressive 8-week target. | H | H | Front-load SSO-2 in week 0-1 as a blocking task; resolve the consent-owner open decision in SSO-1; escalate to Darko immediately if central group IT owns it. Interim username/password auth keeps the pilot functional for Taikun-side demos while the registration is pending, so SSO latency does not block the data-integration workstreams. | TEEP |
| Entra does not emit the groups claim (or hits the >200-group overage that replaces the claim with a Graph link), so group->role mapping silently fails and users land in the wrong role. | M | H | SSO-4 explicitly configures the token groups claim and confirms pilot users are under the overage limit; SSO-5 fails closed (deny login) rather than defaulting to a privileged role when the claim is absent; SSO-9 verifies the claim is present and roles are correct before cutover. If overage is unavoidable, fall back to a Graph group-membership lookup or app-role assignments instead of the raw groups claim. | Joint |
| Disabling interim auth (INTERNAL_LOGIN_ENABLED=false) at cutover locks everyone out if the EntraID config is subtly wrong (e.g. tenant GUID typo, mismatched redirect URI, expired secret). | M | H | Cutover (SSO-10) only after SSO-9 acceptance passes on the test VM with the same config; retain a documented break-glass internal admin account out-of-band; provide instant rollback (re-enable internal login + restart). Do not reset the VM off origin/main during cutover (prior incident broke auth that way). | Taikun |
| Redirect/reply URI mismatch — Microsoft compares the callback URI byte-for-byte, and the demo VM sits behind CloudFront (origin split), so the host the runtime sees may differ from the registered URI. | M | M | SSO-6 pins the exact production host and sets AUTH_PUBLIC_BASE_URL / honors x-forwarded-host per the router's existing proxy-aware logic; register the precise URI in SSO-2; verify the round-trip in SSO-9 before production. | Joint |
| Conditional Access / MFA policy applied by TEEP IT breaks the OIDC flow (e.g. blocks the token endpoint, forces an unsupported grant, or strips the groups claim). | L | M | SSO-8 has TEEP confirm the CA policy is compatible with the authorization-code flow before SSO-9; SSO-9 tests with MFA live; document CA policy IDs so login failures can be triaged against them. | TEEP |
| Client secret expiry (Entra secrets are time-bounded) silently breaks production SSO weeks/months into the Operate phase. | M | M | SSO-11 sets a rotation reminder with a named owner ahead of expiry and prefers a certificate or a long-but-tracked secret lifetime; monitor for 401s from the token endpoint and alert. | Joint |
| The platform's 3-value role model (admin/operator/viewer) does not cleanly express the HSE-reporting persona, leading to Sierra getting either too much access (operator) or insufficient export rights (plain viewer). | M | M | SSO-3 explicitly resolves this — either extend UserRole with a reporting role or realise HSE-reporting as viewer + a reporting/export entitlement; verified per-persona in SSO-9 against the actual Maxwell screens. | Taikun |

---

### BEDROCK · Bedrock — LLM Inference via TEEP AWS Bedrock

**Lead:** Taikun  ·  **Effort:** ~22 person-days  ·  **Tasks:** 11

**Objective.** Route Maxwell's production LLM inference through TEEP's own AWS Bedrock instance (Claude models) instead of the default direct-Anthropic / OpenAI path, so that all TEEP prompt + response data stays inside TEEP's AWS governance boundary, while preserving classification accuracy (>=92% Phase-1 KPI), keeping per-event latency within the ~4s context-injection budget, and providing a clean fallback if Bedrock model access lags.

This workstream is almost entirely a gateway-configuration and validation effort, not a code rewrite — and that is the single most important fact to communicate to the customer. ADR-0004 (the Inference Gateway) was explicitly motivated by "TEEP / Total Energy mandates AWS Bedrock as their LLM hub for governance and data residency." The LiteLLM-based gateway (`actionengine/services/llm_gateway/`) is already live on both Taikun VMs on git main; ~50 call sites — including Maxwell's triage call in `emissions_api.py`, which uses `get_async_client()` from `actionengine.services.llm_gateway` — already egress through it. Switching Maxwell from OpenAI gpt-4o to a TEEP-Bedrock Claude model is therefore a routing-table + credentials change in one service, not an edit to Maxwell. The one code nit: the Maxwell triage call currently passes a concrete model string (`TAIKUN_LLM_MODEL`, default `gpt-4o`) rather than the logical name `taikun-chat`; we switch it to the logical name so the per-tenant routing table can resolve TEEP -> Bedrock with zero further Maxwell changes (this is exactly ADR-0004's P2/P3 design).

The genuinely uncertain and dependency-heavy parts are all on the TEEP/AWS side and are captured as open decisions, never invented: the TEEP AWS account id, the Bedrock region, and which Claude model(s) are enabled in that account. AWS Bedrock requires explicit per-account, per-region "model access" enablement for Anthropic Claude models, and that request can take hours to days of internal AWS/procurement approval — it is the critical-path long-pole and the reason the deck's aggressive 8-week timeline is at risk for the production-Bedrock cutover specifically. We de-risk this by (a) submitting the model-access request as the very first action, and (b) keeping the existing direct-Anthropic / OpenAI gateway route live as the fallback so the pilot is never blocked on Bedrock — the gateway's fallback-chain and per-tenant routing already support exactly this degrade-don't-fail posture.

The chosen approach is cross-account AssumeRole (no static AWS keys), per ADR-0004 and Darko's "no static credentials" directive in 05-security-answers.md §C.1: TEEP creates an IAM role in their account that trusts the Taikun gateway's principal with an external-id, scoped to `bedrock:InvokeModel` on the enabled Claude model ARNs. The Taikun gateway's LiteLLM Bedrock provider assumes that role per call. We then validate four things before cutover — classification parity against the 168-alert labelled sample (Bedrock Claude vs the OpenAI baseline must hold >=92% agreement on the three Sierra resolution_type values), latency (Bedrock Claude in TEEP's region must stay inside the ~4s context-injection budget from 03-architecture.md §4.2), cost per event, and PII redaction (prompts carry pad/well identifiers and free-text LO notes — these must be redacted before egress and verified in the audit ledger which already logs metadata-only).

Two architectural questions are real and recorded as open decisions rather than assumed away. First, data residency: 05-security-answers.md §D.4 commits all TEEP processing to AWS us-east-1; if TEEP's Bedrock region is also us-east-1 the cross-account call stays in-region, but ADR-0004 notes that for true in-boundary residency the gateway itself should run "in/near the customer's AWS account/VPC." For a Phase-1 pilot the recommended default is the lighter cross-account-AssumeRole-from-Taikun-us-east-1 pattern, with co-located-gateway deferred to Phase 2 unless TEEP security mandates it now. Second, embeddings: ADR-0004 flags that Bedrock Titan embeddings are a different (1024-dim) vector space requiring a one-time re-embed. This matters ONLY if Maxwell uses semantic/Stratum retrieval for TEEP; the Phase-1 context-injection classifier (03-architecture.md §4.2) builds its context from SQL over emissions.* plus the four live API reads — it does not depend on pgvector embeddings for the triage decision — so embedding migration is explicitly out of scope for this workstream's Phase-1 cutover and flagged as a watch-item only.

**Key people**

| Name | Org | Role |
|---|---|---|
| Darko Jankovic | TEEP | Engineering / API gateway / security & governance — owns the data-governance mandate behind routing Maxwell LLM calls through TEEP Bedrock; co-approves cross-account IAM trust and data-residency boundary |
| Sahir | TEEP | Cyber & Field Ops (IT/security) — owns the TEEP AWS account, Bedrock model-access enablement, and the cross-account IAM role/external-id; same person on the Entra/SSO workstream |
| Steve Ridder | Taikun | Founder/CEO — commercial owner, Bedrock-cost and data-residency sign-off |
| Taikun engineering | Taikun | LLM gateway (LiteLLM) owners — wire the Bedrock route, per-tenant routing table, parity/latency/cost validation, fallback config |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `BEDROCK-1` | Confirm TEEP Bedrock account, region, and enabled Claude models | Joint · Taikun eng + Sahir (TEEP) + Darko (TEEP) | Kickoff | 1 | 🔴 | — |
| `BEDROCK-2` | Request/enable Anthropic Claude model access in TEEP Bedrock | TEEP · Sahir (TEEP) — with model-id list from Taikun eng | Bootstrap | 3 | 🔴 | `BEDROCK-1` |
| `BEDROCK-3` | Provision cross-account IAM AssumeRole for the Taikun gateway | TEEP · Sahir (TEEP) + Darko (TEEP) — Taikun eng supplies principal ARN | Bootstrap | 2 | 🔴 | `BEDROCK-1` |
| `BEDROCK-4` | Add a TEEP Bedrock provider + route to the LiteLLM gateway | Taikun · Taikun eng (LLM gateway) | Build | 2 | 🔴 | `BEDROCK-2`, `BEDROCK-3` |
| `BEDROCK-5` | Point Maxwell triage at the logical model name (de-hardcode gpt-4o) | Taikun · Taikun eng | Build | 1 |  | `BEDROCK-4` |
| `BEDROCK-6` | Classification parity: Bedrock Claude vs OpenAI baseline on the 168-alert sample | Taikun · Taikun eng | Build | 3 | 🔴 | `BEDROCK-5` |
| `BEDROCK-7` | Latency + cost validation against the per-event budget | Taikun · Taikun eng | Build | 2 |  | `BEDROCK-5` |
| `BEDROCK-8` | PII redaction + data-governance verification on the Bedrock egress | Joint · Taikun eng + Darko (TEEP, governance sign-off) | Build | 2 | 🔴 | `BEDROCK-5` |
| `BEDROCK-9` | Fallback strategy if Bedrock model access or capacity lags | Joint · Taikun eng (build) + Darko (TEEP, approves degraded-mode policy) | Build | 2 |  | `BEDROCK-4` |
| `BEDROCK-10` | Production cutover: TEEP tenant default route -> Bedrock | Taikun · Taikun eng | Cutover | 1 | 🔴 | `BEDROCK-6`, `BEDROCK-7`, `BEDROCK-8`, `BEDROCK-9` |
| `BEDROCK-11` | Operate: monitor Bedrock route health, cost, drift; weekly review | Taikun · Taikun eng (with Sahir on credential rotation) | Operate | 3 |  | `BEDROCK-10` |

**Deliverables:** TEEP Bedrock facts sheet *(Joint)*; Enabled Claude model ARNs in TEEP Bedrock *(TEEP)*; Cross-account AssumeRole + external-id *(TEEP)*; TEEP Bedrock gateway route *(Taikun)*; Maxwell logical-model PR *(Taikun)*; Bedrock parity report *(Taikun)*; Bedrock latency + cost report *(Taikun)*; Data-governance sign-off memo *(Joint)*; Bedrock fallback policy *(Joint)*; Production cutover record *(Taikun)*; Operate-phase Bedrock runbook + dashboard *(Taikun)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| What is the TEEP AWS account id that hosts Bedrock for Maxwell? | TEEP | Unknown — must be supplied by Sahir; DO NOT assume. Needed to write the cross-account trust policy and routing config. | Bootstrap (before BEDROCK-3 IAM role can be created) |
| Which Bedrock region will Maxwell use? | TEEP | us-east-1 (to match Taikun's gateway region and the data-residency commitment in 05-security-answers.md §D.4, minimizing cross-region latency) — confirm with Sahir; do not assume if TEEP standardizes on another region. | Kickoff/Bootstrap (BEDROCK-1, feeds BEDROCK-2/3/4) |
| Which Anthropic Claude model id(s) are (or will be) enabled in the TEEP Bedrock account/region? | TEEP | Enable a Claude 3.5 Sonnet-class model (primary, taikun-chat) + a Claude 3 Haiku-class model (fast/fallback, taikun-chat-fast), per ADR-0004's TEEP routing example — confirm against TEEP's actual enterprise model allow-list. | Bootstrap (BEDROCK-2; gates the parity/latency tests and cutover) |
| Does TEEP require the LLM gateway to run inside their AWS account/VPC for true in-boundary residency, or is cross-account AssumeRole from the Taikun us-east-1 gateway acceptable for the Phase-1 pilot? | TEEP | Cross-account AssumeRole from Taikun us-east-1 for the pilot (lighter, faster, prompts still land in TEEP's own Bedrock); defer co-located/in-VPC gateway to Phase 2 unless Darko mandates it now. | Build (BEDROCK-8 governance sign-off; affects deployment topology) |
| Is a non-TEEP-Bedrock fallback (OpenAI/Anthropic direct) an acceptable temporary degraded mode during the Bedrock model-access-lag window, given prompts would briefly leave the TEEP boundary? | TEEP | Approve as an explicit, opt-in, time-boxed bootstrap allowance with PII redaction on, OFF by default in production — Darko's written sign-off required (BEDROCK-9). | Build (BEDROCK-9; determines whether the pilot can run before Bedrock access lands) |
| Should a Bedrock Guardrail be applied to Maxwell's egress in Phase 1 (ADR-0004 references guardrail_id), or deferred to Phase 2? | TEEP | Defer to Phase 2 — Phase-1 PII redaction at the gateway plus metadata-only logging meets the governance bar; revisit if Darko mandates a TotalEnergies Guardrail standard. | Build (BEDROCK-8) |
| What is the AssumeRole external-id and credential/rotation cadence, and over what secure channel is the external-id delivered? | TEEP | TEEP-issued external-id delivered out-of-band (not email body); rotate role/external-id on TEEP's standard IAM cadence; Taikun supplies its gateway principal ARN (assumption: dedicated role in Taikun account 584673484283). | Bootstrap (BEDROCK-3) |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Bedrock Claude model-access approval in the TEEP AWS account lags (hours-to-days, plus possible TotalEnergies internal procurement/security review), blocking the production-Bedrock cutover and pushing past the deck's aggressive 8-week target. | H | M | Submit the model-access request first (BEDROCK-2) at kickoff; run the entire pilot on the existing OpenAI/Anthropic gateway route in parallel (BEDROCK-9 fallback) so go-live is never blocked on Bedrock — only the production-Bedrock cutover slips, not the pilot. | TEEP |
| Classification accuracy regresses on Bedrock Claude vs the OpenAI baseline (different JSON-mode/prompt behavior; ADR-0004 calls cross-provider normalization 'best-effort'), dropping below the >=92% KPI or mis-routing the costly 27% Unexpected dispatch path. | M | H | Gate cutover on the BEDROCK-6 parity report over the 168-alert labelled sample with a strict <=3% divergence + 100% JSON-validity bar; tune the system prompt for Bedrock and re-run; keep OpenAI fallback until parity holds. | Taikun |
| Cross-account + cross-region Bedrock InvokeModel adds latency that blows the ~4s per-event triage budget, especially if TEEP's Bedrock region is not us-east-1. | M | M | Measure p95/p99 early (BEDROCK-7); prefer a TEEP Bedrock region == us-east-1 to match the gateway's region (BEDROCK-1 open decision); route confident/deterministic cases to the faster Haiku tier; if persistent, evaluate co-locating the gateway in TEEP's region (Phase 2). | Taikun |
| External-id or role ARN delivered over an insecure channel, or the trust policy is over-broad (bedrock:* / missing external-id), creating a confused-deputy / privilege-escalation exposure. | L | H | Enforce least-privilege (InvokeModel on the specific enabled ARNs only) + mandatory external-id; deliver external-id out-of-band (not email body); store as a reference in llm-config which rejects raw secrets; STS dry-run before wiring (BEDROCK-3). | TEEP |
| True data-residency expectation mismatch: TEEP may require the gateway itself to run inside their AWS account/VPC (ADR-0004 notes this for in-boundary residency), not the lighter cross-account-from-Taikun-us-east-1 pattern, forcing a heavier deployment than Phase 1 budgets. | M | M | Pin the residency requirement explicitly in BEDROCK-1 and the BEDROCK-8 sign-off; recommend cross-account-AssumeRole for the Phase-1 pilot and defer co-located gateway to Phase 2 unless Darko mandates it now — surfaced as an open decision. | Joint |
| Scope creep into embedding-space migration: ADR-0004 flags Bedrock Titan embeddings as a separate 1024-dim space needing a one-time re-embed; if anyone assumes Bedrock means re-embedding Maxwell's corpus, the workstream balloons. | L | M | Phase-1 Maxwell is context-injection (SQL + 4 live API reads), not pgvector-dependent for the triage decision (03-architecture.md §4.2) — explicitly scope embeddings OUT of this workstream and flag as a Phase-2 watch-item only. | Taikun |
| Bedrock per-account throttling / service quotas on InvokeModel cause intermittent failures under burst (e.g. a multi-event spike), degrading triage availability. | L | M | Gateway already does num_retries=3 with backoff; configure the fallback chain (BEDROCK-9) to the fast tier then the approved fallback; request a quota increase via Sahir if the ~230/mo + burst profile approaches limits. | Joint |

---

### GW · API Gateway Platform, Security Conventions & Bootstrap

**Lead:** TEEP  ·  **Effort:** ~53 person-days  ·  **Tasks:** 17

**Objective.** Stand up the TEEP-side API gateway foundation that every Maxwell endpoint depends on — OAuth2 client-credentials (or mTLS) auth, a 24h Idempotency-Key replay store, HMAC-SHA256 webhook signing, and an RFC7807 problem+json error envelope — and run the parallel bootstrap track (signed S3 JSON drops for all 5 systems, shaped exactly like the future REST responses) so Taikun development is unblocked from week 1 and live cutover is a config change, not a rewrite.

This is Darko's foundation workstream. TEEP has no API gateway today — on the 2026-05-14 call Darko said they "will have to be creative in the beginning" — so the plan deliberately runs two tracks in parallel. Track 1 (Bootstrap) gets Taikun building against real TEEP data within days: TEEP drops signed S3 JSON files for all five systems (Sensirion, Cygnet, TaskHub/FMP, ProCount, Carte) shaped exactly like the future REST responses defined in teep-api.yaml. Sierra's xlsx is already effectively "bootstrap-0" for Sensirion (168 Jan-2026 alerts ingested on main); we extend the same pattern to the other four. Because Taikun's source adapters read the same JSON shape regardless of origin, the eventual cutover to the live gateway is a base-URL/config change, not a code rewrite — this is the central architectural bet of the whole pilot and it is what keeps the 6-8-week target credible. Track 2 (Gateway platform) builds the cross-cutting plumbing defined in 04-system-integrations.md §0 and §7.1: OAuth2 client-credentials grant with rotating short-lived tokens and Taikun client provisioning; a per-key 24h Idempotency-Key replay store (same key+same body returns the original response, same key+different body returns 409/422); HMAC-SHA256 signing on the two inbound webhooks (X-TEEP-Signature: sha256=<hex> + X-TEEP-Timestamp, ±5min skew rejection) with a per-environment shared secret; and a uniform RFC7807 application/problem+json error envelope across every endpoint.

The contract is already de-risked. teep-api.yaml (OpenAPI 3.1, 17 endpoints + 2 webhooks) lints clean under Redocly, is mock-served by Prism, property-tested by Schemathesis (~300 cases), and driven by a 4-scenario agent simulator on the AWS test VM. Auth (Bearer), Idempotency-Key, RFC7807 422s, and HMAC headers are all declared and exercised against the mock. So the gateway work is implementing behaviors the contract already pins down — not designing from a blank page. Taikun's half of the security story (the inbound webhook receiver: AWS API Gateway + Lambda in us-east-1 that verifies the TEEP HMAC signature, enforces the timestamp/nonce replay window, and returns 2xx/401) is in this workstream too, because the webhook signing scheme is a joint contract: TEEP signs, Taikun verifies, and the shared secret must be provisioned and rotated by both.

The honest scheduling reality: the deck's "Wk2-4 gateway build, Wk4 cutover" is aggressive. Bootstrap (S3 drops) can realistically land by week 2 and is the true unblocker. The full gateway — OAuth2 server config, idempotency store, webhook signing wired through TEEP's (non-existent-today) gateway platform — is more likely a 3-5 week build once TEEP picks the gateway technology, with cutover trailing into weeks 5-7. The biggest schedule risk is not Taikun's adapters; it is TEEP standing up gateway infrastructure from zero plus IT-security (Sahir) sign-off on cross-account S3 data sharing.

Genuinely uncertain / not to be fabricated: the gateway technology TEEP will choose (AWS API Gateway? Apigee? Azure APIM? a TotalEnergies corporate standard?); the OAuth2 token endpoint URL, issuer, and token TTL; the production gateway base URL (teep-api.yaml carries a placeholder gateway.teep.example.com); whether IT-security permits the S3 cross-account bootstrap at all (drives Path A vs Path B for the asset registry); the S3 bucket/account and the KMS/signing approach; and the per-environment HMAC shared-secret rotation cadence. Each is captured as an open_decision with a recommended default and an owner, rather than guessed.

**Key people**

| Name | Org | Role |
|---|---|---|
| Darko Jankovic | TEEP | Engineering — API gateway owner, security/governance; owns OAuth2 client provisioning, RFC7807 envelope, bootstrap S3 bucket |
| Sahir | TEEP | Cyber & Field Ops (IT/security) — conditional access, no-static-creds policy, S3 cross-account data-sharing sign-off |
| Michelle | TEEP | Owns TaskHub/FMP + Sensirion(Nubo) relationship — co-owns HMAC shared-secret provisioning for the two webhooks |
| Sebastian | TEEP | TEEP engineer building FMP/TaskHub API + connecting to Sensirion server — produces TaskHub/Sensirion bootstrap drops |
| Mike | TEEP | Owns Cygnet/SCADA — produces Cygnet bootstrap snapshots/series via existing intermediary |
| Steve Ridder | Taikun | Founder/CEO — pilot sponsor, signs MSA/DPA, nominates Taikun OAuth client |
| Taikun engineering | Taikun | Builds inbound webhook receiver (API Gateway+Lambda, us-east-1), S3 bootstrap poller, source adapters, OAuth token client, idempotency-key generator |
| IFS Merrick stack owner | IFS Merrick | ProCount/Carte owner — TBD at TEEP; produces ProCount/Carte bootstrap drops + later OData-behind-gateway |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `GW-1` | Gateway kickoff + decision session: tech choice, auth method, bootstrap mode | Joint · Darko (TEEP, chair) + Sahir (TEEP IT-sec) + Taikun eng + Steve Ridder | Kickoff | 1 | 🔴 | — |
| `GW-2` | IT-security sign-off on cross-account data sharing (S3 bootstrap + us-east-1 residency) | TEEP · Sahir (Cyber & Field Ops) + Darko | Kickoff | 3 | 🔴 | `GW-1` |
| `GW-3` | Provision S3 bootstrap bucket + signing/access for cross-account read | TEEP · Darko | Bootstrap | 2 | 🔴 | `GW-2` |
| `GW-4` | Bootstrap drop spec: freeze JSON shapes to teep-api.yaml + drop cadence + manifest | Taikun · Taikun engineering | Bootstrap | 2 | 🔴 | `GW-1` |
| `GW-5` | TEEP produces first real bootstrap drops for all 5 systems + catalogs | Joint · Sebastian (Sensirion+TaskHub), Mike (Cygnet), IFS Merrick owner (ProCount+Carte) — coordinated by Darko | Bootstrap | 5 | 🔴 | `GW-3`, `GW-4` |
| `GW-6` | Taikun S3 bootstrap poller + source adapters (one code path, shape-stable) | Taikun · Taikun engineering | Bootstrap | 6 |  | `GW-4`, `GW-5` |
| `GW-10` | Idempotency-Key 24h replay store on the gateway (write endpoints) | TEEP · Darko + Sebastian (TaskHub) | Build | 4 | 🔴 | `GW-7`, `GW-8` |
| `GW-11` | Taikun idempotency-key generation + safe-replay retry policy | Taikun · Taikun engineering | Build | 2 |  | `GW-10` |
| `GW-12` | HMAC-SHA256 webhook signing (TEEP side) + shared-secret provisioning | TEEP · Michelle + Sebastian (webhook emitters) + Darko (secret provisioning) | Build | 3 | 🔴 | `GW-7` |
| `GW-13` | Taikun inbound webhook receiver (API Gateway + Lambda, us-east-1) — HMAC verify + replay guard | Taikun · Taikun engineering | Build | 4 | 🔴 | `GW-4` |
| `GW-14` | RFC7807 problem+json error envelope across all endpoints | TEEP · Darko (envelope) + each system owner (per-endpoint error semantics) | Build | 3 | 🔴 | `GW-7` |
| `GW-7` | Stand up TEEP gateway platform shell (routing, TLS, base URL, /v1 versioning) | TEEP · Darko | Build | 5 | 🔴 | `GW-1` |
| `GW-8` | OAuth2 client-credentials: token endpoint + Taikun client provisioning + scopes | TEEP · Darko | Build | 3 | 🔴 | `GW-7` |
| `GW-9` | Taikun OAuth token client + secret rotation handling | Taikun · Taikun engineering | Build | 2 |  | `GW-8` |
| `GW-15` | Contract conformance run: re-point Prism/Schemathesis/simulator at the live gateway | Joint · Taikun engineering (drives) + Darko (gateway support) | Cutover | 3 | 🔴 | `GW-8`, `GW-10`, `GW-12`, `GW-13`, `GW-14` |
| `GW-16` | Cutover: flip Maxwell source from S3 bootstrap to live gateway (config-only) | Taikun · Taikun engineering | Cutover | 3 | 🔴 | `GW-6`, `GW-15` |
| `GW-17` | Gateway observability + failure-notification wiring (operate) | Joint · Taikun engineering (metrics/alerts) + Darko (gateway-side logs + contact) | Operate | 2 |  | `GW-16` |

**Deliverables:** Gateway & bootstrap decision record *(Joint)*; IT-security approval memo *(TEEP)*; S3 bootstrap bucket + cross-account access *(TEEP)*; BOOTSTRAP-DROP-SPEC.md + example drops *(Taikun)*; First real bootstrap dataset (5 systems + 5 catalogs) *(Joint)*; Taikun S3 bootstrap poller + source adapters + Path-A registry ingest *(Taikun)*; TEEP gateway platform shell *(TEEP)*; OAuth2 client-credentials flow + provisioned Taikun client *(TEEP)*; Taikun OAuth token client *(Taikun)*; Idempotency-Key 24h replay store *(TEEP)*; Taikun idempotency + retry/rate-limit policy *(Taikun)*; HMAC-SHA256 webhook signing + shared-secret provisioning *(TEEP)*; Taikun inbound webhook receiver (API Gateway + Lambda, us-east-1) *(Taikun)*; RFC7807 problem+json error envelope *(TEEP)*; Live-gateway conformance report *(Joint)*; Config-only cutover + rollback *(Taikun)*; Gateway observability + failure-notification *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Which gateway technology will TEEP use, given none exists today (AWS API Gateway, Apigee, Azure APIM, or a TotalEnergies corporate standard)? | TEEP | AWS API Gateway in TEEP's account (aligns with Taikun's us-east-1 residency, native OAuth2/Cognito + WAF + Lambda authorizer support) unless a TotalEnergies corporate API-management standard mandates otherwise. | Kickoff (GW-1) — gates the entire gateway build |
| Primary auth method: OAuth2 client-credentials or mTLS? | TEEP | OAuth2 client-credentials grant with rotating 1h tokens + emissions.read/emissions.write scopes (per 05-security-answers §C.1); mTLS supported as equivalent if TEEP prefers. | Kickoff (GW-1) |
| Bootstrap transport: signed S3 JSON drops, SFTP/managed transfer, or read-replica? | TEEP | Signed S3 JSON drops (Option A) — lowest TEEP infra lift, works with any auth, and Sierra's xlsx already proved the pattern for Sensirion. | Kickoff (GW-1) — gates bucket provisioning |
| Asset-registry path: Path A (Taikun ingests per-system catalogs from bootstrap) or Path B (TEEP builds + exposes GET /v1/assets/{id})? | TEEP (Sahir + Darko) | Path A — proven in production for R2Q, zero TEEP API build, decoupled from gateway delivery; fall back to Path B only if IT-security blocks source-data sharing. | Kickoff/Bootstrap (GW-2) — gates registry build |
| What is the production gateway base URL? (teep-api.yaml carries placeholder gateway.teep.example.com) | TEEP (Darko) | TBD — do not fabricate; Taikun reads it from config so it can be filled in at cutover. | Build (GW-7) — needed before live conformance |
| OAuth2 token endpoint URL, issuer, token TTL, and how the Taikun client_id/secret are delivered? | TEEP (Darko) | Default 1h token TTL; client secret delivered out-of-band (vault/sealed channel), never static long-lived; values recorded in the decision log, not hardcoded. | Build (GW-8) |
| S3 bucket name, TEEP AWS account id, and cross-account access model (bucket-policy principal grant vs pre-signed URLs) + KMS key? | TEEP (Darko + Sahir) | SSE-KMS bucket with a bucket-policy read grant to Taikun's us-east-1 account principal; pre-signed URLs if principal grants are disallowed. Account ids/KMS key TBD — not fabricated. | Bootstrap (GW-3) |
| HMAC webhook shared-secret value + rotation cadence, and whether TEEP/TotalEnergies has a preferred signature scheme over the proposed X-TEEP-Signature/X-TEEP-Timestamp. | Joint (Darko + Michelle + Taikun) | Adopt the proposed scheme (sha256 hex HMAC over raw body, ISO-8601 timestamp, ±5min skew); per-environment secret rotated quarterly or on personnel change; provisioned via vault. | Build (GW-12/GW-13) |
| Who is the IFS Merrick (ProCount/Carte) owner at TEEP responsible for producing those bootstrap drops? | TEEP | Confirm an owner in week 1; default to production-accounting/Michelle if no separate owner; collapse Carte into ProCount so only one IFS owner is needed. | Bootstrap (GW-5) |
| Failure-notification contact + channel for persistent API failures (>=5 in 5 min)? | TEEP (Darko) | Email (default per 05-security-answers §E.2); add PagerDuty/Opsgenie/Teams if TEEP provides an integration key/webhook URL. | Operate (GW-17) |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| TEEP has no API gateway today ('be creative in the beginning') — standing up gateway technology from zero plus a tech-selection decision is the single biggest schedule risk and can blow past the deck's Wk2-4 build window. | H | H | Front-load the gateway tech decision in GW-1; run the bootstrap track (GW-3..GW-6) fully in parallel so Taikun is unblocked regardless of gateway timeline; communicate a realistic 3-5 week gateway build + cutover trailing to weeks 5-7, not the aggressive deck dates. | TEEP |
| IT-security (Sahir) blocks or delays cross-account S3 data sharing, which would force asset-registry Path B (TEEP-built resolver endpoint) and slow the whole bootstrap. | M | H | Raise GW-2 in week 1 with the DPA/residency one-pager; have Path B (GET /v1/assets/{id}, already specced + Prism-tested) ready as the fallback; offer SFTP/managed-transfer as an alternate bootstrap transport if S3 cross-account is disallowed. | TEEP |
| Idempotency-Key store implemented incorrectly (e.g. keyed only by key, not key+body-hash, or no 24h TTL) leads to either duplicate TaskHub dispatch tasks on retries or wrongly-suppressed legitimate writes. | M | H | Pin behavior to teep-api.yaml's declared IdempotencyKey semantics; verify with the simulator's dispatch/monitor/timeout scenarios (replay same key+body -> identical response, no second task; key+different body -> 409/422) in GW-10/GW-15 before go-live. | TEEP |
| HMAC shared-secret mismatch or clock skew between TEEP and Taikun causes valid webhooks to be rejected (401) — silently dropping Sensirion events, defeating the whole MTTA-reduction goal. | M | H | Joint signed-webhook smoke test in GW-12/GW-13 with secret rotation exercised; NTP-synced timestamps; ±5min skew window; alert on signature-verification failures rather than silent drop; poll fallback (GET /v1/sensirion/events?since=) as backstop per spec. | Joint |
| Bootstrap JSON shapes drift from teep-api.yaml (a system owner emits a slightly different shape), breaking the 'cutover is config-only' guarantee and forcing adapter rewrites. | M | M | Freeze shapes in GW-4 with example files + schema_version in every manifest; Taikun validates each drop against schema_version on ingest (GW-6) and rejects/alerts on mismatch rather than masking; same adapter validated against both mock and bootstrap. | Taikun |
| Production gateway base URL, OAuth token endpoint/issuer/TTL, S3 account/KMS, and HMAC secret are all unknown today; building against placeholders (gateway.teep.example.com) risks late rework. | M | M | Keep all of these as configuration (never hardcoded); capture each as an open_decision with owner+needed-by; Taikun code reads base URL + endpoints from config so filling them in is a deploy-time change. | Joint |
| Read-replica bootstrap option (Option C) gets chosen for speed but Darko has stated direct DB access is not permitted long-term, creating a dead-end bridge and rework at cutover. | L | M | Default to Option A (S3 drops) in GW-1; if Option C is used, scope it explicitly as a temporary bridge with a hard cutover date and keep adapters reading the same JSON shape so the source swap stays config-only. | TEEP |
| Multiple TEEP owners (Sebastian, Mike, IFS Merrick TBD) must each produce conformant drops; an unowned IFS Merrick/ProCount/Carte owner stalls 2 of 5 systems. | M | M | Name the IFS Merrick owner as an open_decision needed by bootstrap week 1; Carte can ride ProCount (reducing to one IFS owner); Darko coordinates a single drop-spec walkthrough (GW-4) for all owners at once. | TEEP |

---

### REG · Cross-System Asset Registry & Identity Binding (Path A)

**Lead:** Taikun  ·  **Effort:** ~38.0 person-days  ·  **Tasks:** 16

**Objective.** Stand up one canonical TEEP-Barnett asset registry (asset_metadata.{assets,aliases,bindings}) on the proven R2Q schema, binding each of the 5 source systems' native IDs (ProCount well_id/API#, Cygnet asset_path, Sensirion device_id, Carte well_id, FMP pad_id) to a single canonical asset so a Sensirion event fans out to Cygnet/ProCount/Carte reads and a TaskHub dispatch — all keyed on one identity. Close the two known code gaps (connector-driven binding ingest; generalize the resolver off system='isite') and ship per-system input normalization + a number-aware alias merge so ingest never reproduces the perkins-14→12 collision class.

The asset-identity problem is the single hardest cross-system engineering issue in Maxwell: Sensirion knows devices, Cygnet knows assets, ProCount/Carte know wells, TaskHub knows pads, and none of them share one identifier. Everything downstream — fanning a Sensirion alert out to Cygnet pressure, ProCount codes, Carte injection, and a TaskHub dispatch — depends on resolving those five namespaces to one canonical asset. We take Path A (Taikun-side ingest) because the exact asset_metadata.* schema already runs in production for R2Q today (3,808 assets, ~8,500 bindings, 2,378 assets carrying multiple bindings simultaneously). This is connector wiring on proven rails, not a redesign or a schema change.\n\nThe load-bearing insight is that binding strategy is per-system, not universally fuzzy. ProCount is the canonical spine (production-accounting master with the cleanest well+lease structure) and binds by exact API# (Strategy A, deterministic) where the 14-digit API number is present, else fuzzy on well_name (Strategy C, banded). Cygnet binds by reference on asset_path (Strategy B) or fuzzy on display_name+aliases[]. Sensirion binds by reference via device->pad_id/well_ids/asset_path and must NEVER be fuzzy — device IDs like NUB-D-1234 have zero name/API overlap with wells, so a fuzzy attempt either fails or binds to the wrong well. Carte shares ProCount's store and is mostly a dedup, possibly needing no separate ingest. FMP is pad-level reference, expanded to wells via the hierarchy. Recommended build order: spine from ProCount first, then bind Cygnet, Sensirion, Carte, FMP onto it.\n\nTwo bounded, additive code gaps stand between us and TEEP-readiness, both confirmed in the live code. (1) Connector-driven binding ingest: today the Data-Catalog Import-Assets path writes assets keyed on a single external_id but does not populate asset_bindings/asset_aliases; we add a per-system binding step that emits BindingRecord {system, external_id, display_name, parent_ref?, api_number?, lat/lon?} and routes through the existing Resolver.bind (A/B/C banding logic already lives in asset_discovery_service.py). (2) Generalize the resolver off system='isite': AssetResolver._fetch_isite_well_ids is pinned to system='isite' at the binding lookup; we parameterize it to a per-tenant system list so a canonical asset resolves across all bound namespaces. Neither touches the schema. The 2026-05-29 dry-run already proved the matcher is sound on genuinely foreign names (75.5% auto-resolvable on 400 well names, 99.2% on leases, zero trailing-number collisions), and surfaced two real tuning items we fold in: per-system input normalization (strip the ' , MV'/' , AA' formation suffixes and '*' markers) and a number-aware alias-merge (the historical merged_alias rows carry perkins-14→12-class errors from a pre-existing non-number-aware bulk merge).\n\nWhat is genuinely uncertain and drives the open decisions: whether IT-security (Sahir/Darko) permits sharing per-system catalogs at all (gates Path A vs the Path B GET /v1/assets/{id} fallback); whether ProCount actually exposes the API number per well (the difference between deterministic Strategy-A binding and fuzzy Strategy-C across ProCount↔Cygnet↔Carte); whether Sensirion device metadata reliably carries well_ids or only pad_id (determines well-level vs pad-level Sensirion binding and is the same data Sebastian is wiring up in the SEN workstream); and the re-ingest cadence (nightly diff vs on-demand per connector sync). We sequence the registry to start in Bootstrap week 1-2 off the signed S3 catalog dumps so it is decoupled from the gateway, with config-only cutover to live reads when the gateway lands.

**Key people**

| Name | Org | Role |
|---|---|---|
| Darko Jankovic | TEEP | Engineering / API gateway / security & governance — owns the data-sharing decision (Path A vs Path B) and approves per-system catalog dumps |
| Sebastian | TEEP | TEEP engineer building FMP/TaskHub API and connecting to Sensirion — provides the FMP pad catalog and Sensirion device->pad/well mapping |
| Michelle | TEEP | Owns TaskHub/FMP + Sensirion(Nubo) relationship — source of FMP pad list and Sensirion device metadata |
| Mike | TEEP | Owns Cygnet/SCADA — provides the Cygnet asset catalog (asset_id, asset_path, aliases[]) and confirms whether asset_path is bindable |
| Sahir | TEEP | Cyber & Field Ops — IT-security sign-off on per-system data sharing (gates Path A vs Path B fallback) |
| ProCount/Carte owner (TBD) | IFS Merrick | Production accounting — provides the ProCount well list (well_id, pad, well_name, lease, operator, API#) that becomes the canonical spine; confirms API number availability |
| Taikun engineering | Taikun | Builds connector->binding ingest, generalizes resolver off system=isite, per-system normalization, runs ingest + review queue |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `REG-1` | Confirm Path A vs Path B with TEEP IT-security | Joint · Steve Ridder (Taikun) + Darko + Sahir (TEEP) | Kickoff | 1 | 🔴 | — |
| `REG-2` | Define per-system catalog dump contract (5 systems) | Taikun · Taikun engineering | Kickoff | 1.5 | 🔴 | `REG-1` |
| `REG-3` | TEEP produces ProCount well catalog dump (canonical spine) | IFS Merrick · ProCount/Carte owner (TBD) + Darko | Bootstrap | 2 | 🔴 | `REG-2` |
| `REG-4` | TEEP produces Cygnet, Sensirion, Carte, FMP catalog dumps | TEEP · Mike (Cygnet), Michelle/Sebastian (Sensirion+FMP), ProCount owner (Carte) | Bootstrap | 3 | 🔴 | `REG-2` |
| `REG-10` | Bind Sensirion onto the spine (reference ONLY, never fuzzy) | Joint · Taikun engineering + Michelle/Sebastian (mapping completeness) | Build | 2 | 🔴 | `REG-8`, `REG-4` |
| `REG-11` | Dedup Carte against ProCount + bind FMP pads | Taikun · Taikun engineering | Build | 2 |  | `REG-8`, `REG-4` |
| `REG-12` | End-to-end binding validation on the Jan-2026 168-alert set | Taikun · Taikun engineering | Build | 2 | 🔴 | `REG-6`, `REG-9`, `REG-10`, `REG-11` |
| `REG-13` | Human review-queue triage with TEEP SME | Joint · Taikun engineering + Devin (TEEP SME) | Build | 1.5 |  | `REG-12` |
| `REG-16` | Path B fallback — consume TEEP GET /v1/assets/{id} (conditional) | Taikun · Taikun engineering (consumer) + Darko (TEEP endpoint) | Build | 3 |  | `REG-1` |
| `REG-5` | Build connector-driven binding ingest (Gap 1) | Taikun · Taikun engineering | Build | 5 | 🔴 | `REG-2` |
| `REG-6` | Generalize AssetResolver off system='isite' (Gap 2) | Taikun · Taikun engineering | Build | 4 | 🔴 | — |
| `REG-7` | Per-system input normalization + number-aware alias merge | Taikun · Taikun engineering | Build | 3 |  | `REG-5` |
| `REG-8` | Build ProCount canonical spine | Taikun · Taikun engineering | Build | 2.5 | 🔴 | `REG-3`, `REG-5`, `REG-7` |
| `REG-9` | Bind Cygnet onto the spine (reference/asset_path) | Taikun · Taikun engineering (with Mike for asset_path semantics) | Build | 2 |  | `REG-8`, `REG-4` |
| `REG-14` | Cutover registry ingest from S3 dumps to live gateway reads | Taikun · Taikun engineering | Cutover | 1.5 |  | `REG-12`, `GW-GATEWAY-LIVE` |
| `REG-15` | Schedule recurring re-ingest (registry stays current) | Taikun · Taikun engineering | Operate | 2 |  | `REG-14` |

**Deliverables:** Registry-path decision memo (Path A vs B) *(Joint)*; Per-system catalog dump contract + 5 JSON-schemas *(Taikun)*; 5 bootstrap catalog dumps in S3 *(TEEP)*; Connector-driven binding ingest module (Gap 1 closed) *(Taikun)*; Generalized AssetResolver (Gap 2 closed) *(Taikun)*; Per-system normalization config + number-aware alias merge *(Taikun)*; Canonical TEEP-Barnett asset registry *(Taikun)*; End-to-end 168-alert binding validation report *(Taikun)*; Live-gateway ingest cutover + scheduled re-ingest *(Taikun)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Path A (Taikun ingests catalogs) vs Path B (TEEP builds GET /v1/assets/{id})? | TEEP | Path A — proven in R2Q production today; zero TEEP API-build effort; decoupled from gateway delivery; Path B kept as conditional fallback | Kickoff (gates REG-2 onward) |
| Which system is the canonical spine for TEEP? | Joint | ProCount — it is the production-accounting master with the cleanest well+lease structure (07 §8) | Kickoff / before REG-3 |
| Does ProCount expose the 14-digit API number per well? | IFS Merrick | Assume yes and bind ProCount<->Cygnet<->Carte deterministically by Strategy A; if no, fall back to fuzzy Strategy C with normalization + number-aware guard | Bootstrap (during REG-3 export) |
| Does Sensirion device metadata reliably carry well_ids, or only pad_id? | Sensirion/Nubo | Bind at whatever granularity is present — well-level if well_ids exist, else pad-level expanded via hierarchy; coordinate with the SEN workstream (same mapping) | Bootstrap (during REG-4 Sensirion export) |
| Is Cygnet asset_path a stable reference into the canonical pad/well structure (enables Strategy B), or name-only (forces Strategy C)? | TEEP | Confirm with Mike; default to Strategy B via asset_path, fall back to fuzzy on display_name + aliases[] if asset_path is not a reliable reference | Build (before REG-9) |
| Does Carte need a separate ingest, or is it fully deduped into ProCount? | IFS Merrick | Dedup into ProCount (shared store) — add a 'carte' binding row to the same canonical asset, no separate canonical creation; only ingest separately if well_ids diverge | Build (before REG-11) |
| Re-ingest cadence: nightly diff vs on-demand per connector sync? | TEEP | Nightly diff after gateway cutover (04 §5.1); on-demand re-sync available manually for fleet changes between nightly runs | Operate (before REG-15) |
| TEEP tenant_id value for the asset_metadata.* rows (R2Q uses 'r2q')? | Joint | Use a dedicated TEEP tenant id (e.g. 'teep_barnett') — do NOT reuse 'r2q'; exact string TBD with Taikun ops; not fabricated here | Build (before REG-8 spine ingest) |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| IT-security blocks per-system catalog data sharing, forcing Path B and shifting build effort onto TEEP (new master-list + endpoint) | M | H | Frame REG-1 as catalog-metadata-only (names/IDs/structure, NOT raw production or SCADA values); keep Path B fully specced (teep-api.yaml §5.2) and the consumer adapter (REG-16) ready so a Path-B pivot is a known, bounded path rather than a redesign | Joint |
| ProCount does NOT expose the 14-digit API number per well, so ProCount<->Cygnet<->Carte binding drops from deterministic Strategy A to fuzzy Strategy C, raising review-queue volume and collision risk | M | M | Confirm API-number availability early in REG-3; if absent, lean on the proven fuzzy matcher (75.5% auto-link in dry-run) + per-system normalization (REG-7) + number-aware guard, and budget extra review-queue time (REG-13) | IFS Merrick |
| Sensirion device metadata carries only pad_id (no well_ids), forcing pad-level binding and ambiguous fan-out for multi-well pads | M | M | Measure well_ids coverage in REG-4; where only pad_id exists, bind at pad and expand via hierarchy, and use Cygnet/ProCount pressure+code signals to disambiguate the well at triage time; record as open decision REG-OD3 jointly with the SEN workstream | Sensirion/Nubo |
| Wrong or missing Sensirion binding silently breaks every triage fan-out (Sensirion is 168/168 alert origin) | L | H | Hard rule: Sensirion binds by reference ONLY, never fuzzy; unbindable devices route to the review queue and are never silently dropped; REG-12 validates fan-out on all 168 historical alerts before go-live | Taikun |
| Generalizing AssetResolver off system='isite' regresses the live R2Q production triage path | L | H | Keep tenant default = 'isite'; add per-tenant system-list config rather than replacing the hardcode; run the R2Q regression suite (REG-6 exit criterion) before merge; deploy/verify on the test VM first | Taikun |
| Historical merged_alias rows already contain number-collision errors (perkins-14->12, bare '12'->BURNS SHALLOW 4) that could propagate into TEEP ingest | M | M | REG-7 reuses the live number-aware guard in the alias-merge step so new ingest cannot reproduce the error; the pre-existing R2Q merged_alias cleanup is a separate one-time pass and does not block TEEP (TEEP is a fresh tenant) | Taikun |
| Re-ingest after gateway cutover duplicates bindings or re-opens resolved reviews | L | M | Make re-ingest idempotent (upsert on (asset_id, system, external_id); GREATEST on alias confidence as the live code already does); never re-open a resolved review — only enqueue genuinely-new ambiguities; verify with REG-14 parity diff | Taikun |
| Catalog dumps slip (multiple TEEP owners + a TBD ProCount owner), delaying the spine and every downstream binding | M | M | Sequence ProCount spine dump (REG-3) first and treat it as blocking; develop ingest (REG-5/6/7) against R2Q data in parallel so Taikun work is not idle while dumps land; escalate the TBD ProCount owner at kickoff | TEEP |

---

### AGENT · Maxwell Close-the-Loop Agent Build

**Lead:** Taikun  ·  **Effort:** ~70 person-days  ·  **Tasks:** 19

**Objective.** Extend the existing read-only Maxwell advisor into a full close-the-loop triage agent that detects (Sensirion webhook), enriches in parallel from the 4 TEEP systems via Taikun adapters, classifies (rule-cascade-first with LLM fallback), maps the 6-class TriageClassification to Sierra's 3-value resolution_type, and acts — auto-closing ~70% of events from the office, posting idempotent TaskHub dispatch tasks for ~27%, escalating ~3% to MRO — then monitors to close-out with a 24h escalation timer. The whole lifecycle runs as a true JSON workflow through the engine + LLM gateway, writes an immutable event_audit row per call, and never silently drops an event.

Maxwell already exists on `main` and runs on the demo VM as a read-only advisor (`POST /advisor/triage/{alert_id}` in `actionengine/engine/api/emissions_api.py`): it builds an LLM context from `emissions.alerts` + daily notes + pad history + similar incidents + clean-day baselines via `_build_alert_context`, and emits a 6-value `TriageClassification` (real_leak / false_alarm / thief_hatch / equipment_issue / needs_inspection / process_emission) with a confidence score and a `recommended_action`. This workstream turns that recommender into an agent that acts. The scope is the 8-step lifecycle in 03-architecture.md §0: Detect, Investigate, Reason, Decide, Auto-close, Dispatch, Monitor, Close — plus the failure handling and audit machinery that make it safe to run autonomously against a customer's live systems.

The chosen approach is workflow-first and deliberately deterministic-first. Per CLAUDE.md, every new capability is composed as stage tools (FQTN, inheriting `ActionEngineToolBase`, wrapping existing helpers — never duplicating logic) inside a JSON template (`emissions_triage_close_loop`) that runs through `ModularWorkflowDispatcher` → `WorkflowEngine` and the LLM gateway, editable in the ReactFlow editor and observable in run history. The structural template already exists in the platform — `tank_overflow_protection_with_servicenow.json` and `ring_energy_ai_traffic_cop_v3.json` (which has the human-interlock-and-timeout handling we need for the dispatch path). Because the close-the-loop flow is stateful, effectful, and long-running (a dispatched event can sit open for up to 24h), it is NOT exempt from the durable substrate — unlike the interactive Ask Taikun query path, this workflow MUST run durably so the monitor/timeout survives restarts. Classification is rule-cascade-first (03-architecture.md §8): the 14 distinct `how_cleared` templates across the 168 alerts collapse into a handful of deterministic signatures (pressure-drop + LO-note = 76 events; full multi-system = 14; compressor-specific = 8; field-visit-fugitive = 75). The existing Maxwell LLM is invoked only when rule confidence < 0.70 AND TaskHub free-text exists, so cost stays at ~1 LLM call per ambiguous event.

What is genuinely uncertain: (1) the enrichment fan-out depends on data contracts that other workstreams are still defining — the Cygnet signal arrives through an existing intermediary (FMP ~30-min poll, ProCount's documented Cygnet integration, or a historian export) rather than a direct SCADA API, so the Cygnet adapter's input shape is unknown until that path is chosen with Mike + Darko; the TaskHub write/webhook API is net-new and Sebastian-built; and the canonical asset_id resolution is delivered by the asset-registry workstream. (2) The rule-cascade thresholds (the 0.94 / 0.88 / 0.65 confidence numbers, the pressure-drop magnitudes) are calibrated on only 22 days of data — they need re-tuning against the 6-12 month historical pull before we can stand behind the >=92% accuracy KPI. (3) The auto-close path sends Sierra's closeout email and writes final resolution fields with no human touch; the customer's risk tolerance for fully-autonomous closure is a governance decision, so we ship behind a per-environment "auto-close gate" (shadow → human-confirm → autonomous) rather than turning it on at go-live.

To de-risk the upstream dependencies, the agent is built against the signed-S3-JSON-drop bootstrap (shaped exactly like the future REST responses) and the Prism mock of teep-api.yaml, behind an adapter seam where the data source is config, not code — so cutover to live gateway endpoints is a config change, not a rewrite. The agent is exercised end-to-end by the existing 4-scenario simulator (auto-close / dispatch / monitor / timeout) on the AWS test VM before any live traffic.

**Key people**

| Name | Org | Role |
|---|---|---|
| Devin | TEEP | MRO engineer (triage) — primary user of the advisor queue; validates classification accuracy and escalation behavior |
| Sierra | TEEP | HSE reporting coordinator — owns the 22-column vocabulary and how_cleared templates the agent must populate; reviews auto-close field mapping |
| Michelle | TEEP | Owns TaskHub/FMP + Sensirion relationship; counterpart for dispatch payload shape and evidence-pack content |
| Sebastian | TEEP | Builds the FMP/TaskHub read+write API + task.updated webhook the agent writes to and monitors |
| Mike | TEEP | Owns Cygnet/SCADA; counterpart for the intermediary SCADA path (the enrichment fan-out consumes whatever pipe Mike exposes) |
| Darko Jankovic | TEEP | Engineering / API gateway / governance — defines auth, idempotency, webhook signing, RFC7807 envelope the agent's adapters must honor |
| Clovis | TEEP | Operations lead — pilot sponsor; sign-off on go-live and KPI acceptance |
| Steve Ridder | Taikun | Founder/CEO — Taikun delivery sponsor |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `AGENT-1` | Lock the agent contract: enrichment inputs, TriageResponse, Sierra field mapping | Taikun · Taikun eng (lead) + Sierra (vocab) + Devin (triage-logic review) | Kickoff | 3 | 🔴 | — |
| `AGENT-2` | Add classification_rationale JSONB + status lifecycle states to emissions.alerts | Taikun · Taikun eng | Bootstrap | 1 |  | `AGENT-1` |
| `AGENT-3` | Create emissions.event_audit immutable append-only table | Taikun · Taikun eng | Bootstrap | 1 | 🔴 | `AGENT-1` |
| `AGENT-4` | Build the 4 read-enrichment adapters as ActionEngineToolBase tools (against bootstrap) | Taikun · Taikun eng | Bootstrap | 6 | 🔴 | `AGENT-1`, `AGENT-3` |
| `AGENT-10` | Dispatch path: idempotent TaskHub create_task with evidence pack | Taikun · Taikun eng | Build | 4 | 🔴 | `AGENT-8` |
| `AGENT-11` | Escalate path: surface In Review in MRO advisor queue with full decision trace | Taikun · Taikun eng + Devin (queue UX review) | Build | 2 |  | `AGENT-8`, `AGENT-3` |
| `AGENT-12` | Monitor + 24h-timeout: durable wait on TaskHub webhook + Sensirion return-to-baseline | Taikun · Taikun eng | Build | 5 | 🔴 | `AGENT-10` |
| `AGENT-13` | Close-the-loop finalize: read LO findings, map to Sierra cols, PATCH TaskHub closed | Taikun · Taikun eng | Build | 4 | 🔴 | `AGENT-12`, `AGENT-9` |
| `AGENT-14` | Compose + register the emissions_triage_close_loop JSON workflow (durable, gateway-routed) | Taikun · Taikun eng | Build | 4 | 🔴 | `AGENT-9`, `AGENT-10`, `AGENT-11`, `AGENT-13` |
| `AGENT-15` | Calibrate rule-cascade thresholds + LLM accuracy against historical data | Taikun · Taikun eng + Devin/Sierra (label validation) | Build | 5 |  | `AGENT-7`, `AGENT-6` |
| `AGENT-5` | Build ai.parallel_fetch enrichment fan-out stage tool | Taikun · Taikun eng | Build | 3 | 🔴 | `AGENT-4` |
| `AGENT-6` | Implement rule-cascade pre-classifier from the 14 how_cleared templates | Taikun · Taikun eng + Devin (rule review) | Build | 4 | 🔴 | `AGENT-5`, `AGENT-1` |
| `AGENT-7` | Extend Maxwell context-injection LLM triage to consume live enrichment (LLM fallback) | Taikun · Taikun eng | Build | 4 | 🔴 | `AGENT-6` |
| `AGENT-8` | Implement the decide/branch + 6→3 resolution_type mapping stage | Taikun · Taikun eng | Build | 2 | 🔴 | `AGENT-7`, `AGENT-2` |
| `AGENT-9` | Auto-close path: write final Sierra fields + closeout email + auto-close gate | Taikun · Taikun eng + Sierra (template + closeout-email text sign-off) | Build | 3 |  | `AGENT-8` |
| `AGENT-16` | Drive the full agent through the 4-scenario simulator on the test VM | Taikun · Taikun eng | Cutover | 4 | 🔴 | `AGENT-14` |
| `AGENT-17` | Cut adapters over from bootstrap/mock to live TEEP gateway (config-only) | Joint · Taikun eng + Sebastian (TaskHub) + Mike/Darko (Cygnet path) + Sahir/Darko (auth) | Cutover | 4 | 🔴 | `AGENT-16` |
| `AGENT-18` | Shadow-mode pilot on live traffic, then graduate the auto-close gate | Joint · Taikun eng + Devin + Clovis (gate sign-off) | Operate | 8 |  | `AGENT-17`, `AGENT-9` |
| `AGENT-19` | Agent observability: triage metrics + per-system API health + failure alerting | Taikun · Taikun eng | Operate | 3 |  | `AGENT-14` |

**Deliverables:** agent-contract.md + JSON schemas *(Taikun)*; emissions DB migrations (classification_rationale + lifecycle status + event_audit) *(Taikun)*; teep.* read-enrichment adapters *(Taikun)*; ai.parallel_fetch enrichment fan-out *(Taikun)*; Rule-cascade pre-classifier + LLM-fallback triage *(Taikun)*; Three act-path tools (auto_close / dispatch / escalate) + monitor + finalize *(Taikun)*; emissions_triage_close_loop JSON workflow *(Taikun)*; Calibration report + tuned thresholds *(Taikun)*; 4-scenario simulator evidence + live cutover report *(Taikun)*; Shadow-mode divergence report + gate-graduation record *(Joint)*; Agent observability (metrics, dashboards, failure alerting) *(Taikun)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| What is the go-live posture for autonomous auto-close — shadow only, human-confirm, or fully autonomous for Process Emissions? | TEEP | Default to shadow at go-live, graduate to human-confirm after Devin reviews ~1-2 weeks of divergences, then autonomous for Process Emissions only once divergence is within tolerance and Clovis signs off. Dispatch/escalate paths can go autonomous earlier (lower blast radius). | Before AGENT-18 (Operate phase / live traffic) |
| Who provides the final controlled vocabularies and standardized how_cleared templates, and are they fixed for the pilot? | TEEP | Sierra provides the exact 4 vocabularies (resolution_type, equipment, equipment_component, epa_identifier) and the per-pattern how_cleared template text; treat them as configuration (not hardcoded) so mid-pilot changes are a config edit. Needed to freeze AGENT-1. | AGENT-1 (Kickoff) |
| Which existing intermediary delivers Cygnet/SCADA signals to the agent — FMP ~30-min poll, ProCount's documented Cygnet integration, or a historian/read-replica export? | Joint | Default to sourcing through FMP/TaskHub's existing ~30-min device poll if it carries tubing/line/casing pressure + sales rate; fall back to ProCount's Cygnet integration for the rest. Decide with Mike + Darko; the agent adapts at cutover (AGENT-17), not in the build. | Before AGENT-17 (Cutover) |
| Is the TaskHub write API (POST/PATCH + task.updated webhook) confirmed in Phase-1 scope and on a timeline that meets week-4 cutover, or do we ship the email-to-MRO fallback first? | TEEP | Confirm TaskHub write in Phase-1 (required for close-the-loop on the 43.5% field-cleared events). If Sebastian's API isn't live by week 4, config-toggle the email-fallback so dispatch still works while office-cleared events benefit Day 1. Owner: Darko/Michelle/Sebastian (Q3). | Week 4 (Cutover); decision needed by end of Bootstrap |
| Where do agent API-failure alerts (>=5 failures/5min per system) land — email, Teams, or both — and who is the designated TEEP contact? | TEEP | Default to email to a Darko-designated distro plus the Triage Live dashboard banner; add Teams in Phase 2. Confirm the contact (Q7). | AGENT-19 (Operate) |
| When will the 6-12 month historical event pull be delivered, and will it block the accuracy KPI sign-off? | TEEP | Sierra/Clovis deliver the historical export (Q5) before AGENT-15. If it slips, recalibration slips and the >=92% accuracy KPI stays provisional (validated in shadow mode instead) — communicate this honestly rather than committing to the KPI on 22 days of data. | Before AGENT-15 (Build); ideally during Bootstrap |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Upstream APIs (gateway, Sensirion API, Sebastian's TaskHub write API, Bedrock, Cygnet intermediary path) slip, so the agent can be built but not cut over live — pushing go-live well beyond the deck's 8 weeks. | H | H | Build entirely against the config-driven bootstrap seam (signed-S3 JSON drops + Prism mock of teep-api.yaml) so all agent code is done and simulator-verified independent of upstream readiness; cutover (AGENT-17) is config-only. Sequence dispatch (TaskHub) email-fallback so office-cleared 56.5% benefit even without TaskHub write. Surface the realistic critical path to Clovis early. | Taikun |
| Fully-autonomous auto-close (write final fields + send Sierra's closeout email, no human touch) closes events incorrectly, eroding customer trust or mis-stating a regulatory record. | M | H | Ship behind a 3-state auto-close gate defaulting to shadow; graduate shadow→human-confirm→autonomous only after Devin reviews shadow divergences and Clovis signs off (AGENT-9, AGENT-18). Require confidence>=0.85 for auto-close. Sierra can override any auto-populated field (FR-31), audited. | Joint |
| Rule-cascade thresholds and the >=92% accuracy KPI are calibrated on only 22 days of data; real distribution/seasonality differs once live. | M | M | Keep all thresholds in config, never hardcoded; recalibrate against the 6-12 month historical pull (AGENT-15) before standing behind the accuracy KPI; run shadow mode on live traffic to measure real accuracy before autonomous closure. Flag to Clovis that the KPI is provisional until Q5 historical data lands. | Taikun |
| Free-text TaskHub LO notes are too noisy for the LLM fallback to classify reliably, dragging more events into Undetected/MRO than the ~3% target. | M | M | Rule cascade handles the dominant deterministic patterns (76+14+8 events) without the LLM; LLM only fires on <0.70 + free-text; low confidence safely routes to Undetected→MRO (never silently dropped, NFR-5). Tune the prompt against the historical free-text corpus in AGENT-15. | Taikun |
| Double-dispatch or double-close from replayed webhooks/retries (TaskHub task created twice, or task closed after the LO already closed it). | M | M | Client-supplied Idempotency-Key on every write with a 24h replay store (gateway-provided); finalize only PATCHes closed if the LO hasn't already (03-arch §3.2 alt branch); webhook receivers verify HMAC + +/-5min skew + replay protection; all writes audited so duplicates are detectable. | Taikun |
| The Cygnet-via-existing-intermediary path (FMP poll / ProCount integration / historian) delivers SCADA signals at coarser cadence or shape than the direct API the adapters were stubbed against, weakening the pressure-drop evidence (95/168 events). | M | M | Keep the Cygnet adapter behind the same normalizing seam; defer its final input shape to the chosen intermediary (CYG-*/Mike+Darko) and adapt at cutover (AGENT-17, config + thin adapter layer); the rule cascade already degrades gracefully when a single system's evidence is missing (03-arch §6). | Joint |
| The 24h-monitor durable timer fails to survive an agent restart/redeploy, leaving dispatched events stuck open with no escalation. | L | H | Run the monitor/timeout on the durable substrate (DBOS) — explicitly NOT exempt from durable execution; resume in-flight events from status='enriching' (03-arch §6); test forced-restart-mid-monitor in AGENT-16; supervisord auto-restart on the VM. | Taikun |

---

### REPORT · Reporting & UI — Sierra HSE/EPA Export + 4 Screens

**Lead:** Taikun  ·  **Effort:** ~25.5 person-days  ·  **Tasks:** 13

**Objective.** Make Sierra's monthly HSE/EPA report a true one-click drop-in replacement for her hand-keyed Excel — her exact 22 verbose column headers, her controlled vocabularies, Excel AND PDF export, live updates as Maxwell closes events, full filter set, and a Sierra-override-with-audit path — and finalize the four operator/reviewer screens (Triage Live, Event Detail, Maxwell decision trace, Integration Health) on the existing emissions.html, keeping the frontend thin (Tabler + vanilla JS) and every column sourced from emissions.alerts, never hardcoded.

Track B (Reporting) and the operator UI are the most de-risked part of Maxwell: the reporting tab already exists on `main` and runs on the demo VM (emissions.html, 1481 lines, 2 tabs: Overview + Emissions Advisor), the emissions.* schema already holds all 22 Sierra columns plus 168 real Jan-2026 alerts, and sierra-xlsx-analysis.md has already proven every header maps 1:1 to an existing emissions.alerts field — no schema change needed. What is genuinely missing is the thing that makes the report a drop-in replacement: there is no Sierra-format export endpoint in emissions_api.py today (it has alerts/summary/heatmap/root-causes/routes/timeline/daily-notes/advisor-*, but no `export`), and the current UI "Export" button is a generic advisor dump, not Sierra's verbose-header xlsx. So the core of this workstream is a server-side export tool that renders emissions.alerts into Sierra's exact column order with her exact verbose header strings (e.g. "Emissions Rate per Email Notification (kg/h)", "Was the Alert Cleared In Office or In Field? ", trailing spaces and all) and her controlled vocabularies — Excel via openpyxl (already a transitive dep used in forge/reserve_economics) and PDF — driven entirely by config (a header-map + vocab table), so a mid-pilot format change is a config edit, not a code change (directly mitigates PRD risk "Sierra's report format changes mid-pilot").

The hard, non-obvious work is data hygiene, not rendering. The real xlsx already contains dirty controlled-vocab values — "Process Emissions " (trailing space) appears alongside "Process Emissions", "Resolution Type:" has a stray " " blank, Status has a blank row. If we export raw emissions.alerts we will faithfully reproduce that mess and Sierra will reject it. The export config therefore needs a canonicalization layer (trim/normalize to the controlled set) that Maxwell's writers (Track A) also use when populating fields, so the data is clean at write-time and the export is clean by construction. That is the single biggest coordination point with the ACT/triage workstream.

The override-with-audit path is the other substantive piece. FR-31 requires Sierra to be able to correct any auto-populated field in the UI, with the change written append-only to emissions.event_audit (FR-32/NFR-6). That depends on two cross-workstream things we do not own: who the editing user is (Entra ID/SSO group→role claim — SSO workstream) and the event_audit table's append-only guarantee (shared with ACT). We build the override UI + PATCH endpoint thin, but it cannot be truly attributable until SSO lands; until then we record the override under a pilot identity and flag it. The four screens are mostly finalize-not-build: Triage Live and the Maxwell-advisor command center already exist (mockup 02/the live advisor tab), Integration Health (mockup 04) and the Maxwell decision-trace/Event Detail (mockup 03) are designed but not yet wired to live data. Integration Health in particular cannot show real success/p95/last-error numbers until the gateway + per-system adapters emit to emissions.event_audit, so it ships against bootstrap/audit data first and gets real numbers at gateway cutover.

What is genuinely uncertain: (1) Sierra's *exact* current column list — the xlsx we analyzed has two near-duplicate alert sheets ("Sensirion Alert Data" 22 cols with "Resolution Personnel" + "Problem Identified ", and " Sensirion Data" with "MRO Resolution Personnel: " + "Problem Identified via Email reply: "); we must confirm which one is her live template and whether headers have changed since Jan. (2) Whether her HSE/EPA monthly file is one combined workbook (5 sheets: alerts + daily-notes + linked-notes + pad-baselines + pads-without-alerts) or just the alerts sheet — the export scope differs materially. (3) The EPA-identifier / equipment / equipment_component vocabularies as they stand contain the catch-all literal "Process Emissions" used as an equipment value, which is semantically odd and may need a Sierra ruling. These are open_decisions owned by Sierra, gated on the Q4 column-list confirmation.

**Key people**

| Name | Org | Role |
|---|---|---|
| Sierra | TEEP | HSE reporting coordinator — owns the monthly HSE/EPA Excel; authoritative source for the exact 22 column headers, controlled vocabularies, and the override/audit workflow |
| Clovis | TEEP | Operations lead — consumer of the live KPI/reporting dashboard; sign-off on Phase-1 reporting acceptance |
| Devin Rushing | TEEP | MRO engineer (triage) — primary user of Triage Live + Event Detail + Maxwell decision-trace screens; one of the 5 MRO resolution personnel |
| Darko Jankovic | TEEP | Engineering / API gateway / governance — owns the override-audit immutability requirement, Integration Health data feed, and where API-failure notifications land |
| Sahir | TEEP | Cyber & Field Ops — Entra ID/SSO; the override-audit identity (which TEEP user made the change) depends on SSO group/role claims |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `REPORT-1` | Confirm Sierra's exact live HSE/EPA column list, sheet scope, and vocabularies | Joint · Sierra (TEEP, authoritative) + Taikun eng (capture into config) | Kickoff | 1.5 | 🔴 | — |
| `REPORT-2` | Build config-driven Sierra header-map + vocabulary canonicalization layer | Taikun · Taikun eng | Bootstrap | 2 |  | `REPORT-1` |
| `REPORT-10` | Build Event Detail + Maxwell decision-trace screen | Taikun · Taikun eng (frontend) + Devin (TEEP) review | Build | 3 |  | `REPORT-8` |
| `REPORT-11` | Build Integration Health screen | Taikun · Taikun eng (frontend) + Darko (TEEP) for notification target + Mike for Cygnet adapter status semantics | Build | 2.5 |  | — |
| `REPORT-13` | Optional combined-workbook export (daily notes, linked notes, pad baselines, pads-without-alerts) | Taikun · Taikun eng | Build | 2 |  | `REPORT-1`, `REPORT-3` |
| `REPORT-3` | Sierra-format Excel export endpoint + tool (emissions.alerts.export_sierra_format) | Taikun · Taikun eng | Build | 3 |  | `REPORT-2` |
| `REPORT-4` | PDF export of the monthly HSE/EPA report | Taikun · Taikun eng | Build | 2 |  | `REPORT-3` |
| `REPORT-5` | Wire the Reporting Monthly Report screen into emissions.html (replace generic Export) | Taikun · Taikun eng (frontend) | Build | 1.5 |  | `REPORT-3`, `REPORT-4`, `REPORT-6` |
| `REPORT-6` | Reporting filter bar (date range, resolution_type, route, equipment, equipment_component, cleared_location) | Taikun · Taikun eng (frontend) | Build | 1.5 |  | `REPORT-2` |
| `REPORT-7` | Live update of reporting view as Maxwell closes events | Taikun · Taikun eng (frontend) | Build | 1 |  | `REPORT-6` |
| `REPORT-8` | Sierra override-any-field UI + audited PATCH endpoint | Taikun · Taikun eng (with Sahir for SSO claim mapping; Darko for audit immutability sign-off) | Build | 2.5 |  | `REPORT-1` |
| `REPORT-9` | Finalize Triage Live screen (MRO command center) | Taikun · Taikun eng (frontend) + Devin (TEEP) UX review | Build | 1.5 |  | — |
| `REPORT-12` | Sierra acceptance: side-by-side export validation against her real file | Joint · Sierra (TEEP, acceptance) + Taikun eng | Operate | 1.5 | 🔴 | `REPORT-5`, `REPORT-7`, `REPORT-2` |

**Deliverables:** Sierra export spec (config) *(Taikun)*; emissions.report.export_sierra tool + /api/emissions/export endpoint *(Taikun)*; Vocabulary canonicalization module *(Taikun)*; Reporting tab upgrade in emissions.html *(Taikun)*; Sierra override-with-audit path *(Taikun)*; Triage Live screen (finalized) *(Taikun)*; Event Detail + Maxwell decision-trace screen *(Taikun)*; Integration Health screen *(Taikun)*; Sierra acceptance artifact *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Which alert sheet is Sierra's authoritative live template — 'Sensirion Alert Data' (with 'Resolution Personnel' + 'Problem Identified ') or ' Sensirion Data' (with 'MRO Resolution Personnel: ' + 'Problem Identified via Email reply: '), and have any headers changed since the Jan-2026 file? | TEEP | Use the 'Sensirion Alert Data' 22-column set from sierra-xlsx-analysis.md as the canonical export until Sierra rules otherwise. | Kickoff / start of REPORT-1 — blocks REPORT-2/3 |
| Is the monthly HSE/EPA deliverable a single combined workbook (5 sheets: alerts + daily-notes + linked-notes + pad-baselines + pads-without-alerts) or just the alerts sheet? | TEEP | Alerts sheet only for Phase-1 acceptance; multi-sheet (REPORT-13) only if Sierra confirms she files the combined workbook. | REPORT-1 — determines whether REPORT-13 is in scope |
| How should the dirty/odd controlled-vocab values be canonicalized — collapse 'Process Emissions ' trailing-space dupes to 'Process Emissions'; what default for the blank Resolution Type; and is literal 'Process Emissions' a legitimate value for the Equipment / Equipment Component / EPA identifier columns or a placeholder to be replaced? | TEEP | Trim+collapse all trailing-space dupes; keep 'Process Emissions' as the legitimate catch-all equipment value (matches Sierra's actual usage in 127/168 rows); default a blank Resolution Type to 'Undetected' pending her ruling. | REPORT-1 — feeds REPORT-2 canonicalizer |
| Where should API-failure / health notifications land (FR-34: >=5 failures in 5 min) — email, Teams, or both — and to which address(es)? | TEEP | Email to Darko's TEEP address as the designated contact for Phase 1 (the actual address is unknown — do not fabricate; confirm with Darko). | REPORT-11 build — before Integration Health ships its notification line |
| Until Entra ID/SSO is wired, what identity should the override audit record for changes made in the UI, and does Darko accept that interim posture as Phase-1-sufficient? | TEEP | Record overrides under a single configured 'pilot-operator' identity and flag them as unattributed; replace with real SSO subject claim when SSO-* completes. | REPORT-8 build — before the override path is enabled in production |
| Should the live reporting view use polling (Phase-1 default) or is a push/websocket update expected by Clovis for the ops dashboard? | TEEP | 30-60s polling for Phase 1 (volume is ~7.6 events/day — polling is more than adequate); revisit only if a real-time ops wallboard is requested. | REPORT-7 build |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Sierra's live column headers / sheet scope differ from the Jan xlsx we analyzed (two near-duplicate alert sheets exist; headers may have drifted), so the 'drop-in' export is rejected at acceptance. | M | H | Front-load REPORT-1 against a CURRENT copy of her file, not the Jan archive; make headers/vocab pure config (REPORT-2) so any delta found at acceptance is a config edit, not a code change; treat REPORT-1 and REPORT-12 as blocking gates. | Joint |
| Stored emissions.alerts values carry the dirty controlled-vocab found in the real data ('Process Emissions ' trailing space, blank Resolution Type, blank Status), so a naive export reproduces a messy file Sierra won't file. | H | M | Build the canonicalization layer (REPORT-2) and have Track A's writers import the SAME module so data is clean at write-time; unit-test the canonicalizer against the exact dirty values in the 168-alert sample. | Taikun |
| Override audit is not regulator-grade because 'which TEEP user made the change' depends on Entra ID/SSO claims that haven't landed yet (SSO workstream slip). | M | M | Ship the override path now under a configured pilot identity, clearly flag overrides as unattributed until SSO is wired; sequence REPORT-8/10 so the identity source plugs in when SSO-* completes; Darko signs off on the interim audit posture. | Taikun |
| Integration Health shows empty/fake numbers because the gateway + per-system adapters aren't emitting to emissions.event_audit until late in the pilot. | M | L | Ship REPORT-11 against bootstrap/audit data and the agent-simulator runs first; document a cutover step to swap to live-gateway numbers; label the data source on-screen so it's never mistaken for live production health. | Taikun |
| Excel/PDF rendering of ~230 events/month with full evidence is slow or memory-heavy if implemented naively, hurting the 'one-click' feel. | L | L | Stream/iterate rows with openpyxl write-only mode; the monthly volume (~230 rows) is tiny; reuse the proven PDF approach from reserve_economics rather than a new heavyweight dependency. | Taikun |
| Reporting screens go stale or diverge from the live triage data model because the four screens are built before ACT finishes populating classification_rationale / event_audit. | M | M | Build screens against the agreed JSONB/audit contracts (03-architecture §5.1/§5.2) with the 168-alert sample as fixture data; coordinate the event_audit table contract jointly with ACT before REPORT-8/10/11. | Joint |

---

### DATA · Data Baseline, KPIs & Pilot Success Criteria

**Lead:** Taikun  ·  **Effort:** ~49 person-days  ·  **Tasks:** 13

**Objective.** Move the pilot off the 22-day, 168-alert January sample by securing 6-12 months of historical TEEP emissions data, then build/confirm end-to-end KPI instrumentation (MTTA, MTTR, auto-close rate, classification accuracy on a human-labeled validation set, dispatch-to-close, per-system API error rate, and methane-avoided) so that the published Phase-1 targets are grounded in real seasonality. Define the labeled validation set and the accuracy measurement method, lock the 60-day pilot success criteria from PRD §11, and run the Phase-1->Phase-2 go/no-go review against measured numbers rather than deck aspirations.

This workstream is the measurement backbone of Project Maxwell. Every headline number in the deck and PRD — MTTA 5.2h to under 10 min, resolution 16.5h to under 2h, 0% to 40% auto-close, classification accuracy >=92%, and roughly 70 t CH4 avoided over the sample (about 33,000 t CO2e/yr) — is extrapolated from a single 22-day January window of 168 alerts. PRD §7 explicitly flags this as a risk ("Sample is 22 days only... assumes the Jan distribution is representative") and open item Q5 already asks Sierra/Clovis for 6-12 months of history. The first and most blocking job here is to actually land that historical pull, because seasonality (winter freeze-offs vs. summer thermal venting, holiday staffing) materially changes the resolution-type mix (today 70.2% Process Emissions / 27.4% Unexpected / 2.4% Undetected) and therefore the achievable auto-close ceiling.

The second job is instrumentation. From reading the live code, the reporting side is partially built: GET /api/emissions/summary already returns avg_response_hours, office/field split, and the resolution-type mix from emissions.alerts. But the agent-performance KPIs that the pilot is judged on do NOT exist yet. emissions.alerts has no acknowledged-timestamp distinct from email_received, no auto-close flag, no classification confidence, and no ground-truth label column; the emissions.event_audit table referenced throughout PRD §5.4-§5.6 is not in schema/118_emissions.sql at all. So MTTA-from-kg/hr-threshold, MTTR (dispatch-to-close), auto-close rate, per-system API error rate, and the methane-avoided estimate (today a static deck calculation, not a query) all need to be built as a proper metrics layer over the alerts + event_audit + a new labels table. We will build these as queryable KPIs feeding the existing emissions.html reporting tab and a new pilot scorecard, not as a one-off spreadsheet.

The third job is scientific credibility of the >=92% accuracy claim. Accuracy is meaningless without a frozen, human-labeled validation set and a documented method. We will carve a stratified labeled set out of the historical pull — preserving the real class imbalance and over-sampling the rare Undetected class (only 4 of 168) and the field-cleared cases — have Devin and Sierra adjudicate the ground truth, and measure the classifier with a confusion matrix, per-class precision/recall, and the abstention behavior (confidence <0.65 -> Undetected -> MRO review per FR-12). We hold the validation set out of any prompt/rule tuning to avoid leakage.

What is genuinely uncertain: (1) whether TEEP can even produce a true kg/hr-threshold-crossing timestamp historically — Sierra's xlsx records email_received, not the sensor threshold event, so the real baseline MTTA may have to be reconstructed from Sensirion device data via the Sensirion-API workstream, and until then the "5.2h" baseline is an email-delay proxy; (2) how much of the 6-12 month history is retrievable per system (Sensirion likely full, but ProCount/Carte and Cygnet history depend on the SCADA-via-existing path and an owner who is still TBD); and (3) whether the January class mix holds across seasons, which is the entire reason for the pull. We reflect these honestly as open decisions rather than asserting the deck's aggressive numbers.

**Key people**

| Name | Org | Role |
|---|---|---|
| Sierra | TEEP | HSE reporting coordinator — owns the Sensirion alert xlsx and the monthly HSE/EPA report; provider of the 6-12 month historical pull and the human ground-truth labels |
| Clovis | TEEP | Operations lead — co-owner of the historical-data request, approves the pilot success criteria and the Phase-1->Phase-2 go/no-go |
| Devin | TEEP | MRO engineer (triage) — validates classification labels, reviews accuracy disputes, is one of the two named sign-off personas in the PRD success criteria |
| Michelle | TEEP | Owns TaskHub/FMP + the Sensirion(Nubo) relationship — gates whether kg/hr-threshold timestamps and device cadence needed for true MTTA are available historically |
| Owner TBD (production accounting) | IFS Merrick | ProCount/Carte owner — supplies historical down/up codes + operator comments that feed the labeled validation set; contact still unidentified |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `DATA-1` | Issue and scope the 6-12 month historical data request to TEEP | Joint · Steve Ridder (Taikun) drafts; Sierra + Clovis (TEEP) approve & fulfill | Kickoff | 2 | 🔴 | — |
| `DATA-2` | Receive & ingest the historical pull into emissions.* (extend ingestion) | Taikun · Taikun engineering | Bootstrap | 4 | 🔴 | `DATA-1` |
| `DATA-3` | Recompute KPI baselines on the full history & seasonality analysis | Taikun · Taikun engineering (data) with Clovis (TEEP) reviewing operational interpretation | Bootstrap | 4 |  | `DATA-2` |
| `DATA-4` | Add emissions.event_audit + KPI instrumentation columns to the schema | Taikun · Taikun engineering | Bootstrap | 3 | 🔴 | — |
| `DATA-10` | Measure classification accuracy vs. the labeled set | Taikun · Taikun engineering | Build | 4 | 🔴 | `DATA-8` |
| `DATA-11` | Lock the 60-day pilot success criteria & measurement plan | Joint · Steve Ridder (Taikun) drafts; Clovis, Darko, Sierra, Devin (TEEP) sign | Build | 2 | 🔴 | `DATA-3`, `DATA-5`, `DATA-9` |
| `DATA-5` | Build the KPI metrics layer & pilot scorecard endpoint | Taikun · Taikun engineering | Build | 5 |  | `DATA-4` |
| `DATA-6` | Define & build the methane-avoided estimator (instrumented, not a static deck number) | Taikun · Taikun engineering with Brent (TEEP HSE field lead) reviewing the emissions method | Build | 3 |  | `DATA-3`, `DATA-5` |
| `DATA-7` | Define the labeled validation set & freeze it | Taikun · Taikun engineering (sampling design) with Devin (TEEP) on labeling scope | Build | 3 | 🔴 | `DATA-2` |
| `DATA-8` | Human ground-truth labeling pass (Devin + Sierra adjudication) | TEEP · Devin + Sierra (TEEP), facilitated by Taikun engineering | Build | 5 | 🔴 | `DATA-7` |
| `DATA-9` | Reconstruct the true MTTA baseline (kg/hr threshold, not email proxy) | Joint · Taikun engineering + Michelle/Sebastian (TEEP) + Sensirion/Nubo | Build | 3 |  | `DATA-2` |
| `DATA-12` | Operate the live pilot scorecard during the 60-day window | Joint · Taikun engineering (instrumentation) + Clovis/Devin/Sierra (TEEP weekly review) | Operate | 8 |  | `DATA-5`, `DATA-11` |
| `DATA-13` | Run the Phase-1 -> Phase-2 go/no-go review | Joint · Steve Ridder (Taikun) authors; Clovis + Darko (TEEP) decide | Operate | 3 |  | `DATA-12`, `DATA-10`, `DATA-6` |

**Deliverables:** Signed historical-data request (6-12 months) *(Joint)*; Historical-data ingestion loader + validation report *(Taikun)*; Full-history KPI baseline & seasonality report *(Taikun)*; KPI instrumentation migration (event_audit + alerts columns) *(Taikun)*; KPI metrics layer + live pilot scorecard *(Taikun)*; Methane-avoided estimator + assumptions memo *(Taikun)*; Frozen labeled validation set + emissions.alert_labels *(Taikun)*; Human labeling + inter-annotator-agreement memo *(TEEP)*; Classification accuracy report *(Taikun)*; Signed 60-day pilot success-criteria & measurement plan *(Joint)*; Phase-1->Phase-2 go/no-go decision package *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| What is the actual retrievable historical window per system (Sensirion alerts, LO/daily notes, ProCount codes, Cygnet series), and what is the committed delivery date? | TEEP | Target 12 months, accept minimum 6; deliver Sensirion + notes first (highest value, likely full retention), ProCount/Cygnet history as a fast-follow. Bound KPI confidence to whatever window lands. | End of Kickoff (gates DATA-2/3 and the whole baseline track). |
| Can Sensirion (Nubo) provide a historical kg/hr-threshold-crossing timestamp distinct from the email-received time, so the MTTA baseline reflects the real detection delay rather than the email proxy? | Sensirion/Nubo | If not available historically, keep the email-delay proxy as the published baseline and measure post-Maxwell MTTA as threshold-to-classified, with the clock-change caveat written into the success criteria. | Before DATA-11 (success-criteria lock). |
| What kg/hr trigger threshold X (FR-3) is used for KPI accounting — the PRD default of 1 kg/hr, or a TEEP-set value? | TEEP | Use PRD default 1 kg/hr for baseline computation; record the chosen value as a config parameter in the methane-avoided estimator and scorecard. | Before DATA-3 baseline recompute. |
| What target size and class-stratification does TEEP accept for the labeled validation set, and how much Devin/Sierra time can be committed to labeling? | Joint | ~300-400 events stratified across months with rare-class over-sampling; budget ~5 person-days of Devin+Sierra adjudication with a 30-event overlap for agreement measurement. | Before DATA-7 (validation-set freeze). |
| Is the 92% Phase-1 accuracy target measured overall, or must each class (incl. the rare Undetected) independently clear a bar? | Joint | Overall >=92% AND no class below an agreed floor (e.g. 80% recall on Undetected) measured relative to the inter-annotator ceiling; abstention-to-MRO counts as correct, not as a miss. | Before DATA-11 (success-criteria lock). |
| What GWP convention and duration model underpin the published methane-avoided figure for HSE/EPA defensibility? | TEEP | GWP 28 (IPCC AR5) per the existing deck; duration = MTTA + resolution time, rate held at sensor-measured value; both parameterized as config and reviewed by Brent. | Before DATA-6 (estimator build). |
| Does the 60-day pilot clock start at API-availability cutover or at a later 'steady-state' date, given the new SSO/Bedrock/Sensirion-API dependencies likely push past the deck's aggressive 8 weeks? | Joint | Start the 60-day measured window only after Maxwell is live end-to-end on TEEP APIs (post-cutover) with the scorecard deployed; the pre-cutover bootstrap period is dev/validation, not pilot measurement. | Before DATA-12 (operate phase begins). |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| TEEP cannot deliver a full 6-12 month history (Sensirion retention shorter than assumed, or per-system exports incomplete for ProCount/Carte/Cygnet), so KPI targets stay extrapolated from the thin 22-day sample. | M | H | Front-load DATA-1 as a Kickoff blocker; accept the longest window available and explicitly bound KPI confidence to it in DATA-3; if only Sensirion history is available, firm only the alert-side KPIs and flag enrichment-dependent KPIs as provisional. | TEEP |
| The published '5.2h MTTA' baseline is an email-delay proxy, not a kg/hr-threshold measurement; if Sensirion cannot supply historical threshold timestamps the post-Maxwell MTTA is measured on a different clock, undermining the headline before/after comparison. | H | H | DATA-9 attempts true threshold reconstruction via the Sensirion-API workstream; failing that, state the proxy caveat explicitly in DATA-11 success criteria and report MTTA as 'threshold-to-classified' going forward with a documented clock change. | Joint |
| January class mix (70.2/27.4/2.4) is not representative across seasons (winter freeze-offs vs. summer venting), so the 40% auto-close and 92% accuracy targets are set against a non-representative distribution. | M | M | DATA-3 seasonality breakdown across the full history; revise target ranges before customer publication; design auto-close thresholds against the worst-case month, not the average. | Taikun |
| The Undetected class is extremely rare (4/168 in Jan), making per-class recall statistically unstable and the 92% accuracy claim fragile on the minority class. | H | M | DATA-7 over-samples rare classes in the validation set; report per-class metrics with confidence intervals; treat abstention-to-MRO (confidence <0.65) as the correct behavior for Undetected rather than forcing a guess. | Taikun |
| Human labels are themselves noisy/inconsistent (free-text how_cleared, trailing-space vocab dupes, operator-to-operator drift), capping measurable accuracy and making the 92% target ambiguous. | M | M | DATA-8 measures inter-annotator agreement on a 30-event overlap and reports accuracy relative to that ceiling; normalize vocab on ingest (DATA-2); Sierra reviews the free-text->category mapping weekly during operate. | TEEP |
| Validation-set leakage: tuning the classifier/prompt on data that overlaps the labeled set inflates measured accuracy. | M | H | Freeze the validation set with a snapshot hash (DATA-7) and contractually hold it out of all Agent-workstream tuning; only systematic error-mode descriptions (not the held-out rows) flow back to tuning. | Taikun |
| emissions.event_audit and the agent-performance KPI columns do not exist today; if the Agent workstream does not write acknowledged_at/auto_closed/dispatch timestamps consistently, the live scorecard reports nulls and the success criteria become unmeasurable. | M | H | DATA-4 lands the schema early and coordinates field-write contracts with the Agent workstream; backfill proxies (acknowledged_at=email_received, auto_closed=false) for historical rows so the scorecard never silently shows zero. | Taikun |
| ProCount/Carte owner is still TBD, blocking the enrichment-side history needed to label and validate the cross-system reasoning that drives auto-close decisions. | M | M | Escalate owner identification via Michelle/Clovis at Kickoff; until resolved, build the labeled set from Sensirion + TaskHub + Cygnet-via-existing evidence and mark ProCount/Carte-dependent labels provisional. | TEEP |

---

### CUTOVER · Integration Testing, Cutover, Go-Live and Operate

**Lead:** Taikun  ·  **Effort:** ~67 person-days  ·  **Tasks:** 19

**Objective.** Take Maxwell from a contract proven against a Prism mock to a live, monitored, production triage agent at TEEP Barnett: re-run the 17-endpoint smoke + 4-scenario agent simulator against TEEP's real gateway as each system's endpoints land, flip the bootstrap-to-gateway base URL per system (config-only, no rewrite), sequence go-live system-by-system with TaskHub write last (with email-fallback if it slips), wire failure-notification and production monitoring (Prometheus to CloudWatch + the Integration Health screen), then run a 60-day operate window with weekly mapping-table review with Sierra and close on a Phase-2 go/no-go.

This workstream is the "last mile" of Maxwell: everything other workstreams build (the gateway, the four read APIs, the TaskHub read+write API, the Sensirion integration, the Cygnet-via-intermediary pipe, SSO, and Bedrock) has to be assembled, proven end-to-end against the real systems, switched on without breaking the live path, and then operated for 60 days against measurable KPIs. The big asset we start with is real: the OpenAPI 3.1 contract (teep-api.yaml) is lint-clean, mock-served by Prism on the AWS test VM, smoke-tested 17/17, property-tested with ~300 Schemathesis cases, and driven by a 4-scenario agent simulator (auto-close / dispatch / monitor / timeout) — all green against the mock. The cutover promise we made to Darko in TESTING-EVIDENCE.md and the email is "same code, different --base-url": when TEEP's gateway lands we repoint the simulator and verify the contract holds in minutes. This workstream operationalizes that promise endpoint-by-endpoint rather than as one big-bang cutover.

The chosen approach is a phased, per-system go-live rather than a single cutover date, because the new asks from the customer call (Sensirion API integration, the net-new FMP/TaskHub API, SSO, Bedrock, and Cygnet-via-existing-intermediary) land on different schedules and each has a different risk profile. The four read systems (Sensirion, Cygnet, ProCount, Carte) carry no write risk and go live first as their endpoints become available, with each system's bootstrap S3-drop source flipped to the gateway base URL via config only. TaskHub write is sequenced last and gated behind a dedicated write-soak and idempotency-replay test, because a duplicate POST creates a duplicate field-dispatch task in front of a real lease operator — the one place a bug has physical-world consequences. The brief's email-fallback (agent emails MRO with the full evidence pack instead of POSTing a TaskHub task) is the explicit safety valve if the TaskHub write API slips past go-live; it preserves the office-cleared 56.5% of value on day one and is config-toggled per environment.

What is genuinely uncertain and honestly reflected here: the deck's aggressive "Phase 1 in 6-8 weeks from API availability" almost certainly slips once SSO production federation (gated by Sahir/TEEP IT change control), Bedrock cross-account enablement (gated by an unknown TEEP AWS account + region + model-enablement approval), and the Cygnet intermediary-path decision (no direct SCADA API allowed — must ride FMP's ~30-min poll, ProCount's documented Cygnet integration, or a historian/export, decided jointly with Mike + Darko) are sequenced as real go-live dependencies rather than assumed-ready. We model go-live as a rolling sequence and call the SSO and Bedrock production gates as blocking for full production cutover, while letting the read-system soak proceed against bootstrap/early-gateway data so the pilot clock can start. The 60-day operate window and KPI measurement only begin once the office-cleared auto-close path is live end-to-end on real data, because the success criteria (median MTTA ≤ 15 min, ≥40% auto-close, Sierra's report generated entirely from the agent store, no API incident forcing Darko to roll back access, and signed sign-off from Devin and Sierra) are all measured "after 60 days of production traffic."

Two cross-cutting realities shape every task. First, the 22-day January sample (168 alerts) is too short to firm seasonal KPIs, so the operate window doubles as the data-collection window that, alongside the 6-12 month historical pull, lets us recalibrate the Phase-1 (40%) toward the Phase-2 (65%) auto-close target with evidence. Second, monitoring is not optional polish — success criterion 5 ("no API-related incident requires Darko's team to roll back access") means the Integration Health screen, the Prometheus-to-CloudWatch metrics (teep_api_calls_total / latency / errors per system), the ≥5-failures-in-5-min notification threshold, and the >15-min-unavailable degraded-mode behavior (flag events Undetected, notify MRO, keep Sierra's dashboard up, never silently drop or invent a classification) all have to be live and demonstrated before TEEP will trust the agent in production.

**Key people**

| Name | Org | Role |
|---|---|---|
| Darko Jankovic | TEEP | Engineering / API gateway / security / governance — owns gateway availability, RFC7807 envelope, failure-notification channel sign-off, and the go/no-go on production access |
| Sierra | TEEP | HSE reporting coordinator — owner of the 22-column monthly report and the free-text-to-category mapping table reviewed weekly during the operate window |
| Sebastian | TEEP | TEEP engineer building the FMP/TaskHub read+write API + task.updated webhook; primary counterpart for the TaskHub go-live slice and email-fallback decision |
| Mike | TEEP | Owns Cygnet/SCADA; counterpart for the Cygnet-via-existing-intermediary go-live slice and SCADA data validation |
| Michelle | TEEP | Owns TaskHub/FMP + Sensirion(Nubo) relationship; counterpart for Sensirion go-live slice and poll-cadence confirmation |
| Sahir | TEEP | Cyber & Field Ops — Entra ID/SSO; counterpart for the SSO production cutover gate (TEEP users signing into the Taikun app) |
| Clovis | TEEP | Operations lead — co-owner of the 60-day pilot review and Phase-2 go/no-go decision |
| Devin | TEEP | MRO engineer (triage) — validates the escalation/timeout path and confirms in writing he would not return to the manual process (success criterion 6) |
| Brent | TEEP | HSE field lead — validates that field-dispatch tasks landing in TaskHub match LO expectations during go-live |
| IFS Merrick stack owner | IFS Merrick | ProCount/Carte production-accounting owner (TBD) — counterpart for the ProCount/Carte read go-live slice |
| Steve Ridder | Taikun | Founder/CEO — pilot sponsor, runs the 60-day review and Phase-2 proposal |

**Tasks**

| ID | Task | Owner | Phase | Days | Blk | Depends on |
|---|---|---|---|--:|:--:|---|
| `CUTOVER-1` | Cutover readiness kickoff + per-system go-live sequencing plan | Joint · Taikun delivery lead + Darko (TEEP gateway) + Steve | Kickoff | 2 | 🔴 | — |
| `CUTOVER-2` | Stand up the gateway-pointed test harness on the AWS test VM | Taikun · Taikun eng | Bootstrap | 3 | 🔴 | `CUTOVER-1` |
| `CUTOVER-3` | Smoke + simulator dry-run against bootstrap S3-drop data | Taikun · Taikun eng | Bootstrap | 3 |  | `CUTOVER-2` |
| `CUTOVER-11` | Production monitoring wiring — Prometheus to CloudWatch + Integration Health screen | Taikun · Taikun eng | Build | 4 | 🔴 | `CUTOVER-2` |
| `CUTOVER-12` | Failure-notification channel wiring (email default / Teams / PagerDuty) | Joint · Taikun eng + Darko (names contact + channel/key) | Build | 3 | 🔴 | `CUTOVER-11` |
| `CUTOVER-4` | Per-system contract conformance test as each gateway endpoint lands | Joint · Taikun eng (runs) + Darko (gateway fixes) + per-system owner | Build | 6 | 🔴 | `CUTOVER-2` |
| `CUTOVER-10` | Email-fallback path verification (TaskHub-write-slips contingency) | Taikun · Taikun eng + Devin (confirms MRO email is actionable) | Cutover | 2 |  | `CUTOVER-1` |
| `CUTOVER-13` | SSO production-cutover gate verification (TEEP users into the Taikun app) | Joint · Sahir (TEEP IT) + Taikun eng | Cutover | 3 | 🔴 | `CUTOVER-11` |
| `CUTOVER-14` | Bedrock production-LLM cutover verification (latency + data-residency) | Joint · Taikun eng + Darko (TEEP AWS/Bedrock access) | Cutover | 3 | 🔴 | `CUTOVER-2` |
| `CUTOVER-15` | Full end-to-end production rehearsal (all systems, all 4 scenarios) | Joint · Taikun eng (drives) + Darko + Michelle + Mike + Sebastian + Sierra | Cutover | 3 | 🔴 | `CUTOVER-8`, `CUTOVER-9`, `CUTOVER-11`, `CUTOVER-12`, `CUTOVER-13`, `CUTOVER-14` |
| `CUTOVER-5` | Sensirion go-live slice — live webhook ingestion verification | Joint · Taikun eng + Michelle (TEEP/Nubo) + Sebastian | Cutover | 3 | 🔴 | `CUTOVER-4`, `CUTOVER-12` |
| `CUTOVER-6` | Cygnet-via-existing-intermediary go-live slice — SCADA signal validation | Joint · Mike (TEEP, Cygnet) + Darko + Taikun eng | Cutover | 4 | 🔴 | `CUTOVER-4`, `CUTOVER-13` |
| `CUTOVER-7` | ProCount + Carte go-live slice — codes/comments/injection read validation | Joint · IFS Merrick stack owner (TBD) + Taikun eng | Cutover | 3 | 🔴 | `CUTOVER-4` |
| `CUTOVER-8` | Bootstrap-to-gateway base-URL cutover per read system (config-only) | Taikun · Taikun eng | Cutover | 2 | 🔴 | `CUTOVER-5`, `CUTOVER-6`, `CUTOVER-7` |
| `CUTOVER-9` | TaskHub write go-live — idempotency + duplicate-dispatch soak | Joint · Sebastian (TEEP, builds API) + Taikun eng + Brent (HSE field lead) | Cutover | 4 | 🔴 | `CUTOVER-4`, `CUTOVER-14` |
| `CUTOVER-16` | Production go-live + start of 60-day operate window | Joint · Taikun eng (operate) + Darko (gateway ops) + Clovis (ops oversight) | Operate | 2 |  | `CUTOVER-15` |
| `CUTOVER-17` | Weekly mapping-table review with Sierra (operate window) | Joint · Sierra (TEEP) + Taikun eng | Operate | 6 |  | `CUTOVER-16` |
| `CUTOVER-18` | Operate-window monitoring + incident handling against success criteria | Taikun · Taikun eng (on-call) + Darko (gateway incidents) | Operate | 8 |  | `CUTOVER-16` |
| `CUTOVER-19` | Pilot review + Phase-2 go/no-go decision | Joint · Steve + Clovis + Darko + Sierra + Devin | Operate | 3 |  | `CUTOVER-17`, `CUTOVER-18` |

**Deliverables:** Per-system go-live sequencing plan + RACI *(Joint)*; Gateway-pointed verification harness *(Taikun)*; Per-system gateway conformance reports *(Joint)*; Per-system go-live sign-offs *(Joint)*; Config-cutover changelog + rollback procedure *(Taikun)*; Email-fallback path (verified) + per-environment toggle *(Taikun)*; Production monitoring stack + Integration Health screen *(Taikun)*; Failure-notification runbook + verified alerting *(Joint)*; SSO + Bedrock production-cutover sign-offs *(Joint)*; Production-rehearsal report + go/no-go decision *(Joint)*; Weekly mapping-review log + versioned mapping config *(Joint)*; 60-day operate report measured against the 6 success criteria *(Taikun)*; Pilot review readout + Phase-2 go/no-go + scope proposal *(Joint)*

**Open decisions**

| Question | Owner | Recommended default | Needed by |
|---|---|---|---|
| Is the TaskHub write API (POST/PATCH /v1/fmp/tasks + task.updated webhook) confirmed ready by go-live, or do we launch on the email-fallback path? | TEEP | Plan for email-fallback at go-live (config-toggled), then flip to TaskHub write the moment Sebastian's API passes the idempotency soak — this lets the office-cleared 56.5% launch on time regardless. | Before the Cutover phase begins (gates CUTOVER-9 vs CUTOVER-10) |
| Which channel and which named contact should receive API failure notifications (email default, Teams, or PagerDuty/Opsgenie)? | TEEP | Email to a named Darko/Mike distribution as the default; add Teams via an integration key if TEEP provides one (PRD Q7). | Before CUTOVER-12 (failure-notification wiring), during the Build phase |
| Which existing intermediary pipe will source Cygnet SCADA signals (FMP/TaskHub ~30-min device poll, ProCount's documented Cygnet integration, or a historian/read-replica/curated export)? | TEEP | Default to the ProCount documented Cygnet integration if it carries the needed tags at usable cadence; otherwise FMP's ~30-min poll, accepting reduced 4h-pre-event-window fidelity. Decide jointly with Mike + Darko. | Before CUTOVER-6 (Cygnet go-live slice); ideally at kickoff since it shapes the Cygnet adapter |
| Who is the IFS Merrick stack owner at TEEP (ProCount + Carte), and can Carte's injection-rate data be served through the ProCount API so the separate Carte endpoint is dropped? | TEEP | Name the production-accounting owner (not Michelle/Mike) at kickoff; fold Carte into ProCount if injection data is exposed there, dropping the separate Carte slice (PRD Q-IFS). | Before CUTOVER-7 (ProCount/Carte go-live slice) |
| What is the TEEP AWS account number and the Bedrock region, and which Claude model(s) will be enabled for the Taikun agent? | TEEP | Use a TEEP-governed Bedrock region in us-east-1 if available to minimize latency vs the Taikun us-east-1 processing account; enable the current production Claude model the agent already uses. Do not assume the account/region — confirm with Darko. | Before CUTOVER-14 (Bedrock production-LLM cutover gate) |
| Are SSO production federation and Bedrock cutover hard prerequisites for starting the 60-day pilot clock, or can the operate window start on the Taikun-managed login + direct-Anthropic LLM path while those gates complete? | Joint | Start the pilot clock once the read-system auto-close path is live, using the Taikun-managed login + direct-Anthropic path; treat SSO + Bedrock as blocking for full production cutover only, not for read-system verification — reflecting the honest slip past the deck's 6-8 weeks. | At the cutover-readiness kickoff (CUTOVER-1) |
| What is the data-residency / latency acceptance bound for routing LLM classification through TEEP Bedrock vs the default direct-Anthropic path? | TEEP | Require decision-parity on 20+ known alerts and a per-classification latency within ~2x the direct path; keep direct-Anthropic as documented rollback if Bedrock latency or availability is unacceptable. | Before CUTOVER-14 (Bedrock cutover verification) |

**Risks**

| Risk | L | I | Mitigation | Owner |
|---|:--:|:--:|---|---|
| Duplicate TaskHub dispatch task created on a write retry — a bug here puts a real, duplicate field job in front of a lease operator (physical-world consequence). | M | H | Gate TaskHub-write go-live (CUTOVER-9) behind a dedicated idempotency-replay soak: replay the same Idempotency-Key on simulated 5xx and prove zero duplicates across 50+ writes within the 24h replay window before enabling write in production; keep email-fallback armed as the alternative. | Joint |
| TaskHub write API (net-new build by Sebastian) slips past go-live, blocking the field-cleared 43.5% close-the-loop. | M | H | Arm the config-toggled email-fallback (CUTOVER-10) so the office-cleared 56.5% goes live on schedule and field events get the email path with the same evidence pack until write turns on; sequence TaskHub write last so it never blocks read-system go-live. | Taikun |
| Cygnet intermediary path (FMP ~30-min poll / ProCount integration / historian) delivers SCADA signals at coarser cadence than the 4h pre-event window needs, weakening the pressure-drop signatures auto-close relies on and degrading classification accuracy below the >=92% target. | M | H | Validate signal fidelity against >=10 known Jan-2026 alerts during the Cygnet slice (CUTOVER-6); document achievable resolution; if too coarse, escalate intermediary-path choice to Mike + Darko and route ambiguous cases to Undetected->human rather than mis-auto-closing. | Joint |
| SSO (Sahir/TEEP IT change control) or Bedrock (TEEP AWS cross-account + model enablement) production gates slip, pushing full production cutover beyond the deck's aggressive 6-8 week aspiration. | H | M | Let read-system soak proceed against bootstrap/early-gateway data via the Taikun-managed login + default direct-Anthropic LLM path so the pilot clock can start; treat SSO and Bedrock as blocking only for full production cutover (CUTOVER-13/14), not for read-system verification; engage Sahir and Darko on lead times at kickoff. | Joint |
| An API incident in production forces Darko's team to roll back Taikun's access, failing success criterion 5 and ending the pilot. | L | H | Stand up monitoring + alerting (CUTOVER-11/12) and prove degraded-mode behavior before go-live; rate-limit writes to <=5/min; metadata-only logging; per-system config rollback to S3-drop source; immediate incident triage with a logged resolution path. | Taikun |
| The 22-day January sample (168 alerts) is unrepresentative, so production KPIs (especially the 40% auto-close target) diverge from the modeled baseline. | M | M | Use the 60-day operate window as a data-collection window and combine with the 6-12 month historical pull to recalibrate targets with evidence before publishing; report KPI trends weekly so divergence is caught early. | Taikun |
| Bootstrap S3-drop JSON shapes drift from the gateway's eventual responses, breaking the config-only cutover guarantee and forcing real adapter rework at cutover. | M | M | Run the bootstrap dry-run (CUTOVER-3) against S3-drop data and file any shape drift as a defect to the bootstrap workstream immediately; treat teep-api.yaml as the single frozen contract both sides validate against. | Joint |
| HSE/EPA category vocabulary drifts as new LO phrasings appear during the pilot, eroding classification accuracy and Sierra's report parity. | M | L | Weekly mapping-table review with Sierra (CUTOVER-17) capturing new phrasings as config changes (not code), with each cycle re-confirming the 22-column export matches her template. | Joint |
| IFS Merrick stack owner (ProCount/Carte) remains TBD, stalling the ProCount/Carte go-live slice and the Carte-fold-in decision. | M | M | Escalate owner identification at kickoff (cross-workstream open decision); meanwhile validate ProCount/Carte against bootstrap S3-drop data so the slice is ready the moment the owner and endpoints land. | TEEP |

---

## 4 · RACI (program level)

R = Responsible · A = Accountable · C = Consulted · I = Informed

| Activity | R | A | C | I |
|---|---|---|---|---|
| Program integration, sequencing, cross-org decision driving, customer comms | Steve Ridder (Taikun) + Taikun delivery lead | Steve Ridder (Taikun) | Darko, Clovis | All TEEP + Taikun stakeholders |
| Gateway platform (OAuth2, Idempotency-Key store, HMAC signing, RFC7807, tech choice) | Darko (TEEP) | Darko (TEEP) | Sahir, Taikun eng | Steve, Clovis, all system owners |
| Signed S3 bootstrap bucket + cross-account access + drop scheduling | Darko (TEEP) | Darko (TEEP) | Sahir (IT-sec), Taikun eng | Sebastian, Mike, IFS owner |
| IT-security / data-sharing / residency sign-off (Path A vs B, S3 cross-account, conditional access) | Sahir (TEEP) | Sahir (TEEP) | Darko, Steve | Taikun eng, Clovis |
| FMP/TaskHub net-new API (read + write + task.updated webhook) | Sebastian (TEEP) | Michelle (TEEP) | Darko, Devin, Taikun eng | Clovis, Brent |
| Sensirion/Nubo event integration (cadence, webhook availability, device→pad/well mapping) | Michelle + Sebastian (TEEP) | Michelle (TEEP) | Sensirion/Nubo support, Taikun eng | Steve, Sierra, Darko |
| Cygnet/SCADA via existing intermediary (path choice, tag map, signal validation) | Mike (TEEP) | Mike (TEEP) | Darko, Sebastian (if FMP path), IFS owner (if ProCount path), Taikun eng | Steve, Clovis |
| ProCount/Carte (IFS Merrick) integration + canonical asset spine + code taxonomy | IFS Merrick/ProCount owner (TBD, TEEP) | Clovis (to assign) (TEEP) | Devin, Darko, Taikun eng, IFS Merrick support | Michelle, Sierra |
| Identity / SSO — Entra app-reg, admin consent, groups claim, conditional access | Sahir (TEEP) | Sahir (TEEP) | Darko, Taikun eng, Steve | Devin, Sierra, Clovis, Mike, Michelle |
| AWS Bedrock — cross-account IAM, model enablement, residency governance | Sahir (TEEP AWS) + Darko (governance) | Darko (TEEP) | Steve, Taikun eng | Clovis |
| Taikun adapters, enrichment fan-out, rule-cascade + LLM triage, act paths, durable workflow | Taikun eng | Steve Ridder (Taikun) | Devin (triage logic), Mike (Cygnet), Sebastian (TaskHub) | Darko, Clovis |
| Asset registry build + binding + resolver generalization (Path A) | Taikun eng | Steve Ridder (Taikun) | IFS owner (spine), Mike (Cygnet asset_path), Michelle/Sebastian (Sensirion mapping) | Darko, Sahir |
| Auto-close policy + gate graduation (shadow→human-confirm→autonomous) | Taikun eng + Devin (TEEP) | Clovis (TEEP) | Sierra, Michelle, Darko | Steve, Brent |
| Reporting — Sierra HSE/EPA export, controlled vocabularies, override-with-audit, 4 screens | Taikun eng | Sierra (TEEP, acceptance) | Devin, Darko (audit immutability), Clovis | Steve, Mike |
| KPIs, labeled validation set, accuracy measurement, methane estimator, success criteria | Taikun eng | Steve Ridder + Clovis (joint) | Sierra, Devin (labels), Brent (HSE method), Michelle (Sensirion timestamps) | Darko |
| Integration testing, per-system cutover, production rehearsal, go-live | Taikun eng | Steve Ridder (Taikun) + Darko (TEEP gateway ops) | Sebastian, Mike, Michelle, Sahir, IFS owner | Clovis, Devin, Sierra, Brent |
| 60-day operate, monitoring/incident handling, weekly mapping review, Phase-2 go/no-go | Taikun eng (on-call) + Sierra (mapping) | Clovis (TEEP) + Steve Ridder (Taikun) | Devin, Darko, Brent | Michelle, Mike, Sahir |

---

## 5 · Consolidated risk register

| Risk | L | I | Mitigation | Owner | Workstream |
|---|:--:|:--:|---|---|---|
| Microsoft Entra enterprise app-registration + admin consent is owned/gated by central TotalEnergies group IT (not the Barnett BU) and takes 1-3+ weeks, blocking the SSO production gate and pushing full governed go-live past the deck window. | H | H | File the app-reg request in week 0-1 as a blocking task; resolve the consent-owner decision in SSO-1 and escalate via Darko immediately if central IT owns it. Keep the Taikun-managed login + read-system soak running so the pilot clock can still start; SSO gates only full production cutover, not read-system verification. | Sahir (TEEP) | SSO/CUTOVER |
| AWS Bedrock Claude model-access approval in the TEEP account lags (hours-to-days plus possible TotalEnergies procurement/security review), blocking the production-LLM cutover. | H | M | Submit the model-access request first at kickoff (BEDROCK-2); run the entire pilot on the existing direct-Anthropic gateway route as the fallback so go-live is never blocked on Bedrock — only the governed-inference cutover slips, not the pilot. | Sahir/Darko (TEEP) | BEDROCK |
| TaskHub is the only net-new API with no external API today; Sebastian's read+write+webhook build (~26 dev-days) is the critical-path tail and likely overruns the deck's 8 weeks. | H | H | Decouple sides: Taikun develops fully against Prism mock + S3 bootstrap so it is cutover-ready early; the config-toggled email-to-MRO fallback keeps field events flowing if write slips; sequence read before write and write last in the go-live order. | Sebastian/Michelle (TEEP) | FMP |
| A duplicate POST to TaskHub on a transient retry creates a duplicate real field-dispatch task in front of a lease operator — the one bug class with physical-world, trust-eroding consequences. | M | H | Contract-mandatory 24h Idempotency-Key replay store (key+body-hash, not key-only) built jointly by Sebastian+Darko; Taikun replays a stable key per logical write; gate write go-live behind a dedicated zero-duplicate soak across 50+ replayed writes before any real LO receives tasks. | Sebastian/Darko (TEEP) | FMP/CUTOVER |
| The chosen Cygnet intermediary (esp. FMP ~30-min poll) is too coarse to reproduce intra-event pressure transients the 4h@5m series needs, or omits casing/compressor tags, degrading classification accuracy and auto-close below the ≥92% / 40% Phase-1 targets. | H | H | Run the 95-alert resolution-impact replay (SCADA-9) before committing; default to a historian/curated-export feed where one exists; mark missing tags nullable (no faked values); route ambiguous coarse-cadence cases to Undetected/MRO rather than mis-auto-closing; hold the historian fallback (SCADA-10) ready and renegotiate the SCADA-attributable KPI slice honestly if needed. | Mike/Darko (TEEP) | SCADA |
| Nubo→TEEP path is poll-based (no native Nubo webhook), so TEEP re-emits on its own cadence (FMP reportedly ~30-min), capping end-to-end latency and gutting the MTTA <10min KPI even though Taikun's webhook is instant. | M | H | Resolve webhook-vs-poll-re-emit in SEN-1; if no native webhook, push TEEP to poll Nubo at the true device cadence (not the 30-min FMP cadence) and re-emit on threshold-cross immediately; measure real MTTA at cutover and restate the KPI honestly if the path caps it. | Michelle/Sensirion-Nubo | SEN |
| ProCount/Carte (IFS Merrick) has no named TEEP owner — discovered by mining notes, owned by nobody on the call — and ProCount is the canonical asset spine everything binds onto, so a late owner stalls the spine, the registry, and every cross-system fan-out. | H | H | Make owner assignment the first kickoff action (IFS-1); escalate to Clovis as ops lead; sequence the ProCount spine dump first and build registry ingest against R2Q data in parallel so Taikun is not idle; engage IFS Merrick support interim. | Clovis (TEEP) | IFS/REG |
| TEEP has no API gateway today and standing one up from zero plus IT-security data-sharing sign-off is the single biggest non-Taikun schedule risk; the bootstrap channel depends on it. | H | H | Front-load the gateway tech decision (GW-1) and IT-sec sign-off (GW-2) in week 0-1; run the bootstrap S3 track fully in parallel so Taikun is unblocked regardless of gateway timeline; communicate a realistic 3-5 week gateway build with cutover trailing. | Darko (TEEP) | GW |
| Bootstrap S3 JSON shapes drift from teep-api.yaml (extra/missing fields, casing), breaking the config-only-cutover promise and forcing adapter rework at cutover across multiple systems. | M | M | Freeze shapes with example files + schema_version in every manifest (GW-4); Taikun validates each drop against the Prism/Schemathesis contract in CI before consuming, failing loudly on drift (GW-6, CUTOVER-3); same adapter validated against both mock and bootstrap. | Taikun eng + TEEP system owners | GW/ALL |
| All headline KPIs (MTTA 5.2h→<10min, 0→40% auto-close, ≥92% accuracy, ~70t CH4 avoided) are extrapolated from a 22-day, 168-alert sample with the Undetected class at only 4/168, so targets may be set against a non-representative seasonal distribution and the minority-class accuracy claim is statistically fragile. | M | M | Secure the 6-12 month historical pull (DATA-1); recompute baselines + seasonality (DATA-3); over-sample rare classes in the frozen held-out validation set (DATA-7); report accuracy relative to the inter-annotator ceiling (DATA-8); treat the 60-day operate window as the firming data and recalibrate before committing. | Taikun eng + Sierra/Clovis (TEEP) | DATA |
| The published 5.2h MTTA baseline is an email-delay proxy, not a kg/hr-threshold-crossing measurement; if Sensirion cannot supply historical threshold timestamps, the before/after comparison is on different clocks, undermining the headline KPI. | H | H | DATA-9 attempts true threshold reconstruction via the Sensirion-API workstream; failing that, state the proxy caveat explicitly in the signed success criteria and report post-Maxwell MTTA as threshold-to-classified with a documented clock change. | Michelle/Sensirion-Nubo + Taikun eng | DATA/SEN |
| Fully-autonomous auto-close (writing final regulatory fields + sending Sierra's closeout email with no human touch) closes events incorrectly, eroding trust or mis-stating an HSE/EPA record. | M | H | Ship behind a 3-state auto-close gate defaulting to shadow; require confidence ≥0.85; graduate shadow→human-confirm→autonomous only after Devin reviews divergences and Clovis signs off; Sierra can override any auto-populated field with an audited change. | Clovis/Devin (TEEP) + Taikun eng | AGENT |
| The 24h durable monitor/timeout fails to survive an agent restart/redeploy, leaving dispatched events stuck open with no escalation. | L | H | Run the monitor/timeout on the durable substrate (DBOS) — explicitly NOT exempt from durable execution; resume in-flight events from status='enriching'; test forced-restart-mid-monitor in AGENT-16; rely on supervisord auto-restart on the VM. | Taikun eng | AGENT |
| emissions.event_audit and the agent-performance KPI columns do not exist in the current schema; if the agent does not consistently write acknowledged_at/auto_closed/dispatch timestamps, the live scorecard reports nulls and the signed success criteria become unmeasurable. | M | H | Land the instrumentation migration early (DATA-4/AGENT-2/3) and agree the field-write contract jointly between DATA and AGENT; backfill proxies (acknowledged_at=email_received, auto_closed=false) for historical rows so the scorecard never silently shows zero. | Taikun eng | DATA/AGENT |
| Disabling interim auth at SSO cutover locks everyone out if the EntraID config is subtly wrong (tenant GUID typo, mismatched byte-for-byte redirect URI behind CloudFront, expired secret), and resetting the VM off origin/main has previously broken auth. | M | H | Cut over only after acceptance passes on the test VM with identical config; retain a documented out-of-band break-glass internal admin; provide instant rollback (re-enable internal login + restart); set AUTH_PUBLIC_BASE_URL for the CloudFront origin split; never reset the VM off origin/main. | Taikun eng + Sahir | SSO |

---

## 6 · Open decisions — close in the kickoff working session

These are the cross-workstream decisions that unblock the most downstream work. Bring the right owner to each.

| Question | Owner | Recommended default | Needed by | Workstream |
|---|---|---|---|---|
| Which gateway technology, primary auth method (OAuth2 client-credentials vs mTLS), bootstrap transport (S3 drops vs SFTP vs read-replica), and asset-registry path (A vs B) will TEEP use? (Four linked foundational decisions.) | Darko + Sahir (TEEP) | AWS API Gateway in TEEP's account; OAuth2 client-credentials with 1h tokens + emissions.read/write scopes; signed S3 JSON drops (Option A); asset-registry Path A (Taikun ingests catalogs). Fall back to Path B only if IT-security blocks data sharing. | Kickoff week 0 (GW-1/GW-2) — gates the entire gateway + bootstrap build | GW/REG |
| Is TaskHub write (POST/PATCH) confirmed in Phase-1 scope, does Maxwell auto-close or does the LO close via UI with a webhook, and can TaskHub emit an outbound webhook or must Taikun poll? | Clovis + Michelle + Sebastian + Darko (TEEP) | Write IS in Phase-1 scope (required for the ~27% dispatch close-the-loop); LO closes via the TaskHub UI with the task.updated webhook recording it (lower trust barrier); build the HMAC webhook if feasible but build the 5-min poll backstop regardless; email-to-MRO fallback if write isn't live by week 4. | Kickoff (FMP-1) — gates the entire write build | FMP |
| Does Nubo offer a native webhook to TEEP (true event-speed) or must TEEP poll Nubo and re-emit (latency capped by TEEP's poll cadence)? And what is the authoritative device poll cadence (device_poll_seconds) and PPM/kg-hr sample resolution? | Michelle / Sensirion-Nubo (TEEP) | If no native webhook, TEEP polls Nubo at the confirmed true device cadence (not the 30-min FMP cadence) and re-emits on threshold-cross immediately; proceed on a 60s interim cadence default for series resolution and backfill once Nubo confirms. | Kickoff (SEN-1/SEN-2) — before contract freeze and KPI sign-off | SEN |
| Which existing intermediary sources the 6 Cygnet/SCADA signals (FMP ~30-min poll / ProCount documented Cygnet integration / historian or curated S3 export), and does it carry casing pressure + compressor suction/discharge at usable cadence? | Mike + Darko (TEEP) | Use a CygNet historian/curated-export feed if one exists (closest to 4h@5m fidelity, cleanest contract fit); fall back to FMP's ~30-min cache for state corroboration only and accept reduced series resolution as a documented Phase-1 limitation, conditional on the SCADA-9 replay clearing targets. | Kickoff week 0 (SCADA-2/3) — gates the adapter build and KPI grounding | SCADA |
| Who is the accountable TEEP business + technical owner for ProCount/Carte (IFS Merrick), does ProCount expose the 14-digit API number per well (deterministic Strategy A binding), and should Carte be dropped and served from ProCount OData? | Clovis to assign; IFS Merrick support (TEEP) | Assign the production-accounting/RRC-Form-PR lead with Michelle confirming and IFS Merrick support as technical contact; confirm API# presence (Strategy A if present, fuzzy Strategy C + normalization if not); drop the separate Carte API and serve injection_rate from ProCount OData, keeping the /v1/carte/ contract slot. | Kickoff week 0-1 (IFS-1/IFS-3, REG-3) — gates the canonical asset spine | IFS/REG |
| What is the TEEP/TotalEnergies Entra tenant GUID, who owns Enterprise App registration + admin consent (Barnett BU vs central group IT), and what is the production Taikun hostname + exact byte-for-byte redirect URI? | Sahir (TEEP) | Use the platform's existing OIDC EntraID provider (SAML out of scope); obtain the tenant GUID from Sahir; assume Sahir drives within the BU but escalate via Darko if central IT owns consent; default to the demo.taikunai.com host callback unless TEEP prefers a TEEP-branded host, setting AUTH_PUBLIC_BASE_URL for the CloudFront split. | Kickoff week 0-1 (SSO-1/SSO-2) — enterprise consent lead time is the top schedule risk | SSO |
| What is the TEEP AWS account id, the Bedrock region, which Claude model(s) are enabled, and is cross-account AssumeRole-from-Taikun-us-east-1 acceptable for Phase 1 (vs gateway in TEEP's VPC)? | Sahir + Darko (TEEP) | Confirm account/region (do not fabricate); prefer Bedrock region us-east-1 to match Taikun's gateway and minimize latency; enable a Claude 3.5 Sonnet-class primary + Claude 3 Haiku-class fast tier; use cross-account AssumeRole + external-id for the pilot and defer co-located gateway to Phase 2 unless Darko mandates it now. | Kickoff/Bootstrap (BEDROCK-1/2/3) — model-access approval is a long-pole | BEDROCK |
| How are dispatch + API-failure notifications delivered (email / Teams / PagerDuty) and to which named TEEP contact, and what kg/hr trigger threshold X qualifies an event for dispatch (FR-3)? | Darko (TEEP) | Email to a named Darko/MRO distro as the Phase-1 default (add Teams/PagerDuty if TEEP supplies an integration key); default trigger threshold 1 kg/hr until TEEP HSE confirms. | Build (GW-17/CUTOVER-12/FMP-10) — before observability + email fallback ship | GW/FMP/CUTOVER |
| Who provides Sierra's exact live HSE/EPA column list, sheet scope (alerts-only vs 5-sheet workbook), controlled vocabularies, and rulings on the dirty values ('Process Emissions ' trailing-space dupes, blank Resolution Type)? | Sierra (TEEP) | Use the 'Sensirion Alert Data' 22-column set, alerts-sheet-only for Phase-1, with canonicalized vocabularies (trim/collapse dupes, default blank Resolution Type to 'Undetected'); keep 'Process Emissions' as the legitimate catch-all equipment value; treat all of this as config so a mid-pilot change is a config edit. | Kickoff (REPORT-1/AGENT-1) — gates the export + the agent write-map | REPORT/AGENT |
| When will the 6-12 month historical pull be delivered, must the kg/hr-threshold MTTA baseline be reconstructed vs the email proxy, and does the 60-day pilot clock start at API-availability cutover or at end-to-end-live steady state? | Sierra + Clovis + Michelle/Sensirion-Nubo (TEEP) | Target 12 months (min 6), Sensirion + notes first; reconstruct true threshold MTTA via Sensirion device data if available, else publish the proxy with a documented clock caveat; start the 60-day clock only after the read-system auto-close path is live end-to-end on real data. | Kickoff/Bootstrap (DATA-1/9, before DATA-11/12) — gates KPI sign-off | DATA |
| What is the go-live posture for autonomous auto-close (shadow only / human-confirm / autonomous for Process Emissions), and is the platform's 3-role model extended for the HSE-reporting persona? | Clovis + Devin + Sierra (TEEP) | Default to shadow at go-live, graduate to human-confirm after Devin reviews ~1-2 weeks of divergences, then autonomous for Process Emissions only with Clovis sign-off (dispatch/escalate can go autonomous earlier, lower blast radius); realise HSE-reporting as viewer + reporting/export entitlement rather than a schema change. | Before Operate (AGENT-18) and SSO-3 (Build) respectively | AGENT/SSO |

---

## 7 · Gaps & what's still missing

Completeness critique of the combined plan — items worth deciding on before or during kickoff.

| Gap | Why it matters | Suggested owner |
|---|---|---|
| No MSA / commercial pilot contract or pricing terms are anywhere in the 12 workstreams — the entire plan assumes a signed engagement that does not appear to exist yet, and Bedrock/API egress costs are tracked but never tied to a commercial agreement. | Without a signed MSA + pilot SOW, none of the data sharing, production access, or 60-day commitment is contractually binding; TEEP IT-security sign-off (GW-2) and Nubo data access may be blocked pending legal terms, and a slipped contract silently slips week 0. | Steve Ridder (Taikun) + Clovis/TEEP procurement |
| No Data Processing Agreement / DPA is called out as a deliverable. The plan repeatedly asserts us-east-1 residency, metadata-only logging, ~3yr aggregate retention, and PII redaction, but there is no contractual DPA binding those commitments, and TotalEnergies (an EU-parented major) will almost certainly require one. | Methane/EPA data + LO PII + lat/lon device locations cross into Taikun's AWS account and into Bedrock; absent a DPA the residency/redaction posture is a promise, not an obligation, and IT-security (Sahir) cannot fully sign off cross-account S3 sharing or Bedrock egress. | Darko/Sahir (TEEP legal+security) + Steve (Taikun) |
| No independent security review / penetration test of the inbound webhook receiver, the gateway integration, the SSO federation, or the autonomous write path is scheduled, despite this being a customer-facing system that writes real field dispatches and handles corporate identity. | TotalEnergies enterprise security will likely mandate a pen-test/security assessment before granting production access; discovering this requirement late (after build) could block CUTOVER and is exactly the kind of gate that fails success-criterion 5 (no API incident forcing a roll-back). | Sahir (TEEP Cyber & Field Ops) + Taikun eng |
| TEEP-side engineering capacity is wildly concentrated and never resourced as a constraint: Sebastian owns the net-new FMP API + Sensirion proxy + catalog drops + bootstrap exports; Darko owns the entire gateway, idempotency store, HMAC, RFC7807, S3, and governance; Sahir owns SSO + Bedrock IAM + IT-security. There is no backup/bandwidth plan if any one of them is unavailable. | The critical path runs through a handful of named TEEP individuals doing parallel heavy lifts; one person's PTO, reassignment, or competing priority can stall multiple workstreams simultaneously — this is the most likely real-world cause of slip and is currently invisible. | Clovis (TEEP ops lead) + Steve (Taikun) |
| No change-management / end-user training plan for the lease operators, MRO team, or Sierra. The plan validates that screens exist and Devin reviews UX, but there is no operator onboarding, no LO guidance on receiving agent-generated dispatch tasks, and no training on the new TaskHub task type. | Success criterion 6 is that Devin and Sierra would not return to manual; if LOs distrust or ignore agent-created dispatch tasks, or Sierra never adopts the one-click export, the pilot fails on adoption regardless of technical correctness. Brent's HSE field team especially needs LO-facing change management. | Brent (HSE field lead) + Devin (TEEP) + Taikun delivery |
| Nubo/Sensirion AG is on the critical path (device cadence, webhook availability, API semantics, possibly historical threshold timestamps) but there is no confirmed contractual or technical access channel to Nubo — every Nubo task routes 'via Michelle' to an 'owner contact TBD'. | The MTTA KPI and the true-baseline reconstruction (DATA-9) depend on Nubo answers Taikun cannot obtain directly; if Nubo engagement is slow or requires a separate vendor contract, the latency-critical Sensirion path and the headline KPI stall with no Taikun-side mitigation. | Michelle (TEEP, Nubo relationship) + Sensirion/Nubo support |
| No production support model / SLA / on-call escalation contract beyond 'Taikun eng on-call' for the operate window. There is no defined response time for a production incident, no after-hours coverage statement, and no agreement on who fixes a TEEP-side gateway/FMP outage at 2am during a real methane event. | This is an autonomous system acting on live emissions with regulatory implications; an undefined support/SLA model means an outage during a real release has no committed response, exposing both safety and the no-roll-back success criterion. TEEP will expect an SLA before production. | Steve (Taikun) + Darko (TEEP) jointly |
| Rollback is specified per-component (config flip to S3, re-enable internal login, direct-Anthropic fallback, email-to-MRO) but there is no consolidated whole-system rollback runbook or a defined 'kill switch' to safely halt all autonomous action at once if the agent misbehaves in production. | Component rollbacks assume you know which component failed; a systemic misbehavior (e.g. mis-classification cascade auto-closing real leaks) needs a single, rehearsed, fast way to stop all autonomous writes/closes while preserving the dashboards — without it, an incident could compound before per-component rollbacks are identified. | Taikun eng + Darko (TEEP) |
| Regulatory/compliance validation of the autonomous HSE/EPA record is unaddressed: Maxwell auto-writes resolution_type, epa_identifier, how_cleared and sends a closeout email that becomes part of a regulatory filing, but no one validates that an AI-populated record is acceptable to the RRC/EPA or that 'resolution_personnel=Maxwell AI' is a defensible regulatory entry. | If the regulator or TEEP's own HSE/legal team deems an AI-auto-closed event non-compliant as a filed record, the entire auto-close value proposition collapses retroactively; this needs a ruling before autonomous auto-close graduates, not after. | Sierra + Brent (TEEP HSE) + TEEP legal/regulatory |
| Inter-workstream contract ownership and change control for the shared artifacts (teep-api.yaml, the EnrichmentEvidence/TriageResponse schemas, the emissions.event_audit table, the shared canonicalization module, the Sierra column write-map) is implicit. Multiple workstreams write to emissions_api.py and the same migrations with no single owner or merge-conflict process. | AGENT, REPORT, and DATA all edit emissions_api.py and emissions.* migrations; the canonicalizer is shared between REPORT and AGENT; event_audit is shared by AGENT/REPORT/DATA. Without a named contract owner and change-control gate, concurrent edits collide and the 'config-only cutover' invariant can be silently broken by an uncoordinated schema change. | Steve / Taikun eng lead (single contract owner) |

---

## 8 · Next steps

1. **Internal review** of this plan (Taikun) — adjust scope, owners, and effort.
2. **Kickoff working session** with TEEP — close the Section 6 decisions; confirm system owners (especially IFS Merrick / ProCount + Carte) and the Cygnet-via-existing-systems intermediary.
3. **Lock the calendar** — set Week 0 to a real date; convert relative weeks to dates.
4. **Stand up the tracker** — render [`project-plan.json`](project-plan.json) as the monday.com-style board (swimlanes by workstream/phase, owner split Taikun vs TEEP, blocking + critical-path flags).

*Backing data: [`project-plan.json`](project-plan.json). Source material: `02-prd.md`, `03-architecture.md`, `04-system-integrations.md`, `05-security-answers.md`, `07-asset-binding-integration.md`, `teep-api.yaml`, `sierra-xlsx-analysis.md`, `Taikun Pilot Scope.pdf`.*
