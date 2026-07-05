# Taikun Plan — MCP server

plan.taikunai.com is an **MCP server** (Streamable HTTP). Connect from Claude Code,
Claude Desktop, Cursor, or any MCP client and drive the plan without opening the board.

**Endpoint:** `https://plan.taikunai.com/mcp`

Product naming note: this MCP surface is Switchboard. The deployed unit is still named
`projectplanner-mcp.service` during the compatibility phase; follow
[`SWITCHBOARD-RENAME-MIGRATION.md`](SWITCHBOARD-RENAME-MIGRATION.md) before renaming units,
paths, repo remotes, or env prefixes.

## Tools

Every task/board tool accepts `project`. Use `maxwell` for the TEEP Barnett plan, `helm` for
the marine chartplotter board, and `switchboard` for the live dogfood board that coordinates this
agent-collaboration product itself.

Reads (open):
- `list_projects()` — list routable boards.
- `prepare_agent_session(runtime, agent_id?, project?, task_id?, lane?, deliverable_id?,
  board_id?, mission_id?, milestone_id?)` — boot-time project resolver. It validates or infers
  the selected project and returns a project-bound startup prompt, first calls, and a project-level
  `project_contract`. When `deliverable_id` or `board_id`/`mission_id` is set, the session is
  deliverable-first: `first_calls` include `get_mission_status` and `project_contract` carries
  `mission_context`. See [`DELIVERABLE-FIRST-STARTUP.md`](DELIVERABLE-FIRST-STARTUP.md).
- `get_project_contract(project, lane?, task_id?, deliverable_id?, board_id?, mission_id?,
  milestone_id?)` — project-agnostic lane/task contract from the board.
  selected Project workspace: selected project, lane tasks, assigned task deliverable/exit criteria,
  dependency status, active agents, repo topology, and the local-docs policy.
- `search_tasks(workstream?, status?, owner_person?, blocking?, query?)` — filter the live plan.
- `get_task(task_id)` — full detail (description, fields, recent activity).
- `board_summary()` — project + rollups + one line per task.
- `control_plane_probe(project, lane?, include_heavy?)` — tiny read-only latency probe for MCP
  clients. Compare client wall time to `server_elapsed_ms`: a large gap means the excess is outside
  Switchboard's Python/SQLite path, such as TLS/network, MCP bridge dispatch, response framing,
  payload transfer, or client-side scheduling.
- `get_working_agreement(project)` — connect-time rules: definition of done, branch convention,
  merge strategy, canonical main SHA, protocol/profile envelope, and session-start sequence.
  It also publishes the `fail_fix_signal.v1` schema from
  [`FAIL-FIX-SIGNAL-SCHEMA.md`](FAIL-FIX-SIGNAL-SCHEMA.md).
- `doc_search(query)` — cited snippets from the plan docs.
- `ask_plan(question)` — the **full plan-wide agent** as one tool: a reasoned, doc-grounded
  answer (with sources), and a proposed task change when relevant (NOT applied).

Writes (authenticated when `PM_AUTH_MODE=required`; audited as the authenticated actor):
- `create_project(name, project_id?, label?, pretitle?, github_repo?, purpose?, boundary?, org_id?)`
  — create a routed Project workspace; pass `github_repo="owner/repo"` to wire the canonical GitHub PR
  provenance repo in the same step. Project DBs are physically separate, the canonical repo also
  appears in `repo_topology`, and the creator receives an explicit admin grant on the new project.
- `create_task(workstream_id, title, ...)`
- `update_task(task_id, ...only the fields you pass...)`
- `add_comment(task_id, text)`
- `submit_bug(source_task, observed_behavior, expected_behavior, repro_steps, evidence,
  severity_hint, affected_surface, source_agent?, failure_class?, duplicate_of?)`
- `move_task(task_id, project_from, project_to, reason?, new_task_id?, dependency_policy?)`
- `archive_task(task_id, project, reason?)`
- `register_agent(...)`, `heartbeat(...)`, `list_active_agents(...)`
- `register_host(...)`, `heartbeat_host(...)`, `list_agent_hosts(...)`, `host_status(...)`
- `register_runner_session(...)`, `list_runner_sessions(...)`, `request_runner_snapshot(...)`,
  `request_runner_kill(...)`, `list_runner_control_requests(...)`, `claim_runner_control(...)`,
  `complete_runner_control(...)`
- `claim_resource(...)`, `release_resource(...)`, `send_agent_message(...)`, `ack_message(...)`
- `list_pending_acks(project, agent_id?)`, `list_monitors(project, status?, kind?)`,
  `sweep_monitors(project)`, `resolve_monitor(...)`, `cancel_monitor(...)`
- `reconcile_alerts(project, alert_to?, min_severity?)`
- `request_wake(...)`, `list_wake_intents(...)`, `claim_wake(...)`, `complete_wake(...)`,
  `cancel_wake(...)`
- `claim_next(...)`, `complete_claim(...)`, `abandon_claim(...)`, `revoke_claim(...)`
- `report_usage(...)`, `record_outcome(...)`, `verify_outcome(...)`, `reject_outcome(...)`
- `create_kpi(...)`, `update_kpi_value(...)`, `link_outcome_to_kpi(...)`
- `get_task_tally(...)`, `get_kpi_tally(...)`, `get_deliverable_tally(...)`
- `reconcile(project)` — provenance drift report; always flags board contradictions like
  naked `Done` without merge/default-branch SHA, and when canonical main / GitHub config is
  available, checks recorded SHAs and PR state against git/GitHub. It also reports expired active
  task claims and unreleased resource/file leases as stale claims. Public mirror publication drift
  is reported as `publish_drift_stale_public_mirror`, not as merge drift.
- `set_project_github_repo(project, repo)` — update the repo binding later if a board was created
  before the repository existed or the repo moved. This updates `repo_topology.roles.canonical`.
- `set_project_repo_topology(project, canonical_repo?, public_ci_repo?, public_repo?,
  release_repo?, topology_type?, canonical_default_branch?, public_ci_required_status_contexts?,
  public_ci_sync_scripts?, public_publish_scripts?, release_publish_scripts?)` — configure the
  first-class repo roles for a Project, not a board/mission/deliverable. `canonical` is the only
  code-truth / Done authority.
  `public_ci` is a shared public CI sandbox for verification evidence only; `public` and `release`
  are publication/release evidence roles only. Legacy `ci_*` arguments are accepted as aliases for
  `public_ci_*`.
- `record_publication_evidence(project, source_project?, source_sha, public_repo?, public_ref,
  public_sha?, public_tag?, script?, guard_status?, guard_json?, artifact_url?, task_id?, claim_id?,
  agent_id?)` — record evidence that a canonical source SHA was published to a public mirror/release
  ref. `public_repo` defaults from `repo_topology.roles.public.repo`; `script` defaults from
  `repo_topology.roles.public.publish_scripts`. This evidence can satisfy publish/release gates such
  as `publication_evidence`, but it is evidence-only and cannot satisfy code `Done`.
- `list_publication_evidence(project, task_id?, source_project?, source_sha?, public_repo?)` — list
  recorded public mirror publication proof.
- `create_project_board(title, project, board_id?, mission_id?, kind?, status?, purpose?,
  end_state?, description?, owner_org?, owner_person_or_role?, metadata_json?)` — create a
  first-class Board/Mission child under a Project. Project remains the repo/trust/policy/access/CI/
  model/budget/Done boundary; Boards/Missions are live outcome cockpits.
- `get_project_board(board_id, project)` and `list_project_boards(project, kind?, status?)` —
  discover Board/Mission children for a Project.
- `create_deliverable(title, project, deliverable_id?, board_id?, mission_id?, status?,
  owner_org?, owner_person_or_role?, end_state?, why_it_matters?, confidence?,
  acceptance_criteria?, policy_constraints_json?, proof_requirements_json?, kpi_links?,
  metadata_json?)` — create a deliverable, optionally attached to an existing Board/Mission.
  Unknown `board_id` / `mission_id` fails closed.
- `get_deliverable(deliverable_id, project)`, `list_deliverables(project, board_id?)`,
  `add_deliverable_milestone(...)`, and `link_task_to_deliverable(...)` — build the cross-epic and
  cross-board mission rollup. Linked tasks are validated by explicit `task_project + task_id` and
  are not moved or mutated.
- `create_board(...)`, `create_mission(...)` — aliases for `create_project_board` with
  `kind=board` or `kind=mission`.
- `unlink_task_from_deliverable(...)`, `get_mission_status(...)`, `mission_status(...)` —
  remove cross-project links and return the mission cockpit rollup (end state, milestones, linked
  tasks, proof, blockers, active work, next actions).
- `update_mission_narrative(...)`, `propose_deliverable_breakdown(...)`, and
  `approve_deliverable_breakdown(...)` — store operator narrative and draft milestone/task
  breakdowns without creating tasks until approval.
- `submit_deliverable_outcome(...)`, `get_deliverable_breakdown_proposal(...)`,
  `list_deliverable_breakdown_proposals(...)`, `update_deliverable_breakdown_proposal(...)`,
  `reject_deliverable_breakdown(...)`, and `defer_deliverable_breakdown(...)` — coordinator
  outcome intake, human editing, and audited reject/defer before materialization.

Agent completion rule:
- `complete_claim(evidence)` moves the task to `In Review` and records branch/SHA/PR evidence.
- `Done` is reserved for GitHub/default-branch provenance: merged/rebased into the intended branch
  with `merged_sha` or equivalent recorded by webhook/reconcile.
- If an agent passes `final_status="Done"`, Switchboard records the attempt, releases the claim,
  and keeps the task `In Review` with a `done_requires_merge_provenance` warning.
- Naked `update_task(status="Done")` fails closed unless the task already has merge/default-branch
  provenance; hook-capable adapters deny the call before it reaches the server.
- Bootstrap repair: `jobs.py backfill_default_branch_provenance` can stamp legacy direct-to-default
  commits that already landed before PR-only flow was enforced. It is a system/reconcile action,
  not a normal agent completion path. Use `PM_BACKFILL_DRY_RUN=1` first to inspect candidates.

Safe merge rule:
- Agents may merge only when their control registration, task instructions, or the human operator
  explicitly allow it.
- Before merging, fetch origin, rebase/merge the task branch onto the intended target branch,
  resolve conflicts intentionally, rerun relevant tests, verify a clean committed branch, and push.
- Merge through GitHub or the configured merge queue only when checks/review are green.
- After merge, fetch the target branch, verify the content landed, record `merged_sha`, and rely on
  webhook/reconcile/backfill to mark `Done`.

Scheduled reconcile alert rule:
- `jobs.py reconcile_alerts` runs `reconcile` and emits a directed `reconcile_alert` IXP message
  when findings at or above `PM_RECON_ALERT_MIN_SEVERITY` exist.
- The job defaults to `PM_RECON_ALERT_PROJECTS=all` so GitHub merge provenance is hydrated for
  Helm, Vulkan, Switchboard, and dynamic boards even when a repo webhook is missing or delayed.
  Set a comma-separated list such as `switchboard` only when deliberately narrowing the timer.
  Unknown project ids fail closed.
- Alerts are deduped by project, severity floor, alert recipient, finding signature, and
  `PM_RECON_ALERT_DEDUPE_SECONDS` bucket, so unresolved drift does not spam every timer tick.
- Production runs this through `projectplanner-reconcile.timer`; agents/operators can trigger the
  same path with `reconcile_alerts(project, alert_to?, min_severity?)`.

Project contract rule:
- At boot, agents should call `prepare_agent_session(...)` before registration and use the returned
  `selected_project` on every call.
- For cross-board outcomes, boot with `deliverable_id` or `board_id`/`mission_id` on
  `prepare_agent_session` and read `get_mission_status` before editing. Boards own execution;
  deliverables own outcomes. See [`DELIVERABLE-FIRST-STARTUP.md`](DELIVERABLE-FIRST-STARTUP.md).
- Agents should treat `project_contract` / `get_project_contract(...)` as the canonical lane/task
  contract for the selected board. Do not assume repo-local docs such as `docs/EPICS.md` describe
  the active project unless the selected project or task explicitly points there.
- This rule is what lets a Vulkan agent work from the Vulkan board while sitting in a checkout that
  also contains Helm-specific docs.
- If work lands on the wrong board, use `move_task` or `archive_task` rather than direct DB cleanup.
  `move_task` fails closed on unknown projects, active claims/leases, destination id conflicts, and
  dangling destination dependencies unless `dependency_policy="clear"` is explicitly chosen.
- Project discovery and session bootstrap include each project's purpose/boundary text. Treat that
  as the project ownership contract: `claim_next` and all writes are scoped to the selected
  `project`, never global.

Dispatch rule:
- `claim_next(agent_id, lanes?, capabilities?, max_risk?, max_budget_usd?)` filters ready work
  by lane, dependency, active claim, declared required capabilities, risk, and budget.
- Successful claims include `dispatch_reason` with the score, factor breakdown, candidate count,
  required/matched capabilities, and skipped counts by constraint.
- `budget.status` and `recommendation.model_tier` are advisory guidance for the runtime; they
  should be surfaced to the model/operator before work starts.
- `revoke_claim(claim_id, reason, reassign_to?, sort_order?, partial_evidence?)` is the
  operator override path. It releases the active claim, requeues the task, optionally redirects or
  reprioritizes it, preserves partial evidence, and sends the displaced agent an ack-required
  `claim_revoked` stop message. After revoke, the old claim cannot complete the task.

Bug intake rule:
- `submit_bug(...)` is the supported agent-facing path for filing discovered bugs.
- It requires `write:bug_intake`, not generic `write:tasks`.
- A complete submission creates one `BUG` task in `Triage` with structured `bug_report` state,
  source task/agent linkage, evidence payload, severity hint, affected surface, and optional
  failure class / duplicate link.
- `failure_class`, when present, must be one of the `fail_fix_signal.v1` classes. Unknown classes
  fail closed and return the schema instead of creating a BUG task.
- It never creates implementation work, marks work Ready, claims work, wakes an agent, or bypasses
  the human gate.
- REST parity lives at `POST /ixp/v1/bugs/submit`.

Durable ack rule:
- `send_agent_message(... requires_ack=true ...)` creates a durable `ack_deadline` monitor.
- `send_agent_message` accepts `ack_deadline_minutes` and the versioned aliases
  `ack_timeout_seconds` / `ack_timeout_s`; all produce the same persisted deadline.
- `list_pending_acks` and `get_message_status` expose monitor state; `sweep_monitors` resolves
  acked messages and fires timed-out monitors. The production host should run
  `jobs.py sweep_monitors` through `projectplanner-monitors.timer`.
- By default, a fired ack monitor only notifies the sender. Callers may opt into
  `on_ack_timeout="wake_target"` or `on_ack_timeout="wake_or_operator_alert"` to create a
  durable wake intent for an eligible Agent Host. Host registration and wake intent semantics
  are specified in [`docs/AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md).
- Ack timeouts are typed as `unreachable_agent`, and visible delivery fallback comments carry the
  same failure class so QA and operators do not mistake mailbox storage for delivery.

Agent Host wake rule:
- `register_host` advertises always-on host inventory, runtimes, lanes, capabilities, capacity,
  and TTL.
- `request_wake` creates a durable wake intent; `claim_wake` atomically assigns it to one
  eligible host; `complete_wake` records start/failure evidence.
- `list_wake_intents` distinguishes `pending`, `claimed`, `completed`, `failed`, and
  `cancelled` wakes. A host daemon should poll pending wakes and launch/reuse a supervised
  runtime.

Tally outcome/KPI rule:
- `report_usage` records gateway-measured or agent-reported spend. Spend can attach to a
  `task_id`, `claim_id`, or `outcome_id`; outcome-attached spend resolves back to the outcome's
  owning task for task-level rollups.
- `record_outcome` creates pending value. `verify_outcome` moves it into the denominator;
  `reject_outcome` keeps the record auditable but excluded.
- `create_kpi` and `link_outcome_to_kpi` map verified outcomes to business movement. Task and
  KPI tallies report cost per verified outcome and, when contribution is numeric, cost per KPI
  contribution unit.

Protocol compatibility:
- `get_working_agreement` includes `protocol.version`, `protocol.profile`,
  `protocol.profiles`, `protocol.compatible_versions`, and `protocol.field_aliases`.
- `get_working_agreement` and `get_project_contract` include `repo_topology` and
  `code_repo_gate`. `repo_topology.scope` is `project`; Switchboard `project` ids are the top-level
  authority boundary, while `project_boards` / Board/Mission ids are first-class outcome cockpits
  under that Project. Agents should treat `repo_topology.roles.canonical` as the only repo that can
  prove code Done, `repo_topology.roles.public_ci` as shared verification evidence only, and
  `repo_topology.roles.public` as public mirror publication evidence only.
- `register_agent` may include `protocol_json` / REST `protocol`; the response includes
  `protocol_compatibility`.

Runner kill rule:
- Runner kill is outside `IXP-core`. Only Switchboard-managed sessions with a
  `runner_session_id`, owning `host_id`, and `managed_process=true` may advertise
  `runner_kill=true`. Unmanaged sessions are still visible, but their kill action is stripped.
- Operators request snapshot/kill through runner-control requests. The owning Agent Host claims
  the request, executes the local supervisor action, completes the request with the captured
  snapshot/result, and writes `runner.*` audit events.
- Kill/restart requests snapshot state first and never mark work complete. See
  `docs/INTERRUPT-TIERS-SPEC.md`.

## Connect

**Claude Code (CLI):**
```bash
claude mcp add --transport http taikun-plan https://plan.taikunai.com/mcp
```

**Claude Desktop / Cursor (JSON config):**
```json
{
  "mcpServers": {
    "taikun-plan": { "url": "https://plan.taikunai.com/mcp" }
  }
}
```

If writes are token-gated (`PM_AUTH_MODE=required`), add a bearer token header. Existing
deployments can keep using `PM_MCP_TOKEN`; new deployments should create per-agent
principals through `create_scoped_token` / `/api/access/tokens`, or set `PM_AUTH_TOKEN`
during bootstrap:
```json
{
  "mcpServers": {
    "taikun-plan": {
      "url": "https://plan.taikunai.com/mcp",
      "headers": { "Authorization": "Bearer <SWITCHBOARD_TOKEN>" }
    }
  }
}
```

Admin MCP credential tools:

- `create_scoped_token(project, kind, display_name, role?, scopes?, principal_id?)`
  - requires `write:system` on the target project and returns the raw token once.
- `list_scoped_tokens(project, include_revoked?, kind?)`
  - requires `write:system`; never returns raw tokens or token hashes.
- `revoke_scoped_token(project, principal_id)`
  - requires `write:system`; revokes the principal and its live sessions.

## Ops

- Runs as its own process: `projectplanner-mcp.service` (uvicorn, `127.0.0.1:8111`).
  Caddy routes `/mcp*` → `:8111`, everything else → the web app (`:8110`).
- Production Caddy enables `zstd`/`gzip` compression for web/API responses, while leaving `/mcp*`
  uncompressed to avoid changing MCP stream framing. Full board snapshots can be 100KB+ JSON; clients
  should still prefer `get_lane_delta` or `control_plane_probe` for polling and diagnostics.
- HTTP responses include `Server-Timing: app;dur=...` and `X-Switchboard-Server-Ms` so operators can
  separate application time from public network, TLS, transfer, and MCP/client bridge overhead.
- `projectplanner-mcp.service` is a compatibility unit name. Future `switchboard-mcp.service`
  support should be introduced as an alias first, not as an in-place replacement.
- The coordination monitor sweep is host-owned: enable `projectplanner-monitors.timer` so
  `requires_ack` messages can time out and notify senders even if no Codex thread is awake.
- Shares the SQLite file (WAL) with the web app; reuses `store`/`rag`/`agent` in-process.
- Auth: reads may remain open; writes are bearer-authenticated when `PM_AUTH_MODE=required`.
  `PM_MCP_TOKEN` and `PM_AUTH_TOKEN` map to compatibility system principals until explicit
  per-agent principals are created.
- `PM_MCP_PUBLIC_HOST` (default `plan.taikunai.com`) is trusted by MCP's DNS-rebinding guard —
  set it if the public host changes, or you'll get HTTP 421.
