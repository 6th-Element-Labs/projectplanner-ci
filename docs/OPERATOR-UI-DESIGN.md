# Operator UI v1 — design reference for implementing agents

**Deliverable:** `switchboard-operator-ui` · **Milestone:** Operator UI v1
**Wireframes (human-viewable):** [design/operator-ui-wireframes.html](design/operator-ui-wireframes.html) — open locally in a browser; gray = existing chrome, pink dashed = new.
**This file** is the agent-facing distillation: for each UI task, where it lives, what controls it adds, and which existing MCP/REST it calls. Nothing here requires new substrate — every control maps to a tool that already exists.

## Ground rules (all tasks)

- **Style:** match the existing app — Tabler components + `static/taikun-tabler.css` tokens. The wireframes show *placement and content*, not final styling. Reuse existing patterns: `card`, `datagrid`, `badge bg-*-lt`, status dots, the modal shells in `static/index.html`.
- **Data:** the SPA's fetch wrapper auto-appends `?project=` — new endpoints should accept it the same way. Prefer extending `store.py` + a thin `app.py` route over new services.
- **Permissions:** admin-gated screens check scopes exactly like `POST /api/projects` does (`write:projects` precedent, ACCESS-14/15). Destructive actions (runner kill, archive, revoke) get a typed/confirm step and an audited actor.
- **Truth discipline:** anything derived (narration, rationale) carries its stale flag; terminal provenance always wins (BUG-13/17, HARDEN-30 lessons).
- **Tests:** each task ships a `test_*.py` registered in `scripts/switchboard_ci.sh` (see `test_mission_page.py` for the API+`app.js`-needle pattern).

---

## UI-11 · Deliverable completion & history — *mission page*

- **Status control** on the mission header: dropdown walking `proposed → approved → in_progress → blocked → in_review → done → archived`. Confirm step for `done`/`archived`. Backend: `create_deliverable` upsert (status is already validated server-side).
- **Archive button (operator-requested):** an explicit first-class `Archive…` action beside the status control — not just a dropdown entry. Typed/confirm step; archiving drops the deliverable out of the Active filter into Archived; reversible by setting status back; audited. Same `create_deliverable` upsert with `status=archived`.
- **Picker upgrade:** status badge per deliverable + filter chips `Active / In review / Done / Archived` (data already in `list_deliverables`; the picker just ignores it today).
- Acceptance: mark a deliverable Done from the web; archive it via the explicit button + confirm; find it again under the Done/Archived filters.

## UI-1 · Author the deliverable graph — *mission page*

- **`+ Link task`** on the dependency-map card: search tasks (any board), pick milestone + role → `link_task_to_deliverable`. **Unlink** via node action.
- **`+ Milestone`**: title, acceptance criteria, sort → `add_deliverable_milestone`.
- **Node click actions:** set milestone, set role (`contributes/implementation/acceptance/foundation/parked`), unlink, open task.
- **Breakdown review card:** pending proposals with Approve / Defer / Reject → `approve_/defer_/reject_deliverable_breakdown`; plus a **Record outcome** header action → `submit_deliverable_outcome`.

## UI-2 · KPIs & outcomes — *mission page* · **depends on UI-12**

- **KPI tiles** beside economics: value + trend, `+ New KPI` tile → `create_kpi`, `update_kpi_value`, rollup via `get_kpi_tally`.
- **Outcomes-to-verify queue:** rows with Verify / Reject → `verify_outcome` / `reject_outcome`; link-to-KPI select → `link_outcome_to_kpi`.
- Dollar figures in these tiles only become real after UI-12 — hence the dependency.

## UI-3 · Work-session health — *board strip + mission + task Dev tab* · **realizes SESSION-8, unlocks UI-8**

- **Health strip** (board + mission header): `N sessions active · N dirty · merge gate: N blocked · leases held` → `list_session_health` (+ `list_active_resource_leases`).
- **Task panel** (Dev tab, next to the existing runner panel): sessions table — agent, branch, workspace path, state chips (`clean/dirty/expired`, tests ✓/missing) → `list_work_sessions(task_id)`, `get_work_session_health`.
- **Merge-gate verdict** in plain words with a Re-check button → `merge_gate`. Semantic colors (green/amber/red) are the point of this surface.

## UI-7 · Directed messaging & ack inbox — *task header + top bar*

- **Message button** on any live-agent chip → compose popover: text, `requires_ack` toggle, deadline select → `send_agent_message`.
- **Ack inbox** (top-bar bell + drawer): unacked list with age/deadline countdown and ack responses → `list_unacked_messages`, `list_pending_acks`, `get_message_status`; delivery state from the send response.

## UI-12 · Real cost in Economics — *backend-heavy; task + mission panels* · **unlocks UI-2; Bridge prereq P-1**

- Wire LiteLLM `success_callback` → `POST /tally/v1/spend/ingest` (idempotent by request id). Callers thread `task_id/claim_id/agent_id/source` via LiteLLM `metadata`; untagged calls still land as `source=gateway`.
- **UI change:** Economics panels (already rendered) show real `$` + token counts, a **source badge** per figure — `provider-actual` / `agent-reported` / `unattributed` — and a **model-mix** line.
- See `docs/BRIDGE-IMPLEMENTATION-PLAN.md` §2.8 for the full design.

## UI-8 · Fleet control — *new top-level “Fleet” tab* · **depends on UI-3**

- **Hosts table:** id, heartbeat age, capacity slots → `list_agent_hosts`, `host_status`; **Wake agent ▾** → `request_wake`.
- **Wake-intents queue:** queued/delivered with Cancel → `list_wake_intents`, `cancel_wake`.
- **Runners table:** uptime/health + Logs / Snapshot / **Kill (human-gated, typed confirm)** → `request_runner_logs/_snapshot/_kill`, `request_runner_health`.

## UI-4 · Settings → API keys

- Key table (name, scope chips, project binding, last-used, created-by) + Revoke → `list_scoped_tokens`, `revoke_scoped_token`.
- **Create modal:** name, scope checkboxes (mirror `ROLE_SCOPES` values), project binding, expiry → `create_scoped_token`.
- **Shown-once banner** for the raw token (storage is hash-only — never re-display). Admin-gated, audited.

## UI-5 · Members & access — *settings* · **depends on ACCESS-14 (done)**

- **Visibility explainer row** for private projects: owner ✓ · invited ✓ · org admins ✓ · org peers ✗ (state the ACCESS-14 rules on-page).
- **Members table:** role select (`viewer/commenter/contributor/operator/admin`) → `grant_project_role` / revoke; granted-by + audit trail visible.
- **Invite modal:** email + role; pairs with global-auth signup. Coordinates ACCESS-5/8 — don't fork their scope.

## UI-9 · Repo & provenance admin — *settings, admin-gated*

- **Repo topology card:** roles (`canonical/public_ci/public/release`) with authority chips; edit → `set_project_github_repo`, `set_project_repo_topology`.
- **Reconcile now** + findings list in plain words → `reconcile`, `reconcile_alerts`.
- **Verify offline completion:** task + evidence URL → `verify_offline_completion` (verifier-attributed).
- **Move task** (cross-project, admin 🔒, audited) → `move_task`.

## UI-13 · Multi-project intake + per-project corpus — *backend* · **unlocks UI-14**

- Domain→project routing map applied in `src/switchboard/integrations/inbox_routing.py`; **plus-addressing** `plan+<project>@taikunai.com` as zero-config routing; unmatched senders fall back to today's allowlist→maxwell (Maxwell unchanged).
- `project` column through `inbox_store` / `rag_store` / `intake` / `transcribe` (the standard `_conn(project)` pattern); migrate existing rows to maxwell; `doc_search`/`rag.search` take `project`.
- Acceptance: mapped-domain email lands in that project's Inbox tab; a transcript uploaded on X is searchable on X, invisible on Y.

## UI-14 · Communications settings — *settings* · **depends on UI-13**

- **Inbound card:** the project's plus-address (copy chip) + associated-domains table (add/remove) writing the UI-13 routing map.
- **Outbound card:** per-project digest/notify recipients (chips) + cadence + **Send test** (global `.env` list stays as fallback).
- Admin-gated; every change audited.

## UI-15 · New Project modal: repo field + guided webhook wiring

- **Repo field** (optional `owner/name`) in the existing New Project modal → threads to `create_project(github_repo=…)` (backend already accepts it).
- **Post-create “Wire your repo” panel:** copyable webhook URL **with the `?project=` pin pre-filled** (HARDEN-2 lesson), secret name, a `gh` one-liner, and a **Verify connection** button (reuses reconcile's reachability check; flips green on first delivery). Same panel reachable later from Settings for existing projects.
- **Non-goal:** auto-creating repos / auto-installing webhooks — that arrives with the Bridge GitHub App (`docs/BRIDGE-IMPLEMENTATION-PLAN.md` §3, G-1); this panel adopts it then.

## UI-17 · Browser Proof Console — *Mission deep link* · **depends on CO-12/13, COORD-34, UI-3, UI-8**

- **Deep link / toggle:** `?proof=1` or `?mode=proof` on the Deliverable Mission page (header **Proof console** button). Same Tabler canvas + `static/taikun-tabler.css` tokens — no second frontend.
- **Reuse only:** Mission header/timeline state, Fleet `runnerControlHtml` / Watch/Chat (`static/js/runner-session.js`), existing cards/`datagrid`/`badge bg-*-lt`/`table card-table`, Arm → `POST api/deliverables/{id}/coordinator_tick`.
- **Identity KV:** task_id, claim_id, Work Session, runner_session_id, host, provider identity ref (never secrets), source SHA, CLI, placement — redacted.
- **Provider rows:** Codex / Claude Code / Cursor with redacted auth + CO-14 MCP probe cells (`configured`…`cleanup`). Missing bind/MCP/cleanup/identity = **red** and blocks green proof.
- **Module:** `static/js/proof-console.js` (`SwitchboardProofConsole`), composed after Mission + Runner Session.

---

**Build frontier:** UI-3, UI-12, UI-13, UI-15 first (each unblocks others or is independently shippable); UI-2 waits on UI-12, UI-8 on UI-3, UI-14 on UI-13. UI-10 shipped (PR #213); UI-6 was an archived duplicate of UI-3. UI-17 is the browser-only acceptance surface for the session-terminal + coordinator dispatch dogfood.
