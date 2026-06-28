# Taikun Plan — MCP server

plan.taikunai.com is an **MCP server** (Streamable HTTP). Connect from Claude Code,
Claude Desktop, Cursor, or any MCP client and drive the plan without opening the board.

**Endpoint:** `https://plan.taikunai.com/mcp`

## Tools

Every task/board tool accepts `project`. Use `maxwell` for the TEEP Barnett plan, `helm` for
the marine chartplotter board, and `switchboard` for the live dogfood board that coordinates this
agent-collaboration product itself.

Reads (open):
- `list_projects()` — list routable boards.
- `prepare_agent_session(runtime, agent_id?, project?, task_id?, lane?)` — boot-time project
  resolver. It validates or infers the selected project and returns a project-bound startup prompt,
  first calls, and a board-derived `project_contract`.
- `get_project_contract(project, lane?, task_id?)` — project-agnostic lane/task contract from the
  board: selected project, lane tasks, assigned task deliverable/exit criteria, dependency status,
  active agents, and the local-docs policy.
- `search_tasks(workstream?, status?, owner_person?, blocking?, query?)` — filter the live plan.
- `get_task(task_id)` — full detail (description, fields, recent activity).
- `board_summary()` — project + rollups + one line per task.
- `get_working_agreement(project)` — connect-time rules: definition of done, branch convention,
  merge strategy, canonical main SHA, protocol/profile envelope, and session-start sequence.
- `doc_search(query)` — cited snippets from the plan docs.
- `ask_plan(question)` — the **full plan-wide agent** as one tool: a reasoned, doc-grounded
  answer (with sources), and a proposed task change when relevant (NOT applied).

Writes (authenticated when `PM_AUTH_MODE=required`; audited as the authenticated actor):
- `create_task(workstream_id, title, ...)`
- `update_task(task_id, ...only the fields you pass...)`
- `add_comment(task_id, text)`
- `register_agent(...)`, `heartbeat(...)`, `list_active_agents(...)`
- `register_host(...)`, `heartbeat_host(...)`, `list_agent_hosts(...)`, `host_status(...)`
- `claim_resource(...)`, `release_resource(...)`, `send_agent_message(...)`, `ack_message(...)`
- `list_pending_acks(project, agent_id?)`, `list_monitors(project, status?, kind?)`,
  `sweep_monitors(project)`, `resolve_monitor(...)`, `cancel_monitor(...)`
- `request_wake(...)`, `list_wake_intents(...)`, `claim_wake(...)`, `complete_wake(...)`,
  `cancel_wake(...)`
- `claim_next(...)`, `complete_claim(...)`, `abandon_claim(...)`
- `report_usage(...)`, `record_outcome(...)`, `verify_outcome(...)`, `reject_outcome(...)`
- `create_kpi(...)`, `update_kpi_value(...)`, `link_outcome_to_kpi(...)`
- `get_task_tally(...)`, `get_kpi_tally(...)`
- `reconcile(project)` — provenance drift report; always flags board contradictions like
  naked `Done` without merge SHA or agent completion evidence, and when canonical main / GitHub
  config is available, checks recorded SHAs and PR state against git/GitHub.

Agent completion rule:
- `complete_claim(evidence)` moves the task to `In Review` and records branch/SHA/PR evidence.
- `complete_claim(evidence, final_status="Done")` marks the task `Done` when the agent has verified
  completion and supplied evidence. For code tasks, include branch/head SHA/PR or `merged_sha` when
  available.
- Agents should not use naked `update_task(status="Done")`; it records no evidence and reconcile
  treats it as suspect.
- Bootstrap repair: `jobs.py backfill_default_branch_provenance` can stamp legacy direct-to-default
  commits that already landed before PR-only flow was enforced. It is a system/reconcile action,
  not a normal agent completion path. Use `PM_BACKFILL_DRY_RUN=1` first to inspect candidates.

Project contract rule:
- At boot, agents should call `prepare_agent_session(...)` before registration and use the returned
  `selected_project` on every call.
- Agents should treat `project_contract` / `get_project_contract(...)` as the canonical lane/task
  contract for the selected board. Do not assume repo-local docs such as `docs/EPICS.md` describe
  the active project unless the selected project or task explicitly points there.
- This rule is what lets a Vulkan agent work from the Vulkan board while sitting in a checkout that
  also contains Helm-specific docs.

Dispatch rule:
- `claim_next(agent_id, lanes?, capabilities?, max_risk?, max_budget_usd?)` filters ready work
  by lane, dependency, active claim, declared required capabilities, risk, and budget.
- Successful claims include `dispatch_reason` with the score, factor breakdown, candidate count,
  required/matched capabilities, and skipped counts by constraint.
- `budget.status` and `recommendation.model_tier` are advisory guidance for the runtime; they
  should be surfaced to the model/operator before work starts.

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
- `register_agent` may include `protocol_json` / REST `protocol`; the response includes
  `protocol_compatibility`.

Runner kill rule:
- Runner kill is outside `IXP-core`. Only Switchboard-managed sessions with a
  `runner_session_id` may advertise `runner_kill=true`.
- Kill requests target the runner session, snapshot state first, write `runner.*` audit events,
  and do not silently mark work complete. See `docs/INTERRUPT-TIERS-SPEC.md`.

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
principals or set `PM_AUTH_TOKEN` during bootstrap:
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

## Ops

- Runs as its own process: `projectplanner-mcp.service` (uvicorn, `127.0.0.1:8111`).
  Caddy routes `/mcp*` → `:8111`, everything else → the web app (`:8110`).
- The coordination monitor sweep is host-owned: enable `projectplanner-monitors.timer` so
  `requires_ack` messages can time out and notify senders even if no Codex thread is awake.
- Shares the SQLite file (WAL) with the web app; reuses `store`/`rag`/`agent` in-process.
- Auth: reads may remain open; writes are bearer-authenticated when `PM_AUTH_MODE=required`.
  `PM_MCP_TOKEN` and `PM_AUTH_TOKEN` map to compatibility system principals until explicit
  per-agent principals are created.
- `PM_MCP_PUBLIC_HOST` (default `plan.taikunai.com`) is trusted by MCP's DNS-rebinding guard —
  set it if the public host changes, or you'll get HTTP 421.
