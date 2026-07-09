# PRD — Switchboard as the execution layer *under* GitHub, Jira, and Linear

**Status:** Draft · **Workstream (proposed):** `BRIDGE` · **Owner:** Switchboard Operator · **Task:** DOGFOOD-13
**Provenance:** Recovered and completed from a strategy session on 2026-07-08→09 that stalled mid-write when the connection dropped. This document carries that session's thesis forward and adds the per-platform technical implementation scope the discussion had reached but not yet written.
**Implementation plan:** the detailed engineering scope (shared substrate, per-platform work items, effort, acceptance, milestones) lives in [BRIDGE-IMPLEMENTATION-PLAN.md](BRIDGE-IMPLEMENTATION-PLAN.md) (BRIDGE-1).

---

## 1. Thesis

Switchboard is not a project tracker. It is the **agent-execution layer** that sits between *whatever board the humans already use* and *the fleet of agents doing the work*, and it does the four things a tracker structurally will not: **dispatch, isolate, prove, and cost.**

The human keeps living in Linear / Jira / GitHub. An issue assigned to an agent quietly gets picked up, worked in an isolated worktree, and comes back to *their* board with a real, merge-proven status, a plain-English update, and a dollar figure — none of which their tracker can produce on its own.

This is deliberately **not** "kill our board." Our control plane is already UI-agnostic — `app.js` talks to the same REST/MCP endpoints the agents do — so our board is just *one client* of the substrate. The strategy is to **add clients, not remove one**.

### Why this is not a fantasy pivot — we've already built the pattern twice

This is the *third* instance of a pattern we already ship in production:

1. **Runtime adapters (executor side).** `switchboard_core` already normalizes Claude Code / Codex / Cursor / LangGraph / raw API loops to one protocol (IXP/TXP/OXP). We know how to make heterogeneous *executors* look uniform.
2. **GitHub provenance (source side).** Our webhook + `reconcile` + `repo_topology` system is *literally already "the layer under GitHub"* — it watches an external system and stamps Done only on real merge provenance.
3. **Tracker adapters (the new side).** A Linear adapter, a Jira adapter, a GitHub-Projects adapter — each normalizing that tracker's issues to our task model, exactly like the runtime adapters normalize executors.

So "be the layer under Linear" = **generalize two adapter patterns we already have**. IXP/TXP/OXP was designed substrate-agnostic for precisely this. We are not inventing; we are pointing existing machinery at a new socket.

---

## 2. The unifying model: one execution layer, six verbs, many surfaces

Every integration is the same six verbs, pointed at a different host tracker:

| Verb | What it does | Machinery we already run |
|---|---|---|
| **Mirror in** | host issue ↔ our task (external-id mapped) | webhook ingest + `create_task` / `update_task` |
| **Dispatch out** | assigned-to-fleet → the right agent/runtime claims it | `claim_next`, capability/budget scoring |
| **Isolate** | worktrees + file/resource **leases** so parallel agents don't collide | SESSION-7/11, leases |
| **Gate** | status cannot say Done until proof exists | merge webhook + `reconcile`, `merge_gate`, executed-test evidence |
| **Price** | every issue gets $ + tokens + model | `llm_spend` ledger (once UI-12 wires it) |
| **Narrate** | plain-English progress posted where humans read | the NARRATE timer, pointed at a new destination |

The leases / worktrees / enforcement are the part the human never sees and the tracker cannot do — they are *why* five agents can work one repo through Linear without clobbering each other.

---

## 3. The hard problem, already solved: the surface-topology contract

The design problem that kills most sync products is **who wins on conflict**. We already solved it in production for GitHub: **field-level authority** via the `repo_topology` pattern (`done` authority = canonical repo; evidence roles can't stamp Done).

We generalize that to a **surface topology** — a declared, per-field, fail-closed authority map across every connected surface:

| Field | Authority | Rendered where |
|---|---|---|
| title, description | host tracker | native |
| priority, assignee, labels | host tracker | native |
| **status = Done** | **Switchboard** (only on merge/offline proof) | written back to host |
| **cost ($ / tokens / model)** | **Switchboard** | written back as field/comment |
| **evidence** (tests, hygiene, audit) | **Switchboard** | attached to host |
| **narration** | **Switchboard** (authored) | rendered in host UI |
| worktree isolation, leases, work-session/merge gating | **Switchboard, internal only** | never leaves our layer |

Every adapter ships with a **reconcile sweep as a backstop, not just webhooks** — the orphan-merge sweeps (HARDEN-2/9/19) taught us in prod that webhooks lie. This is the single most important, most reusable piece of the whole strategy, and it should be written first (see §7).

---

## 4. Per-platform scope — mechanics, use cases, "2+2=5", so-what

Ordered by build sequence. "2+2=5" = the value that exists only because we sit under the host, that neither we nor the host can produce alone.

### 4.1 GitHub Issues / Projects — the proof surface (do first)

**Mechanics.** GitHub App. We already run the webhook + reconcile half in production. Add: Issues/Projects API for mirror-in, labels as dispatch triggers, and — the gem — **Check Runs**: post `Switchboard: tests executed ✓ (hashed log) · session clean · cost $0.61` *directly on the PR*, beside CI.

**Technical implementation.**
- GitHub App (not a PAT): `issues`, `pull_requests`, `checks`, `projects` scopes; per-install token.
- Ingest: `issues.*`, `issue_comment.*`, `projects_v2_item.*` webhooks → adapter → `create_task`/`update_task`, storing `external_id = gh:{repo}#{number}` on the task.
- Dispatch trigger: a configurable label (`agent:ok`) → intake scorer comments a feasibility/cost estimate → one-click (or auto) `claim_next`.
- Write-back: Check Run on the head SHA carrying executed-test evidence hash, session-hygiene verdict, and cost; issue comment for narration; issue closes on merge provenance via the existing reconcile path.
- Backstop: extend the existing reconcile sweep to reconcile issue↔task status, not just merge↔task.

**Use cases.**
1. **Label-to-fleet.** Maintainer labels an issue `agent:ok`; scorer comments an estimate ("agent-doable, ~$4–9, confidence B"); one click → dispatched → agent works in a managed worktree → PR opens referencing the issue → our check run carries the evidence → merge → the issue closes *itself*, with provenance. Every link in that chain exists today.
2. **Fleet-safe monorepo.** Six agents, one repo, zero collisions — leases and worktrees are why (we lived the collision this session; SESSION-7 is the fix). GitHub just sees clean parallel PRs.
3. **Nightly backlog burn.** Cron fleet walks `good-first-issue` in dependency order; morning digest lists PRs opened and total spend.

**2+2=5.** GitHub owns the *proof event* (the merge). Copilot does the coding. **Nobody does coordination + receipts.** We turn their merge event into work-truth and make multi-agent-on-one-repo safe.

**So what.** Cheapest adapter (half-built), dev-native buyer, and the demo surface — "watch this issue close itself with a receipt."

### 4.2 Linear — the trust upgrade (do second)

**Mechanics.** Linear's agent API + MCP + webhooks. Agents are assignable; we write comments, statuses, and project updates.

**Technical implementation.**
- OAuth app + webhooks (`Issue`, `Comment`, `IssueLabel`), agent actor identity so our writes render as an assignable teammate.
- Map Linear workflow states ↔ our task status; `external_id = linear:{teamKey}-{number}`.
- Point the NARRATE timer at Linear's project-update API (their weekly update = our CEO-voice narrator output).
- Cost/evidence written as issue comments + custom attributes.

**Use cases.**
1. **Agent as teammate.** Assign an issue to the fleet. It moves In Progress with "claude-code/opus, claimed 14:02" (our map-hover data, rendered in their UI). It shows Done only when our provenance stamps it. The team never leaves Linear.
2. **Cycle economics.** End of cycle: "31 issues shipped, 14 by agents, $212, 2 human escalations" — cost-per-issue in the cycle review. Their velocity meets our ledger.
3. **Auto project updates.** Their weekly project updates draft themselves from provenance, not vibes — the narrator already writes exactly that text.
4. **Trustworthy triage.** New bug → our scorer comments feasibility/cost → one-click dispatch.

**2+2=5.** Linear's agent story accepts agent *activity* but trusts self-report — no proof-of-done, no cost governance, no cross-agent safety. Their polish becomes our free frontend; our enforcement makes their agent story *true*.

**So what.** The design-partner sweet spot — modern, agent-curious teams, small enough to move fast. Their per-seat model doesn't monetize agents; our metered layer does. Non-competing revenue.

### 4.3 Jira — the compliance jackpot (do third; it's where the money is)

**Mechanics.** Forge/Connect app, webhooks, custom fields + attachments for evidence, JSM for incidents.

**Technical implementation.**
- Forge app (Atlassian-hosted) or Connect (self-hosted) — Forge preferred for enterprise trust.
- Webhooks on issue create/update/transition → adapter; `external_id = jira:{PROJECT}-{n}`.
- Evidence as issue attachments + custom fields: executed-test hash, merge SHA, session-hygiene verdict, `get_audit_export` (HARDEN-13) bundle.
- Transition guard: our status write to "Done" is gated on proof; otherwise it posts evidence and holds.

**Use cases.**
1. **Change-management-grade agent work.** Every ticket an agent touches carries executed-test evidence (hashed logs), merge provenance, session-hygiene verdict, and a full audit trail. That bundle is what gets agent work through a CAB / SOX review. Today no enterprise can answer "prove the AI's change was tested" — we have the schema.
2. **Mass migration epics.** The 1,400-ticket framework migration: fan-out dispatch with leases across the monorepo, epic burndown + running cost, humans reviewing exceptions only.
3. **JSM incidents.** Incident → runbook agent → fix PR provenance-linked to the incident → post-incident doc auto-drafted by the narrator.
4. **Planned vs. actual.** Story points vs. real cost/time per ticket (the TALLY-7 concept) in their dashboards.

**2+2=5.** Jira brings process ceremony and enterprise budgets; we bring the evidence that makes the ceremony *true for agents*. Atlassian is pushing assistant-style AI; the compliance-grade execution layer is unclaimed ground.

**So what.** The enterprise buyer with real money and a hard requirement ("auditable AI changes") that maps 1:1 to what we built. Longest sales cycle, hardest API — hence third.

### 4.4 Slack / Teams — not a tracker; the human-loop accelerator

**Mechanics.** Bot + interactive buttons, mapped onto machinery we already have: `notify`, `requires_ack` + deadlines + escalation monitors, human-gates, `digest.py`. This is essentially ACCESS-11/13 already on our board.

**Use cases.**
1. **Approvals where humans live.** "Agent wants to merge PR #212 — Approve / Deny" as Slack buttons, with ack deadlines and escalation if ignored. Human-gate latency collapses from "next time someone opens a board" to "tap on phone."
2. **@mention intake.** "@Switchboard fix the flaky login test" → triaged → filed in the tracker of record via the same adapters → thread gets narration updates.
3. **Morning fleet digest.** What shipped, what's blocked, what it cost.
4. **Smart escalation.** A blocked agent DMs *the person who holds the conflicting lease* — we know who that is; nobody else does.

**2+2=5.** Our ack/monitor semantics are exactly Slack-shaped, and every other integration gets faster because the human loop lives here.

### 4.5 Asana / Monday / Notion / ClickUp — beyond code (later)

**Mechanics.** Their APIs + automations. Key difference: **no merge event**, so provenance uses our **offline-evidence path** (verifier-stamped artifacts — RECON-7/QA-4 machinery, already dogfooded).

**Use cases.** Content pipelines (brief → draft → verifier-stamped artifact), research agents filling Notion databases with cited pages, ops runbooks with evidence receipts.

**2+2=5.** These tools sell "AI features"; we deliver an accountable *workforce*. For us: TAM beyond engineering.

**So what.** Honest caveat — the proof story is structurally weaker without a merge event, so this tier waits until verifier-stamped evidence has earned trust in the code world first.

---

## 5. The emergent 2+2=5 — value that exists only *under several surfaces at once*

This is the real prize, and it is structural — it cannot be copied by any single host:

1. **The cross-surface dependency graph.** Enterprises run Jira + GitHub + Slack simultaneously. A Jira ticket blocked on a GitHub PR blocked on a Linear design issue — no tracker can see that chain, because each is an island. The layer underneath sees all of it. **No possible competitor among the hosts.**
2. **Portfolio economics.** One ledger across every surface: cost-per-outcome by team, tracker, model, quarter. The CFO view no tool has, because no tool sees all the work.
3. **One fleet, many fronts.** Agents don't care which tracker the work arrived from; capacity flows to the highest-priority work *across* tools, under one budget policy.
4. **The fleet cockpit — where our UI stops being a liability and becomes the point.** The cross-surface cockpit (every agent, lease, session, dollar, across Linear + Jira + GitHub) *cannot exist inside any single tracker*. Our UI's destiny isn't "worse Linear" — it's "the only pane of glass **above** all of them." UI-3/7/8/12 (sessions, messaging, fleet, cost) are cockpit — keep and sharpen. UI-1/2 (deliverable authoring, KPIs) increasingly render *into the host trackers* as enrichment.

---

## 6. Deployment modes — "we can have both," architecturally free

Three modes, one control plane, chosen per team, because the board was always just a client:

- **Native** — small teams; our board *is* the tracker.
- **Embedded** — enterprise keeps Jira/Linear; we're invisible substrate + the cockpit.
- **Hybrid** — some teams native, some embedded, one fleet and one ledger underneath.

---

## 7. What must be true first — sequenced rollout

1. **Make the claims real before rendering them into someone else's UI.** Spend ingestion (UI-12), enforcement defaults, backups (HARDEN-43). *An empty cost ledger posted into a customer's Jira is a credibility grenade.*
2. **Write the surface-topology contract** (generalize `repo_topology` to field-level authority across surfaces). Small doc, huge leverage — the design answer to sync hell.
3. **GitHub adapter MVP** — the wedge trio only: provenance-gate + cost + narration as check runs/comments.
4. **Slack approvals** (ACCESS-11) — makes everything feel alive.
5. **Linear**, then **Jira**.
6. **Publish the IXP spec** (PROTO-6, already on the board) so third parties can write adapters we don't — that's what makes this a *layer* rather than an integrations chore.

### Proposed `BRIDGE` workstream (first tasks)

| Task | Deliverable | Depends on |
|---|---|---|
| BRIDGE-1 | Surface-topology contract spec (field authority, fail-closed, reconcile-backstop) | repo_topology (done) |
| BRIDGE-2 | GitHub adapter MVP: check-run evidence + cost + narration on real PRs | BRIDGE-1, UI-12 |
| BRIDGE-3 | Slack approval buttons wired to human-gates + ack monitors | ACCESS-11 |
| BRIDGE-4 | Linear adapter (assignable agent, provenance-gated Done, auto project updates) | BRIDGE-1 |
| BRIDGE-5 | Jira Forge adapter (evidence bundle → CAB/SOX-grade audit trail) | BRIDGE-1, BRIDGE-2 |

---

## 8. Risks

- **Platform / ToS dependency.** Hedge by being multi-surface early; no single host can strand us.
- **Sync is genuinely hard.** Field-level authority + reconcile sweeps are our proven answer (webhooks lie; sweeps are the backstop).
- **Market-timing bet on fleets.** This strategy makes the bet *cheaper*, not *safer* — it reuses built machinery rather than adding a new one.

---

## 9. Open questions

- Native vs. Forge/Connect for Jira given enterprise trust vs. build cost.
- Whether GitHub Projects (v2) or plain Issues is the right first ingest surface for the MVP demo.
- How the cross-surface dependency graph is authored/visualized in the cockpit (extends the existing deliverable DAG).
- Pricing shape of the metered agent layer that sits under per-seat trackers without cannibalizing them.
