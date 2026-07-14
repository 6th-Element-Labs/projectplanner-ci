# Switchboard — Native Mobile App (design & wireframes)

High-fidelity wireframes for a **true native iOS app** for Switchboard, built around the
three things that matter on a phone:

1. **Kickoff** — describe a goal (text / PRD / deck / voice) → `intake.py` goal→plan synthesis
   returns the full workstream (epics, tasks, deps, owners, estimates) to *approve*, not author.
2. **Autopilot** — the autonomous delivery loop (`dispatch.py`): a mission-control cockpit that
   drains ready tasks (dispatched → building → review → merged) with budget, throughput, and
   outcome tracking, plus per-agent live sessions you can **redirect** or **stop**.
3. **See it live** — the observation deck: a deliverable against honest, evidence-backed gates
   ("claimed done, PR open"), the whole fleet at a glance, and the governed **action queue**
   (dispatch / chase / ship) — supervise → approve → redirect.

## View it

Self-contained files (no external assets, no build) — open any in a browser; top-right toggle previews light/dark:

- **[`switchboard-flagship.html`](switchboard-flagship.html) — THE PITCH. Start here.**
  The real product given the R/GA cinematic treatment: dark mesh statement bands, big-idea typography, a
  manifesto, the real client-to-invoice loop (real screens, grounded), and the persuasion devices — the arc,
  the **role-collapse** (Sales/BA/architect/PM/devs/QA/DevOps/finance → one operator + a fleet), the
  **compression math** (~12 people / weeks / ~40% → 1+fleet / days / ~94%), and the honest grounding ledger.
- **[`switchboard-rga-in-a-box.html`](switchboard-rga-in-a-box.html) — "R/GA in a box." The innovation-firm vision (hypothetical Nike).**
  The premium reframe: not a dev shop but an elite innovation agency. Runs a hypothetical Nike flagship
  ("Nike Pulse") A–Z — **Pitch → Strategy → Big idea → Design → Prototype → Build → Launch → Measure** —
  with the role-collapse (ECD/strategy/creative/design/tech/producer/analytics → one principal + a fleet),
  the compression math (~40 people / months / ~40% margin → 1 + fleet / weeks / ~99%), and honest grounding
  (build/track/cost live; creative front = new agent types).
- **[`switchboard-complete-end-to-end.html`](switchboard-complete-end-to-end.html) — the full software-delivery app, one story.**
  v3 (the engine) and v4 (the business) merged into one continuous 26-screen narrative:
  **Win → Price (PRD/SOW) → Kickoff → Draw the work → Autopilot → Deliverables → Supervise → Deliver & invoice.**
  Nothing cut — the whole client-to-invoice loop, screen by screen.
- **[`switchboard-v4-shop.html`](switchboard-v4-shop.html) — "software shop in a box." The business wrapper alone.**
  Wraps the v3 engine in the client-services lifecycle: **Client → Brief → PRD → Scope/SOW → (v3: Build & Track) →
  Deliver → Invoice** — engagement pipeline, brief capture, PRD doc, priced SOW with margin, per-engagement P&L
  (Tally→cost), white-label client portal, and merged-PR→accept→invoice handoff.
- **[`switchboard-v3-complete.html`](switchboard-v3-complete.html) — the definitive product design (the factory floor).**
  The complete 20-screen app told as one story: **Kickoff → Draw the work → Autopilot → Deliverables → Supervise**,
  with grounding badges (🟢 live in code / 🟡 thin add / 🔴 roadmap) baked into every action.
- [`switchboard-mobile-app.html`](switchboard-mobile-app.html) — v1: the original **Kickoff → Autopilot → Live** cut.
- [`switchboard-deliverables-mobile.html`](switchboard-deliverables-mobile.html) — v2: the **Deliverables** view in depth
  (waves, parallel lanes, keystones, dependency map, closure/proof, file-lease presence).
- [`switchboard-v3-mobile.html`](switchboard-v3-mobile.html) — v3 slice: dispatch sheet + autopilot spectrum + push + portfolio.

### Deliverables & parallelism — the model behind it

- **Draw the work = waves.** Wave 1 is the ready/unblocked set (`claim_next` eligibility; `peek_next`
  previews without claiming). Each later wave unlocks when its **keystone** merges. Waves are dependency
  order, not a calendar.
- **Parallel lanes** are workstreams (IDP / DATA / QA …) that run concurrently; a capacity meter shows how
  many free runners a wave can absorb, and **file leases** flag collisions before two agents touch the same file.
- **Honest by construction.** Done means merged with terminal provenance; a Done task without proof shows
  **"Done · no proof"**; closure grades **PASS / WAIVE / HOLD** and a required gate with no result reads **NOT RUN**.
- **Dependency map** rebuilt for a phone as swipeable depth columns (prerequisites left → dependents right),
  node color = real state, blocker ringed, external deps dashed.

## Design language (traces to the shipping app)

- **Brand** — Taikun red `#c0392b` as the only accent (and the "live" signal); ink `#101114`;
  white canvas; hairline `#e2e5ea`. Semantic green/amber/slate are kept separate from the accent
  and used only for state. Matches `static/taikun-tabler.css`.
- **Type** — native **SF Pro** (system stack) for authentic on-device feel + CSP-safety, paired
  with **SF Mono** for the technical voice (task IDs, session handles, spend, telemetry).
- **Native patterns** — dynamic-island frame, status bar, a 5-tab bar with a raised center
  **Kickoff** action, swipe-approve cards, live pulse dots, home indicator.

Wireframes are directional; every surface maps to a real control-plane module
(`intake.py`, `dispatch.py`, narration/interrupts, deliverable evidence gates, the action queue, Tally).
