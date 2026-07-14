# Switchboard — Vision Brief
### The full-stack software firm, in a box

> **One operator, a fleet of agents, and the entire lifecycle of building software —
> from an incoming client to a paid invoice — collapsed into a phone.**

- **Status:** vision brief (north star + honest grounding)
- **Owner:** Steve
- **Date:** 2026-07-13
- **Companion concepts:** [`docs/mobile/`](mobile/) — `switchboard-flagship.html` (red) / `-nike.html` (volt),
  `switchboard-rga-in-a-box.html`, `switchboard-complete-end-to-end.html`, `switchboard-v4-shop.html`,
  and the [`READY-WAVE-SPEC.md`](mobile/READY-WAVE-SPEC.md).

---

## 1. The one-sentence thesis

Every project tool makes a **human** the operator — the AI summarizes, suggests, drafts, and the
person does the work. **Switchboard inverts it:** the agent fleet *creates, schedules, and does the
work*; the human *supervises, approves, and redirects*. The software is the observation deck, not
the workplace. If a human ever has to open the board to keep the plan honest, we've failed.

## 2. What we own today (the beachhead)

**We own the end-to-end cycle of software development, in a box.** This is not aspirational — it is
the shipping control plane:

| Stage | Owned by | Primitive |
|---|---|---|
| Intake a goal / PRD / transcript | goal→plan synthesis | `intake.py`, `build_plan_artifacts.py` |
| Break it into a dependency-aware plan | mission + dependency graph | `mission_graph.py`, deliverable breakdown |
| Assign the right work to the right agent | the scheduler | `claim_next` (`+TXP`), `peek_wave` (spec'd) |
| Build it — many agents, in parallel | the fleet | dispatch, runtime adapters, file leases |
| Keep it honest | evidence gates | `deliverable_closure`, provenance ("Done = merged") |
| Track cost → outcome | the ledger | `Tally` (`+OXP`) |
| Report the truth | narration + rollups | `mission_status`, `mission_narrative` |

That is a **complete software delivery firm** reduced to two small processes on one cheap VM. The
beachhead claim is defensible and demo-able **today**: *a plan that ships itself, with proof.*

## 3. The full vision (both firms in a box)

Software gets built by **two kinds of firm**, and today they're two vendors with a lossy handoff
between them:

- **The creative / innovation firm** (R/GA, IDEO, Frog) — the **front**: strategy, the big idea,
  brand, experience design. The *taste* and the *why*. High-margin, prestige, hard for AI to own.
- **The elite delivery firm** (the Ukrainian / Polish engineering & hardware houses) — the
  **execution**: senior engineering, QA, craft, shipping the hard thing at quality. The *how*.
  Under-credited, where the real differentiation usually lives.

The agency decks it and throws it over the wall; the delivery shop rebuilds it. **Value, time, and
fidelity leak out of the handoff.**

**Switchboard collapses both into one operator and one fleet — with no handoff.**

> The human is the creative director (taste, direction, the client relationship).
> The fleet is the delivery house (execution, at scale, with proof).
> One roof. One loop. Nothing lost in the middle.

We are not claiming the AI has taste — it doesn't yet, and buyers know it. We are claiming
**your taste, at the scale and cost of a fleet.** You supply the judgment R/GA charges millions
for; Switchboard supplies the execution the delivery firms are elite at — fused.

## 4. The operating model — client to invoice

The whole business is one loop; the phone runs all of it. The agents own the middle.

```
01 Win        land the client, capture the brief          (BD + strategy)
02 Shape      PRD → scope → priced SOW, e-signed          (BA + architect + pre-sales)
03 Kickoff    the signed scope becomes a live task graph  (solution architect)
04 Draw work  it lays out in parallel waves; dispatch     (delivery manager)   ← engine
05 Build      the fleet designs, builds, merges, proves   (the developers)     ← engine
06 Track      progress, cost, honest evidence gates       (QA + PMO)           ← engine
07 Supervise  approve · redirect · veto; pinged when needed(delivery manager)
08 Deliver    hand off, client accepts, invoice releases  (delivery + finance)
```

Steps 04–06 are the live engine. The commercial edges (01–02, 08) are the wrapper that turns the
engine into a **business**.

## 5. The role-collapse

A software program today takes a dozen-plus specialists across two firms. Switchboard folds every
role into **you + the fleet**:

**Creative firm →**
- Executive Creative Director → **you** (the principal)
- Strategy Director / planners → strategy agent
- Creative Director → concept agent
- Design Director → design agent (tokenised system)

**Delivery firm →**
- Solution Architect → plan + dependency graph
- Delivery / Project Manager → autopilot loop + action queue
- Developers ×N → the build fleet (`claim_next`)
- QA Engineers → CI gates + closure verification
- DevOps → dispatch + CI + release
- Finance / PMO → `Tally` → P&L → invoice

## 6. The economics (why it's a category, not a tool)

| | Two-firm status quo | Switchboard |
|---|---|---|
| People | 12–50 specialists across 2 vendors | 1 operator + a fleet |
| Timeline | months (plus handoff drag) | weeks |
| Delivery cost | $1–2M in labour | thousands in agent spend |
| Gross margin | ~35–45% | ~90%+ |
| The middle | a lossy vendor handoff | **no handoff** |

The margin line *is* the pitch: bill like an agency, deliver at fleet cost. As the internal docs
already saw it — **"the trojan horse is the invoice."**

## 7. The product surface — a control room in your pocket

The UI is the window, not the workplace. The mobile concepts (see `docs/mobile/`) render the whole
loop as a native app:

- **Kickoff** — describe a goal; the plan authors itself.
- **Draw the work** — the dependency graph as parallel **waves**; dispatch a batch through the real
  scheduler, transparently (candidates, skips, model/budget guidance).
- **Autopilot** — a mission-control cockpit that drains ready work; drop into any agent's live
  session to redirect or stop it; file leases prevent collisions.
- **Deliverables** — honest, evidence-backed tracking. *Done means merged, with proof.*
- **Supervise** — a portfolio home + an approve/deny action queue; push only when a human call is
  genuinely needed. Close the phone; trust the fleet.
- **Deliver & bill** — a white-label client portal, acceptance sign-off, per-engagement P&L,
  invoice.

Design language: the Taikun system (`taikun-tabler.css`) — a single ownable accent, native SF type,
dynamic-island frames, dark control-room signatures, cinematic mesh statement bands. **Brand
decision open: taikun red (enterprise-legible, already ours) vs. black + electric volt (bolder, more
"new category"). We commit to one and own it relentlessly.**

## 8. The moat

1. **We own the whole chain.** Neither the creative firm nor the delivery firm can claim end-to-end;
   each owns half. We remove the handoff between them.
2. **Honesty as a feature.** Done requires merged provenance; closure gates never optimistically
   pass; the brief is derived from status, not agent optimism. This is what makes the invoice
   *defensible* — and it's hard to fake.
3. **Cost-to-outcome, not cost-to-token.** `Tally` ties spend to verified outcomes and to a
   per-engagement P&L — the commercial unit competitors don't have.
4. **A scheduler, not a ledger.** `claim_next` makes Switchboard the thing that *assigns* work
   across a fleet, atomically and explainably — not a passive board.

## 9. Honest grounding — what's real vs. the expansion

- 🟢 **Live today:** intake, dispatch (`claim_next`), live narration, redirect/stop, deliverables &
  `mission_status`, dependency map, closure gates, file leases, cost (`Tally`). *The whole software
  delivery floor.*
- 🟡 **Thin adds:** `ready_wave` (the batch/keystone read behind Draw-the-work), the PRD (Document
  Engine section-set), the SOW scope (from the plan breakdown), the portfolio rollup — reads over
  primitives we already have.
- 🔴 **The real build:** the full autopilot loop (PR→merge→auto-dispatch), push/APNs, the
  **commercial layer** (client entities, e-sign SOW, per-engagement P&L, white-label, invoicing),
  and the **creative front** (strategy / concept / design-system / prototype / launch / measure as
  new specialist agent types). `P0-SPEC` parks the commercial and multi-tenant pieces on purpose —
  this vision is when we turn them on.

## 10. Horizons

1. **Prove the beachhead** — the end-to-end delivery loop, on real primitives, on a live engagement.
2. **Wire the money** — `Tally` spend → a live per-engagement P&L. The margin screen on real numbers.
3. **Ship the wrapper** — `ready_wave`, the PRD/SOW artifacts, the client portal, the invoice.
4. **Grow the front** — strategy, concept, and design agents. From delivery firm → full-stack firm.

## 11. Open questions

- **Brand:** red or black+volt? (Commit to one; own it for years.)
- **First vertical:** internal dogfood → a design partner → who?
- **Commercial model:** fixed-price (margin story) vs. dedicated-fleet retainer (AOR) first?
- **Where the human's taste plugs in** — the highest-leverage point for the operator's judgment.

---

*The plan ships itself. You just supply the taste — and the invoice.*
