# Deliverable-First Agent Startup

Switchboard agents can boot from a **deliverable** or **mission** instead of only from
`project + lane + task_id`. Use this when the operator cares about a cross-board outcome such as
"Helm C++ + WebGPU Renderer" rather than a single workstream queue.

## Ownership

```text
Projects     → repo/trust/policy/access/CI/model/budget/Done authority
Boards       → execution boards and live outcome cockpits
Deliverables → what shipped value means: end_state, milestones, proof, cross-board links
Tasks        → execution units on exactly one project workstream
```

**Boards own execution. Deliverables own outcomes.**

- A task always lives in exactly one project database and one workstream.
- A deliverable lives in one **mission-home project** but may link tasks from any project through
  explicit `project_id + task_id` references.
- Writes always go to the named project. Cross-board coordination reads mission status; it never
  cross-pollinates project databases.

## Mission-home project

The deliverable record lives in one owning project (for example `helmrenderer` for the Helm renderer
mission). Pass that project as `project=` on:

- `prepare_agent_session`
- `get_mission_status`
- `get_deliverable`
- `claim_next(deliverable_id=...)` — idempotency and mission scope use the mission-home project

Linked tasks may live on other projects (`helm`, `vulkan`, …). When claiming or completing work
on a linked task, pass `project=<task_project>` to `claim_task`, `complete_claim`, and task writes.

## Boot sequence (all agents)

1. `prepare_agent_session(project=<mission_home>, deliverable_id=<id>, milestone_id=<optional>)`
2. `get_working_agreement(project=<mission_home>)` — includes `deliverable_first_startup`
3. `register_agent(...)`
4. Drain inbox: `list_unacked_messages`, `list_unblock_requests`
5. `get_mission_status(project=<mission_home>, deliverable_id=<id>)`
6. Read before editing:
   - `deliverable.end_state`
   - `acceptance_criteria`
   - `policy_constraints`
   - `milestones`
   - `linked_tasks` (note each `project_id`)
   - `blockers` and `next_actions`
7. Claim work (worker or coordinator — see below)
8. `complete_claim` with mission evidence when finished

`prepare_agent_session` returns `deliverable_scope: true`, `mission_context`, and `first_calls`
that include `get_mission_status` when a deliverable scope is set.

## Worker agent example

**Goal:** implement the next ready linked task for milestone `helm-cpp-webgpu-renderer:build-webgpu-ingest`.

```text
prepare_agent_session(
  runtime="cursor",
  agent_id="cursor/helm-webgpu-worker",
  project="helmrenderer",
  deliverable_id="helm-cpp-webgpu-renderer",
  milestone_id="helm-cpp-webgpu-renderer:build-webgpu-ingest",
)

get_mission_status(project="helmrenderer", deliverable_id="helm-cpp-webgpu-renderer")

claim_next(
  agent_id="cursor/helm-webgpu-worker",
  project="helmrenderer",
  deliverable_id="helm-cpp-webgpu-renderer",
  milestone_id="helm-cpp-webgpu-renderer:build-webgpu-ingest",
)
# → claims RENDER-1 on project=vulkan (task_project in response)

# Edit files in the vulkan checkout / scope for that task...

complete_claim(
  claim_id="taskclaim-...",
  project="vulkan",
  evidence='{"branch":"cursor/RENDER-1-ingest","head_sha":"abc123",'
           '"deliverable_id":"helm-cpp-webgpu-renderer",'
           '"mission_project":"helmrenderer",'
           '"milestone_id":"helm-cpp-webgpu-renderer:build-webgpu-ingest",'
           '"pr_url":"https://github.com/.../pull/123"}',
)
# → task moves to In Review; mission progress updates in_review_count (not Done-with-proof)
```

Rules for workers:

- Never call unscoped `claim_next(project="vulkan")` when assigned to a mission — use
  `deliverable_id` so dispatch stays inside linked tasks.
- Pass `mission_project` and `deliverable_id` in `complete_claim` evidence so mission progress
  refreshes.
- Done counts only with terminal merge/offline provenance — agent completion is In Review.

## Coordinator agent example

**Goal:** keep deliverable `switchboard-access-rollout` moving across boards without the human
naming each next task.

```text
prepare_agent_session(
  runtime="codex",
  agent_id="codex/access-coordinator",
  project="switchboard",
  deliverable_id="switchboard-access-rollout",
)

status = get_mission_status(project="switchboard", deliverable_id="switchboard-access-rollout")

for action in status["next_actions"]:
  # approve_breakdown → approve_deliverable_breakdown(proposal_id=...)
  # claim_task → claim_next(deliverable_id=..., project="switchboard")
  # verify_merge_provenance → wait for webhook/reconcile; do not mark Done manually
  # request_human_approval → send_agent_message to operator
  ...

update_mission_narrative(
  "switchboard-access-rollout",
  "Auth milestone is In Review; scoped MCP tokens are the current bottleneck.",
  project="switchboard",
)
```

Rules for coordinators:

- Prefer `run_mission_coordinator` or `next_actions` from `get_mission_status` over inventing tasks.
- Use `propose_deliverable_breakdown` / `approve_deliverable_breakdown` for new work — never
  create tasks without explicit project routing.
- Interrupt humans only for approval, architecture drift, budget/risk changes, or blocked decisions.
- Audit every dispatch with `deliverable_id` in claim and completion evidence.

## MCP tools reference

| Tool | Deliverable scope |
|------|-------------------|
| `prepare_agent_session` | `deliverable_id`, `board_id`, `mission_id`, `milestone_id` |
| `get_project_contract` | same — returns `mission_context` |
| `get_mission_status` | `deliverable_id` or `board_id`/`mission_id` |
| `claim_next` | `deliverable_id`, optional `milestone_id`; `project`=mission home |
| `complete_claim` | evidence: `deliverable_id`, `mission_project`, `milestone_id` |
| `run_mission_coordinator` | `deliverable_id`, `coordinator_agent_id`, `worker_agent_id`, optional `policy_json` |
| `get_working_agreement` | includes `deliverable_first_startup` and `session_start_sequence_deliverable` |

See also [`DELIVERABLES-MISSION-MODEL.md`](DELIVERABLES-MISSION-MODEL.md) and [`MCP.md`](MCP.md).
