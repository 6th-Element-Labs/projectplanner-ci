# The Universal Workflow UI — how the planner generalizes

**Thesis.** `plan.taikunai.com` (Project Maxwell) is not a bespoke project-management app. It is one
**domain pack** rendered by a generic, workflow-driven shell — the same shell that should render *any*
Taikun workflow's surfaces. It already shares the brand theme (`taikun-tabler.css`) and the
"generic, workflow-driven" thesis with the **Agent Console**, the **Document Engine**, and the
**Universal Input Queue**. This doc maps the planner's screens onto those patterns and lays out the
generic UI so any workflow can adopt it.

## The planner is three patterns you've already built, in one shell

| Planner surface | Underlying pattern | Where it already exists |
|---|---|---|
| **Inbox** (email-triaged updates → confirm/dismiss) + **Ask Taikun → "drop a transcript/email → ingest & triage"** | **Universal Input Queue** — any signal normalized → agent-triaged → *auto-applied or routed to a human* → fully logged | the `universal-input-*` graphics; Agent Console queue; `intake.py` / `inbox.py` / `signals.py` |
| **Pulse** (weekly chief-of-staff digest: what changed / slipping / pick up next) + **Exec Summary** + **Excel / MS-Project export** | **Reports = Plans = workflow documents** — ordered typed sections rendered to a format; a plan is a report you can run | Document Engine (`SECTION_REGISTRY.md`); `digest.py` / `export.py` / `build_plan_artifacts.py` |
| **Board / My-work / Gantt / Milestones / Decisions / Risks** + **dispatch a task → a coding agent (Building… → PR ready)** | **Workflow runs + governed effects** — each task is a unit of work; *dispatch* is a governed action with a human gate, exactly like the Console's approve→dispatch | Agent Console PRD (`workflow_runs/steps/approvals/effects`); `dispatch.py` / `jobs.py` / `runner/` |
| **Ask Taikun** (RAG over plan docs · propose-to-confirm) | **The governed agent** — proposes, never silently mutates; every action is confirm-gated and logged | `agent.py` / `rag.py`; the LLM-governance gateway (allow-list, budget, audit ledger) |

So the planner is **Universal Queue (in) → Workflow Runs (act) → Reports/Plans (out)**, with a governed
agent threaded through — the same loop as every other Taikun surface, just scoped to a *project plan*
instead of a *well* or a *SIM fleet*.

## It's already "official Tabler" — that's the point

The app loads stock `@tabler/core` + `tabler-icons` and a single `taikun-tabler.css` that sets **only
Tabler's own `--tblr-*` properties** (brand red `#c0392b`, soft `#f5f6f8` canvas, hairline borders,
rounded radius, Inter type). No custom classes, no custom CSS. That is the *identical* strict-Tabler
contract our agent-console and document-engine wireframes follow — which is why a mock built this way
**translates 1:1 into the running app**. Elevating the UI to our premium mock tier therefore needs **zero
new CSS** — only better *composition* of documented utilities:

- `card border-0 shadow-sm rounded-3` → floating cards on the soft canvas
- `card-status-start` / `card-status-top` in a phase color → the kanban accent
- `avatar avatar-xs rounded-circle` + `avatar-list avatar-list-stacked` → owners/assignees
- `bg-{azure,purple,blue,orange,green,red}-lt text-*` → tinted phase / risk / status chips
- `table table-vcenter table-borderless`, `progress progress-sm`, `status-dot`, `badge-outline`, `btn-pill`, `empty`, `ribbon`

Premium comes from hierarchy, spacing, avatars, status color and float — not from bespoke CSS. The premium
mock variants (`TaikunWebsite/planner-mocks/`) are built under exactly these rules.

## The generic UI — one shell, any workflow

The planner = a **domain pack** (config), not code. The generic shell binds to the same engine records
the Agent Console PRD names; a pack only supplies labels, columns, sections, and the queue/report bindings.

| Generic concept | Planner pack | A well-advisor pack | A SIM/telco pack |
|---|---|---|---|
| **Scope** | a project plan | a fleet / a well | a SIM cohort / region |
| **Queue** (inbox) | inbound emails / transcripts → plan updates | alarms / events → triage | SIM-offline / anomaly events |
| **Run lanes** (board columns) | lifecycle phase (Kickoff…Operate) | triage → diagnose → plan → dispatch → measure | sense → triage → resolve |
| **Run card** | a task (owner, risk, blocking, deps, effort) | an opportunity / intervention | an incident |
| **Report / Plan** | Pulse digest · Exec summary · MS-Project export | well assessment · fleet summary | SIM health report |
| **Agent** | Ask Taikun (RAG over plan docs) | well advisor | network advisor |
| **Governed action** | dispatch task → coding agent | approve → dispatch field work | approve → carrier action |

Everything else — the page-header, KPI strip, nav, kanban, tables, the queue list-group, the report card,
the ask panel, the detail modal — is **shared chrome**, driven by the workflow's typed shape:
`workflow_runs / steps / approvals / effects` for the board, the Document-Engine `sections[]` for reports,
and the ingestion contract for the queue. Bind those once and any workflow gets this UI for free.

## Build plan (after the user picks a variant)
1. **Lock the visual** from the chosen premium variant (A board / B command-center / C my-work+queue).
2. **Componentize as strict-Tabler partials** — header+KPI strip, kanban column + run-card, the queue
   list-group (confirm/dismiss), the report card (Pulse), the ask panel, the detail modal — all `--tblr-*` only.
3. **Bind to the engine shape** — board ← `workflow_runs/steps`; queue ← ingestion items + `approvals`;
   reports ← Document-Engine `sections[]`; dispatch ← `effects` (governed, human-gated).
4. **Pack config** — scope label, lane set, card fields, section set, queue source. The plan ships as the
   first pack; well-advisor / telco follow as config, not forks.

Net: the planner becomes the reference instance of **the universal workflow console** — queue in, runs in
the middle, reports/plans out, a governed agent throughout — and any future workflow renders through the
same Tabler shell by supplying a pack.
