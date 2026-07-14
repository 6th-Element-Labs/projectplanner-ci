# R/GA-in-a-Box — Product Requirements & Technical Design

### The eight rituals that turn a delivery engine into a full-stack firm

- **Status:** PRD v1 — scoped for build, grounded in shipped primitives
- **Owner:** Steve
- **Date:** 2026-07-14
- **Companions:**
  - [`docs/ui/switchboard-flow.html`](ui/switchboard-flow.html) — the 16-stage lifecycle wireframe (all 8 rituals, clickable)
  - [`docs/ui/switchboard-scope-studio.html`](ui/switchboard-scope-studio.html) — the scope-and-discuss workspace
  - [`docs/ui/switchboard-rga-stages.html`](ui/switchboard-rga-stages.html) — Concepts + Case Study deep-dives
  - [`docs/VISION-BRIEF.md`](VISION-BRIEF.md) — the north star this executes against

---

## 0. Thesis & scope discipline

Switchboard already owns the **delivery floor**: intake → dependency-aware plan → `claim_next`
dispatch → merge-proven closure → Tally cost-to-outcome. That is the *delivery firm*.

This PRD specs the **creative firm** on top — the eight R/GA rituals — as thin, honest layers
over primitives that exist today. The discipline for every ritual is the same:

> **No new engines.** Each ritual is (a) a new *artifact type*, (b) an *agent role* that authors
> it, (c) an *approval path* that reuses the existing attention model, and (d) a *render* through
> one shared deck/document engine. If a ritual needs a new database engine, scheduler, or workflow
> runtime, the design is wrong.

Legend used throughout:
- ✅ **SHIPPED** — exists in the repo today (file:line cited where load-bearing)
- 🟡 **THIN** — new read/render over shipped data; days not weeks
- 🔴 **NEW** — genuinely new write-path or agent role; scoped and phased

---

## 1. The shared substrate (build once, every ritual uses it)

Three cross-cutting pieces are prerequisites. They are the real "engine" of this PRD; the eight
rituals are mostly configurations of them.

### 1.1 The Artifact store 🔴 (the one new table this PRD needs)

Every ritual produces a versioned, discussable artifact (POV, territory, brief, deck, crit
report, playback, case study). Today, artifacts live implicitly (plan meta, deliverable fields,
`plan-docs/`). We make them first-class:

```sql
CREATE TABLE artifacts (
  id            TEXT PRIMARY KEY,          -- "art-<16hex>"
  project       TEXT NOT NULL,
  deliverable_id TEXT,                     -- nullable: strategy can precede a deliverable
  kind          TEXT NOT NULL,             -- pov | territory | brief | prd | sow | deck |
                                           -- crit_report | playback | case_study | design_tokens
  schema        TEXT NOT NULL,             -- e.g. "switchboard.strategy_pov.v1"
  version       INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'draft',  -- draft | proposed | approved | superseded
  content_json  TEXT NOT NULL,             -- the typed payload (validated against schema)
  authored_by   TEXT,                      -- agent_id or principal_id
  approved_by   TEXT,
  created_at    REAL NOT NULL,
  approved_at   REAL
);
```

Design rules, copied from what already works in this codebase:
- **Immutable versions.** Edits append a new row with `version+1` and mark the prior
  `superseded` — same append-only discipline as `decisions_store.record_decision()`
  (✅ `decisions_store.py:23-58`, statuses `accepted|superseded|proposed`).
- **Every write emits an `activity` event** (`kind="artifact.created" / "artifact.approved"`),
  exactly as `report_usage` emits `tally.usage_reported` (✅ `store.py:10631`). The activity log
  stays the single source of history.
- **Approval routes through the attention model** (§1.3), never a bespoke button-to-DB path.

Endpoints (FastAPI, mirroring the `/tally/v1/*` conventions ✅ `app.py:2833-2972`):
```
GET  /artifacts/v1?project=&deliverable_id=&kind=
GET  /artifacts/v1/{id}
POST /artifacts/v1                 (agent or human author; status=draft|proposed)
POST /artifacts/v1/{id}/approve    (writes approved_by, emits activity, resolves attention item)
POST /artifacts/v1/{id}/supersede
```
Plus MCP tool mirrors (`artifact_create`, `artifact_approve`) in `mcp_server.py`, because the
authoring agents reach the control plane over MCP, not REST.

**Effort:** ~3–4 days including migrations, MCP tools, and activity wiring.

### 1.2 The Deck/Document engine 🟡

One renderer, four consumers (kickoff deck, playback mini-deck, strategy deck, case study).

- **Input:** an artifact's `content_json` (typed sections) + a slide-template set.
- **Templates:** the exact HTML/CSS slide system already proven in
  `switchboard-flow.html` (16:9 `.stage16`, `.slide--cover`, pillars, filmstrip). Extracted into
  `static/decks/templates/*.html` with `{{token}}` slots — **no client-side framework**; it's
  string-template + the existing Tabler/taikun tokens (✅ `taikun-tabler.css:14`,
  `--tblr-primary:#c0392b`).
- **Render paths:**
  1. *Live view* — served as a page inside the app (the wireframe already does this).
  2. *PDF/PNG export* — headless Chromium print-to-PDF, run as a background job in
     `background_jobs.py` (✅ job runner exists). Chromium is already a deploy dependency for
     nothing else, so this is the one new system package; acceptable.
- **Why not slides-as-LLM-HTML every time?** Determinism and brand. The LLM fills *sections*
  (typed JSON); the engine owns layout. Agents can't drift the brand.

**Effort:** ~4–5 days (template extraction, `deck_render.py`, background export job, `GET
/decks/v1/{artifact_id}.pdf`).

### 1.3 Approval = the attention model (no new approval system) ✅ reused

Everything a human must decide already flows through one engine:
`store._mission_next_actions()` with the explicit ownership model — `owner_type`, `attention`,
`automatic`, `delivery_impact` (✅ `store.py:2518-2620`). The only two triggers that page a human
today are `approve_breakdown` (a `pending_proposal.status == "proposed"`) and
`request_human_approval` (a task's `human_gate.blocked`). The coordinator halts on them via
`HUMAN_ESCALATION` and returns `status:"human_required"` (✅ `mission_coordinator.py:8-25,83-86`).

**We extend the action vocabulary, not the mechanism.** New attention-bearing actions:

| action                 | fires when                                            | owner_type     | impact    |
|------------------------|-------------------------------------------------------|----------------|-----------|
| `approve_pov`          | a `pov` artifact reaches `proposed`                   | project_owner  | at_risk   |
| `choose_territory`     | ≥2 `territory` artifacts `proposed` on one deliverable| project_owner  | at_risk   |
| `approve_prototype`    | prototype milestone hits its evidence gate            | project_owner  | blocking  |
| `resolve_crit`         | a crit report contains ≥1 `must_fix`                  | reviewer       | blocking  |
| `client_playback_due`  | milestone closure PASS + playback not yet sent        | project_owner  | none      |
| `kpi_below_target`     | measurement job finds a KPI < target for N periods    | project_owner  | at_risk   |

Each is ~15 lines in `_mission_next_actions()` following the `_action()` helper pattern, plus a
row in `ACTION_PRIORITY`. The Supervise surface and the coordinator pick them up for free. The
existing test contract (`test_mission_attention_model.py`) extends to cover each new action.

**Effort:** ~1 day per action, including tests.

---

## 2. The eight rituals

The lifecycle, with the phase map used in the wireframe:

```
STRATEGY   1 Intake        ✅ shipped (intake.py, build_plan_artifacts.py)
           2 POV           🔴 ritual #1
CONCEPT    3 Big Idea      🔴 ritual #2
DEFINE     4 Brief(+deck)  🟡 artifact + deck engine
           5 PRD           🟡 artifact (sections already produced by intake synthesis)
           6 SOW           🔴 commercial edge — parked fields noted below
           7 Scope         ✅ shipped (deliverable_breakdown.py + Scope Studio UX)
DESIGN     8 System        🔴 ritual #3
           9 Prototype     🔴 ritual #4
BUILD     10 Build         ✅ shipped (claim_next, sessions, leases)
          11 Crit          🔴 ritual #5
          12 Monitor       ✅ shipped (mission page, Gantt, closure)
          13 Playback      🔴 ritual #6
SHIP      14 Wrap          ✅ shipped (closure + acceptance)
GROW      15 Case study    🟡 ritual #7
          16 Measure       🟡 ritual #8
```

---

### Ritual 1 — Strategy / POV (before the brief)

**What.** A point-of-view artifact: the challenge *reframed*, audience insight, competitive
teardown, why-now, and the metrics that matter. R/GA never opens with execution; neither do we.

**UX.** `switchboard-flow.html` stage 2 — POV document on the left, discuss rail on the right,
"Export strategy deck" via the deck engine, gate = *POV agreed? Concepts explore against this.*

**HOW — data.** Artifact `kind=pov`, `schema=switchboard.strategy_pov.v1`:

```json
{
  "reframe":        {"headline": "...", "rationale": "..."},
  "audience":       [{"who": "...", "insight": "..."}],
  "teardown":       [{"player": "...", "read": "...", "our_lane": false}],
  "why_now":        "...",
  "metrics":        [{"name": "SSO success", "unit": "percent", "direction": "increase",
                      "target_value": 95, "baseline_value": 62, "period": "release"}]
}
```

**HOW — the strategy agent.**
- A gateway call site — `call_site="strategy"` — added to the enum already carried on every
  `llm_spend` row (✅ `db/schema.py` call_site: `ask_plan|summarize|ocr|digest|coding`). It runs
  through the local LiteLLM gateway exactly like `ask_plan`, so the spend is `source=gateway`,
  `confidence=exact` — the POV's cost is *provider-actual* on Tally from day one.
- Context assembly reuses the existing RAG path (✅ `rag.py` — the Ask agent already retrieves
  over `plan-docs/`): transcript + RFP + any uploaded competitor docs are ingested through the
  existing Add-to-corpus flow (✅ live on the Overview page), then the strategy agent gets
  top-k chunks + the intake summary as its prompt context.
- Output is *structured*: the agent must return `strategy_pov.v1` JSON (schema-validated on the
  artifact POST; malformed → retry with validator errors, same propose-to-confirm discipline the
  Ask agent uses for task edits).

**HOW — metrics seeding (the load-bearing bit).** On POV approval, each entry in `metrics[]`
becomes a **real KPI row** via the existing `POST /tally/v1/kpis` (✅ table `kpis`,
`db/schema.py:441-455`, fields `name/unit/direction/baseline_value/target_value/period` map
1:1 — the schema was designed for exactly this and is currently under-fed). This is what makes
Measure (ritual 8) automatic later: the strategy *literally is* the measurement contract.

**HOW — approval.** Artifact `proposed` → `approve_pov` attention item → approving writes
`approved_by`, records an ADR via `decisions_store.record_decision(title="POV: <reframe>")`
(✅ immutable decision log), and unblocks the Concepts stage (which refuses to run without an
approved POV — a simple precondition check, not a new state machine).

**Failure modes & mitigations.**
- *Hallucinated competitive claims* → teardown entries carry a `sources[]` list of corpus chunk
  ids; render shows "unsourced" badges on entries with none. Cheap, honest.
- *POV theater (word salad)* → the schema forces a falsifiable `reframe.headline` ≤ 140 chars and
  numeric `metrics[]`. No numbers, no approval button.

**Effort:** ~1 week (agent prompt + call site: 2d; seeding + approval wiring: 1d; UI: 2d).

---

### Ritual 2 — Concept territories → the Big Idea

**What.** 2–3 named, divergent approaches explored **in parallel**, each with a one-liner, an
approach sketch, tradeoffs, and a cost/time/risk shape. You pick one; it becomes the scope.

**UX.** Stage 3 — three `territory` cards, Taikun's read banner, choose → "this becomes the
scope." (Built in `switchboard-flow.html` + `switchboard-rga-stages.html`.)

**HOW — parallel generation.**
- N **independent** gateway calls (`call_site="concept"`), one per territory, each seeded with
  (a) the approved POV, (b) a *divergence instruction* ("optimize for experience" / "optimize for
  risk retirement" / "optimize for cost"), and (c) a **different model or temperature** per lane
  — the same diversity principle as a judge panel. They run as plain `async` fan-out inside one
  FastAPI background job; **no new orchestration** (these are single-shot generations, not
  multi-step agent sessions, so the wake/dispatch substrate is overkill here).
- Each result lands as an artifact `kind=territory`, `schema=switchboard.concept_territory.v1`:

```json
{
  "name": "Parity-First",
  "one_liner": "Lead with the CI-parity gate. Go-live risk retired on day one.",
  "sketch": {"nodes": [{"id":"build","label":"Build"},{"id":"parity","label":"Parity ✓"}],
             "edges": [{"from":"build","to":"parity"}]},
  "tradeoffs": [{"dir":"up","text":"Lowest go-live risk"},{"dir":"down","text":"..."}],
  "shape": {"tasks": 21, "days": 9, "est_usd": 3400, "risk": "low"},
  "draft_breakdown_ref": "art-...",
  "proposed_by": "nova/concept#3f1a"
}
```

**HOW — honest estimates (`shape`).** This is where most concept stages lie. Ours doesn't,
because each territory generation *also* runs the existing LLM breakdown draft
(✅ `deliverable_breakdown.py` — target_projects, milestone titles, roles) in *draft* mode.
- `tasks` = count from the draft breakdown (not a vibe).
- `est_usd` = `tasks × cost_per_verified_outcome` from Tally history for similar work — the
  rollup already exists (✅ `store.task_tally()` `unit_cost.cost_per_verified_outcome`,
  `store.py:10900-10948`); we add a small helper that medians it over the project (or global)
  when history is thin, and **labels the basis** (`"basis":"project_median_n=14"` vs
  `"basis":"default"`). No history → show the default badge, don't fake precision.
- `days` = draft wave count × observed wave-drain rate (from activity timestamps of prior
  `task.claimed`→merge events; fallback default, labeled).

**HOW — the sketch render.** `sketch` is a tiny node/edge JSON rendered by the same code path
that renders the dependency map pills (✅ `static/js/mission.js:462` node/pill rendering) — NOT
mermaid, so it works without the CDN and stays on-brand.

**HOW — choosing.** `choose_territory` attention item. Choice does three writes atomically:
1. Winning territory → `approved`; siblings → `superseded` (kept forever — they're valuable
   memory: "we considered Invisible Sign-In and rejected it because…").
2. `decisions_store.record_decision()` ADR: *"Direction: Parity-First over A/C; rationale."*
3. The winner's `draft_breakdown_ref` is promoted to the deliverable's `pending_proposal` — which
   drops us **exactly into the shipped `approve_breakdown` flow** (✅ `store.py:2544-2549`). The
   Big Idea literally becomes the scope through the existing pipe.

**Failure modes.**
- *Three flavors of the same idea* → divergence instructions are part of the job spec; a cheap
  post-check embeds the three one-liners and rejects/regenerates if pairwise cosine similarity
  is above threshold (embedding via the gateway's existing `taikun-embed`).
- *Operator rubber-stamps the recommended one* → fine. The value is that the alternatives and the
  ADR exist. Taste is a right, not homework.

**Effort:** ~1.5 weeks (fan-out job + schema: 3d; estimate helper: 2d; choose-flow wiring: 2d;
UI polish: 2d).

---

### Ritual 3 — The living Design System (a deliverable, not an artifact)

**What.** Tokens + components the fleet builds against, owned by a design-system agent,
versioned, consumed by every build deliverable. "Design systems, not artifacts."

**HOW — it's a deliverable, and the graph already supports it.** The design system is a
*deliverable of type* `design_system` in a shared home project whose linked tasks live in the
repo that owns the tokens. Other deliverables reference it with the **existing context-link role
`foundation`** (✅ `CONTEXT_LINK_ROLES = {"foundation","parked"}`, `mission_graph.py:16` — kept
out of the flow map, promoted only when depended on). Zero new graph semantics.

**HOW — the tokens are code, so proof is merge.**
- Canonical source: `design/tokens.json` (W3C design-tokens format) + generated
  `tokens.css` in the target repo — the same pattern as the shipped `taikun-tabler.css` brand
  block (✅ the ONLY place brand tokens are declared, per its own header comment). The
  design-system agent's tasks produce **PRs against that file** — so provenance, closure, and
  Tally all apply unchanged. A token change is Done when it's merged. No parallel design-tool
  state to reconcile.
- Components: a `static/system/components.html` gallery page (the wireframe's stage 8 is the
  mock) rendered from the token file — the gallery *is* the visual regression baseline.

**HOW — the fleet builds against it.**
1. **Task-side:** build tasks in consuming deliverables get
   `required_capabilities: ["design-system-v3"]` — the capability gate already exists in the
   claim filter (✅ `_task_required_capabilities`, `store.py:9101`; skip counter
   `capability_mismatch`). Agents declare the capability when their bootstrap includes the
   token snapshot.
2. **Session-side:** the work-session bootstrap injects the current `tokens.json` + component
   inventory into agent context (the same mechanism that already binds work sessions to claims —
   ✅ work_session plumbing in `claim_next`). Version pin recorded on the claim so a crit later
   knows *which* system version the work was built against.
3. **Enforcement:** soft at build time, hard at crit time (ritual 5 fails drift).

**HOW — "living".** Version bumps are milestones on the design-system deliverable. On merge of a
token change, a background job re-renders the gallery and posts a `heads_up` signal (✅ interrupt
tier vocabulary, `docs/INTERRUPT-TIERS-SPEC.md:191-209`) to active sessions in consuming
deliverables: "system v3→v4; tokens changed: color.fallback-link."

**Failure modes.** *Token sprawl* → the schema caps semantic tokens; additions require the
design-system agent to supersede, not append (crit enforces usage of semantic, not raw, values).

**Effort:** ~1 week (token file + generator: 2d; capability/bootstrap wiring: 2d; gallery: 1d;
version-bump job: 1d). The *agent role* is a prompt + capability profile, not new infra.

---

### Ritual 4 — Prototype-to-decide ("make to think")

**What.** A rough clickable prototype, built cheap and early, that gates the expensive build.

**HOW — it's a milestone with teeth, not a new object.**
- The breakdown template gains an optional first milestone **"Prototype the idea"** (joining the
  shipped default titles — ✅ "Define shared contract / Build core implementation / Integrate
  cross-board / Prove parity and ship", `deliverable_breakdown.py`). 1–3 tasks, one agent.
- **Budget cap:** dispatched with `max_budget_usd` (✅ a first-class `claim_next` parameter with
  `_budget_status` thresholds `ok|tight|over_budget`, `store.py:9520-9532`) — e.g. $400. The
  prototype cannot silently become the build.
- **Artifact:** static HTML/JS against the design-system tokens, merged into the target repo
  under `/prototypes/<deliverable>/` — merge provenance applies; the mission page embeds it in a
  sandboxed iframe (`sandbox="allow-scripts"`, same-origin static file, no backend).
- **The gate:** all post-prototype milestones' tasks carry
  `agent_state.human_gate = {blocked: true, reason: "prototype approval"}` at breakdown time —
  the **shipped** human-gate mechanism (✅ `_task_human_gate_state`, `store.py:3279`; surfaces as
  `request_human_approval`, skip counter `human_approval` in `claim_next`). Approving the
  `approve_prototype` attention item clears the gates in one write. The scheduler enforces the
  ritual; no one has to remember it.
- **Client option:** the prototype can be shared into the playback portal (ritual 6) for early
  client reaction — same signed-URL mechanics.

**Failure modes.** *Prototype ≠ build drift* → the crit agent receives the prototype URL as
reference input for build-task crits; divergence becomes a crit note automatically.

**Effort:** ~4 days (breakdown template + gating writes: 2d; iframe embed + budget preset: 2d).

---

### Ritual 5 — Crit gates (taste, not just CI)

**What.** CI proves it works; the crit proves it's *good*. A review agent grades work against
the design system + your standing taste notes, before acceptance.

**HOW — a third gate inside the shipped closure engine.** `deliverable_closure` runs two gates
today — scope + functional — with gate kinds `store_check | offline_evidence | script | pytest`,
and the crucial property that an unrun required gate **holds the grade closed**
(✅ `deliverable_closure/__init__.py:93-310`; `_grade()` → `pass|waive|hold` at `:315-319`;
"never optimistically passed"). We add gate kind **`crit`**:

- When closure runs and a required `crit` gate has no fresh report, the verifier dispatches a
  **crit agent** exactly the way closure verification is dispatched today — a `message_only`
  wake (✅ `request_closure_verification()`, `deliverable_closure/__init__.py:544-636`,
  signal `deliverable.closure_verification`; we add `deliverable.crit_requested`).
- The crit agent's inputs: the merged diffs (provenance gives exact PRs), the pinned
  design-system version from the claim, the rendered pages/screenshots (headless Chromium — same
  dependency as the deck engine), and the operator's **taste notes**.
- Output artifact `kind=crit_report`, `schema=switchboard.crit_report.v1`:

```json
{
  "graded_against": "design-system@v3",
  "notes": [
    {"severity": "pass",     "title": "Uses system tokens", "detail": "..."},
    {"severity": "must_fix", "title": "Fallback link fails contrast token",
     "detail": "...", "suggested_task": {"title": "Bump fallback link to ink-2", "est_h": 1}},
    {"severity": "nit",      "title": "Loading state has no motion", "detail": "..."}
  ]
}
```

- **Grading rule:** `must_fix` present → gate `pass:false` → closure grade **HOLD** (the shipped
  grader does this for free). `nit`s never block; they log. The operator can **WAIVE** —
  and waivers are already first-class in the closure report (✅ `waivers[]`,
  `grade:"waive"`), so taste overrides are recorded, not silent.
- **Must-fix → work:** accepting a must-fix creates a linked task from `suggested_task` (role
  `implementation`) — it enters the normal claim/merge/closure loop. The crit's teeth are the
  scheduler, not a comment thread.

**HOW — taste notes (the memory).** A small standing list, stored as `decisions_store` entries
with `task_id="taste"` (append-only, supersedable — the semantics we want, zero new tables):
*"contrast is non-negotiable"*, *"prefer motion on state change"*. The crit prompt always
includes the active set. Every crit you correct (waive or add) can append a note — the crit
agent gets *more like you* per engagement. This is the moat version of "your taste at fleet
scale."

**Failure modes.**
- *Nitpick storms* → severity budget in the prompt (≤1 must_fix per component unless a token is
  violated) + must_fix requires citing **which token/note** it violates; uncited must_fix is
  demoted to nit by the validator.
- *Rubber-stamp crits* → crit agent is a different model tier than the builder
  (`_model_recommendation` tiers ✅ `store.py:9561` — build `balanced`, crit `high`), and never
  the agent that authored the PRs (claim history makes this checkable).

**Effort:** ~1.5 weeks (gate kind + dispatch: 3d; crit agent + screenshot harness: 4d; taste
notes + UI: 2d).

---

### Ritual 6 — Playbacks (client checkpoints)

**What.** Every milestone gets played back to the client — a mini-deck of what shipped, with
evidence, and a one-click accept. Not just the end; every phase.

**HOW — trigger and content.**
- Trigger: milestone closure grade `PASS|WAIVE` (✅ the closure report is the event source) fires
  `client_playback_due`. Generating the playback is automatic; *sending* it is the human action
  (attention item, `impact:none` — a nudge, not a page).
- Content: artifact `kind=playback` assembled from data we already store — milestone title +
  linked tasks + merged-PR counts (provenance), acceptance-criteria status, and one number the
  client cares about (KPI current vs. target). Rendered by the deck engine into the 3-slide
  mini-deck (the wireframe's stage 13 card is the template).

**HOW — the client can actually see it (the real work here).** Auth today is
principal/session-based with `PM_AUTH_MODE` (✅ `auth.py` — password principals + hashed expiring
session cookies). We add the smallest possible client surface:
- A **scoped share token**: `create_scoped_token(project, scopes="read:playback:<deliverable>")`
  — the scoped-token concept already exists for the gateway's Tally ingest
  (✅ `.env.example:18-25` documents `create_scoped_token(... scopes="write:ixp")`). Reuse it
  with a read scope.
- Playback URL: `/p/<deliverable>/<playback_id>?t=<token>` → server-rendered, read-only,
  white-label page (the client-portal wireframe, `switchboard-client-portal.html`). No client
  accounts, no new IdP — a signed link with expiry, revocable like any token.
- **Accept** posts back with the same token → writes an `outcomes` row
  `type=milestone_acceptance`, `status=verified`, `verification=human` (✅ table + enums,
  `db/schema.py:423-440` — `human` is already a verification kind) and stamps the playback
  artifact `approved`. Client acceptance is thereby a *verified outcome* — it shows up in Tally's
  verified denominator and later in the case study, for free.
- Optional email delivery via the shipped SMTP path (✅ `comms.py` / `PM_SMTP_*`).

**What we deliberately do NOT build now:** invoicing/payment release on accept. The acceptance
*event* is recorded (that's the hard part); the commercial layer stays parked per `P0-SPEC`, as
the vision brief already decided. When it lands, it subscribes to `outcome:milestone_acceptance`.

**Failure modes.** *Stale links* → tokens expire (TTL like sessions ✅ `session_ttl_seconds`);
re-send mints fresh. *Client confusion* → playback shows exactly the acceptance criteria they
signed in the SOW — same wording, from the same artifact.

**Effort:** ~1.5 weeks (scoped read token + portal route: 4d; playback assembly + deck: 3d;
accept→outcome wiring: 1d).

---

### Ritual 7 — The Case Study / showcase (post-ship)

**What.** The moment the engagement closes, package challenge → approach → craft → results into
the document that wins the next client.

**HOW — pure composition; nearly everything exists.** A generator (background job on
engagement close, or the "Generate case study" button) that reads:
- **Challenge** ← the approved POV artifact (reframe + why-now) — ritual 1.
- **Approach** ← the chosen territory + its ADR rationale — ritual 2.
- **Craft, with proof** ← linked tasks grouped by milestone with merged-PR counts from
  provenance, closure `grade` + gate results (✅ closure report fields
  `gates/acceptance_criteria_results/evidence_hash`).
- **Results** ← `deliverable_tally()` proven/in-review/spend split (✅ `store.py:3150-3239`) +
  KPI current-vs-baseline from `kpi_tally()` (✅ `store.py:10951`).
- **Quote** ← playback acceptance comments (ritual 6 captures an optional client comment field).
- **Narrative glue** ← `mission_narrative` (✅ shipped narration path) polished by one
  `call_site="casestudy"` gateway pass — the *numbers are never LLM-generated*, only the prose;
  every stat in the render binds to a store read, listed in a `sources` block on the artifact.

Output: artifact `kind=case_study` → deck engine → (a) in-app showcase page, (b) PDF export,
(c) optional public static publish (S3/Caddy static dir) with client-name redaction toggle
(white-label rules from ritual 6 apply).

**Also — the memory edge:** the generator appends the engagement's routing precedent to the
decision log (*"parity-first sequencing cut end-of-project risk; claude-code on identity 3/3
clean"*) — which is exactly the precedent text the Fleet dispatch banner already cites in the
console wireframe. Case study out, precedent in, next engagement routes smarter.

**Failure modes.** *Puffery* → the sources block is rendered in the footer of the artifact
("every number links to the ledger"); a claim without a source id fails schema validation.

**Effort:** ~1 week (generator + schema: 3d; showcase route + publish: 2d).

---

### Ritual 8 — Measurement & optimize (post-launch)

**What.** Track the KPIs the work was meant to move; when a number lags, propose the optimize
deliverable. Ship → measure → optimize → renew.

**HOW — the tables literally exist.** `kpis` (baseline/current/target/direction/period ✅) and
`outcome_kpi_links` (contribution + confidence `measured|estimated|directional` ✅
`db/schema.py:441-468`), with rollups in `kpi_tally()` and per-KPI cost via
`cost_per_kpi_contribution_unit` (✅). What's missing is **ingestion** and a **watchdog**:

1. **Ingestion — `POST /tally/v1/kpis/{id}/observations`** 🔴 small:
```sql
CREATE TABLE kpi_observations (
  id TEXT PRIMARY KEY, kpi_id TEXT NOT NULL, project TEXT NOT NULL,
  value REAL NOT NULL, observed_at REAL NOT NULL,
  source TEXT NOT NULL,        -- webhook | manual | agent_probe
  meta_json TEXT DEFAULT '{}'
);
```
   Three source paths, cheapest first: **manual** (operator types the week's number in the
   Measure surface — day one), **webhook** (client's analytics posts with a scoped write token —
   same token machinery as ritual 6), **agent_probe** (a scheduled cheap-tier agent runs a
   read-only check, e.g. hits the staging SSO health endpoint; spend lands on Tally under
   `call_site="measure"`). Each observation updates `kpis.current_value`.
   Outcome verification via `verification="external_metric"` is already an enum value on
   `outcomes` (✅) — observations can verify outcomes that were `proposed` at ship time.

2. **Watchdog — in `background_jobs.py`** (✅ runner exists): per active KPI, if
   `direction`-adjusted `current_value` misses `target_value` for N consecutive observations →
   emit `kpi_below_target` attention item **with a drafted optimize proposal attached**: the
   coordinator's existing `propose_breakdown` path (✅ an action in the shipped vocabulary,
   `store.py:2615`) seeded with the KPI, the crit notes near the offending flow (ritual 5's
   reports are queryable by component), and a small budget. Approving it is just
   `approve_breakdown` — a new mini-engagement is born inside the shipped pipe.

3. **Surface** — the Measure stage (wireframe stage 16): three KPI cards (current vs. target vs.
   baseline, sparkline from observations), the Taikun proposal banner, "Scope optimize v1.1."
   Chart rendering uses the vendored ApexCharts (✅ `static/vendor/apexcharts/`).

**Failure modes.** *No data* → cards show "no observations yet — connect a webhook or log
weekly"; the watchdog never fires on empty series (no false pages). *Metric gaming* → the KPI
contract came from the approved POV (ritual 1) and is immutable post-SOW except by ADR.

**Effort:** ~1 week (observations table + endpoints: 2d; watchdog + proposal seeding: 2d;
Measure surface: 2d).

---

## 3. Agent roles introduced (prompts + policy, not infrastructure)

| Role            | Call path                          | Tier (via ✅ `_model_recommendation`) | Spend attribution |
|-----------------|------------------------------------|----------------------------------------|-------------------|
| Strategy agent  | gateway, `call_site=strategy`      | high                                    | gateway/exact     |
| Concept agents  | gateway ×N, `call_site=concept`    | high, diversified                       | gateway/exact     |
| Design-system   | normal fleet agent + capability    | balanced                                | agent_report      |
| Prototype agent | normal fleet agent, budget-capped  | balanced                                | agent_report      |
| Crit agent      | wake-dispatched, `crit` gate       | high, never the builder                 | agent_report      |
| Case-study gen  | gateway, `call_site=casestudy`     | balanced                                | gateway/exact     |
| Measure probe   | scheduled, `call_site=measure`     | small                                   | gateway/exact     |

Every role's spend lands in `llm_spend` with its own `call_site` — meaning **Tally can price each
ritual**: "the POV cost $1.80; the crit program cost $22 this engagement." The creative firm's
overhead is itself cost-accounted. That's a pitch slide by itself.

## 4. Sequencing — four releases, each independently valuable

**R1 · Substrate + the front of funnel (~3 wks)**
Artifact store → deck engine → POV (ritual 1) → Concepts (ritual 2).
*Exit demo:* transcript in → POV discussed/approved → three territories → chosen Big Idea becomes
an approved breakdown in the shipped pipe. The "taste" story is real end-to-end.

**R2 · Craft (~3 wks)**
Design-system deliverable (3) → Prototype gate (4) → Crit gate (5).
*Exit demo:* a build where the fleet is capability-pinned to system v3, the prototype gated the
spend, and a must-fix crit note held closure until fixed. "Feels expensive" is enforceable.

**R3 · Client ritual (~2 wks)**
Playbacks + client portal accept (6) → Case study generator (7).
*Exit demo:* milestone PASS → playback link → client accept recorded as a verified outcome →
engagement close → case study with ledger-bound numbers.

**R4 · The loop (~1.5 wks)**
KPI observations + watchdog + Measure surface (8).
*Exit demo:* a lagging KPI pages Supervise with a drafted optimize deliverable; approving it
starts engagement N+1. The firm renews itself.

Total: ~9–10 focused weeks, no step blocked on a rewrite, every release demoable on the
dogfood fixtures (✅ `deliverable_dogfood_fixtures.py` seeds cross-board deliverables today —
each release adds its fixtures there so QA and demos are one command).

## 5. Success metrics (we eat our own ritual 8)

Seed these as KPIs on the Switchboard project itself:
- **Time-to-approved-scope** (intake → approved breakdown): target < 1 day with rituals 1–2 on.
- **Crit catch rate**: must-fix notes per shipped milestone that CI did *not* catch — proves the
  taste gate earns its cost. Target ≥ 1 early; falling over time as taste notes compound.
- **Prototype kill rate**: % of directions changed at prototype gate. If 0%, the gate is theater
  — revisit. Healthy: 15–30%.
- **Playback acceptance latency**: closure PASS → client accept. Target < 48h.
- **Case-study reuse**: case studies attached to new-engagement intakes.
- **Ritual overhead**: Σ ritual `call_site` spend / engagement contract value. Target < 1%.

## 6. Open questions (decide during R1)

1. **SOW/commercial edge** — acceptance events are recorded now; when do we un-park invoicing
   (P0-SPEC)? Proposal: after R3, once playback-accept is proven with a design partner.
2. **Public showcase hosting** — static publish under plan.taikunai.com vs. a separate
   white-label domain per client. Leaning: single showcase, per-client redaction.
3. **Taste notes scope** — per-operator vs. per-workspace. Leaning: workspace-global with an
   author field; revisit at multi-tenant.
4. **Crit on non-UI work** — the crit vocabulary generalizes (API ergonomics, naming, docs tone).
   R2 ships UI-crit only; a `crit_profile` field on the gate leaves the door open.
5. **Territory count** — fixed 3, or budget-scaled N? Start fixed; the fan-out job takes N.

---

*The delivery floor is shipped. These eight rituals are thin, honest layers on it — each one an
artifact, an agent, an attention item, and a render. Taste in front, proof in the middle, growth
at the end. That's the whole firm, in the box.*
