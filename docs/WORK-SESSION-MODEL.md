# Work Session Model

`switchboard.work_session.v1` is the first-class contract for code-producing agent work.
It binds an agent/task claim to the project authority boundary, repo role, branch, local
workspace, hygiene state, file/resource leases, and lifecycle status.

## Why It Exists

Switchboard already knows who claimed a task and which runner process is alive. That is not enough
to prevent agent git churn. Operators also need to know where the code is being changed, which repo
role controls Done, whether the workspace is dirty, and whether the branch/path match the task.

A Work Session is the missing middle object:

```text
task_claim -> work_session -> runner_session
     |             |                |
 who owns it   where code lives   what process is active
```

## Required Fields

- `project_id`: the Switchboard project that owns repo/trust/policy/Done authority.
- `agent_id`: stable runtime id such as `codex/SESSION-1-work-session-model`.
- `repo_role`: one of `repo_topology.roles`, usually `canonical`.
- `storage_mode`: `worktree`, `clone`, or `external`.
- `status`: `proposed`, `active`, `blocked`, `completed`, `archived`, or `expired`.
- `dirty_status`: `clean`, `dirty`, or `unknown`.
- `worktree_path` when `storage_mode=worktree`.
- `clone_path` when `storage_mode=clone`.

## Optional Proof Fields

- `task_id`, `claim_id`
- `repo`, `default_branch`, `branch`, `upstream`
- `base_sha`, `head_sha`
- `conflict_marker_count`
- `hygiene`
- `file_leases`
- `resource_leases`
- `env`
- `policy_profile`
- `expires_at`
- `session_token` or `session_token_hash`

`session_token` is never stored raw. Switchboard stores only a one-way token hash and audit export
reports `session_token_hash_present`.

## Fail-Closed Rules

- Unknown project ids are rejected.
- `task_id` must exist when supplied.
- `claim_id` must exist when supplied and must match `task_id`/`agent_id`.
- `repo_role` must exist in the selected project's `repo_topology.roles`.
- `storage_mode`, `status`, and `dirty_status` must be recognized values.
- `worktree` and `clone` sessions require their matching path.
- JSON fields must decode to the expected object/list shape.
- Negative conflict-marker counts are rejected.

## Audit Events

- `work_session.created`
- `work_session.updated`
- `work_session.completed`
- `work_session.expired`

Audit export includes `work_sessions` and `summary.work_session_count`.

## MCP Tools

- `create_work_session(work_session_json, project)`
- `get_work_session(work_session_id, project)`
- `list_work_sessions(project, task_id?, agent_id?, status?, repo_role?)`
- `update_work_session(work_session_id, updates_json, project)`

## REST API

- `GET /ixp/v1/work_sessions?project=switchboard`
- `POST /ixp/v1/work_sessions`
- `GET /ixp/v1/work_sessions/{work_session_id}?project=switchboard`
- `PATCH /ixp/v1/work_sessions/{work_session_id}`

## Example: Helm SAT-1

```json
{
  "project": "helm",
  "task_id": "SAT-1",
  "agent_id": "claude/SAT-1-basemap-memory",
  "repo_role": "canonical",
  "branch": "claude/SAT-1-basemap-memory",
  "upstream": "origin/main",
  "base_sha": "96f13c3",
  "worktree_path": "/tmp/helm-sat1",
  "storage_mode": "worktree",
  "status": "active",
  "dirty_status": "clean",
  "conflict_marker_count": 0,
  "hygiene": {
    "git_status": "clean",
    "conflict_marker_scan": "passed",
    "repo_role": "canonical"
  },
  "file_leases": [
    {"path": "web/src/basemap.ts", "lease_id": "lease-sat1"}
  ],
  "env": {
    "private_port": 9134,
    "live_8080_touched": false
  }
}
```

## Example: Switchboard SESSION-1

```json
{
  "project": "switchboard",
  "task_id": "SESSION-1",
  "agent_id": "codex/SESSION-1-work-session-model",
  "repo_role": "canonical",
  "branch": "codex/SESSION-1-work-session-model",
  "upstream": "origin/master",
  "worktree_path": "/tmp/projectplanner-session1",
  "storage_mode": "worktree",
  "status": "active",
  "dirty_status": "clean",
  "conflict_marker_count": 0,
  "policy_profile": "code_strict"
}
```

## Follow-On Enforcement

`SESSION-1` defines the model and surfaces. `SESSION-2` starts binding it into execution:

- `claim_task` and `claim_next` accept `work_session_id`, `work_session`, `work_session_json`,
  `session_policy_profile`, and `require_work_session`.
- When `require_work_session=true` or `session_policy_profile=code_strict`, a valid Work Session
  is required before assignment.
- Successful strict claims return `work_session_id` and include `dispatch_reason.work_session`.
- Missing sessions fail with `failure_class=missing_data`.
- Dirty workspaces and conflict markers fail with `failure_class=failed_gate`.
- Wrong task branches and expired sessions fail with `failure_class=stale_branch`.
- Wrong agent/session identity fails with `failure_class=unbound_identity`.
- `claim_next` skips unsafe candidates and reports `dispatch_reason.skipped.work_session` plus
  `dispatch_reason.work_session_findings`.

Later tasks deepen enforcement:

- `SESSION-3`: populate hygiene from repo preflight and conflict-marker scans.
- `SESSION-4`: add `pre_tool_check` for file writes and shell commands.
- `SESSION-5`: gate `complete_claim` on pushed clean branch/session proof.
- `SESSION-6`: gate merge on session/branch/provenance consistency.
- `SESSION-7`: create managed worktrees/clones from repo topology.
