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
- `create_managed_work_session(managed_session_json, project)`
- `get_work_session(work_session_id, project)`
- `list_work_sessions(project, task_id?, agent_id?, status?, repo_role?)`
- `get_work_session_health(work_session_id, project)`
- `list_session_health(project, task_id?, agent_id?, status?, only_unsafe?)`
- `update_work_session(work_session_id, updates_json, project)`
- `archive_work_session_workspace(work_session_id, remove_workspace?, project)`
- `repo_preflight(worktree_path, project, task_id?, agent_id?, repo_role?, expected_branch?,
  expected_base_ref?)`
- `preflight_work_session(work_session_id, project, expected_branch?, expected_base_ref?)`

## REST API

- `GET /ixp/v1/work_sessions?project=switchboard`
- `POST /ixp/v1/work_sessions`
- `POST /ixp/v1/managed_work_sessions`
- `GET /ixp/v1/work_sessions/{work_session_id}?project=switchboard`
- `GET /ixp/v1/work_sessions/{work_session_id}/health?project=switchboard`
- `GET /ixp/v1/session_health?project=switchboard&task_id=SESSION-8`
- `PATCH /ixp/v1/work_sessions/{work_session_id}`
- `POST /ixp/v1/work_sessions/{work_session_id}/archive_workspace`
- `POST /ixp/v1/repo_preflight`
- `POST /ixp/v1/work_sessions/{work_session_id}/preflight`

## Repo Preflight

`switchboard.repo_preflight.v1` is the fail-early check agents and hosts run before edit,
claim, complete, or merge. It is side-effect-free unless called through
`preflight_work_session`, which also writes the result into the Work Session.

The report includes:

- `verdict`: `pass`, `warn`, or `deny`.
- `repo_path`, `remote`, `expected_repo`, `branch`, `upstream`, `head_sha`, `base_ref`,
  `base_sha`, and `merge_base`.
- `upstream_distance` and `base_distance` ahead/behind counts.
- `git_status`, `dirty_files`, and `untracked_files`.
- `merge_state` for active merge/rebase/cherry-pick/revert state.
- `conflict_markers` and `conflict_marker_count`.
- `resource_collisions` for active worktree leases held by another agent.
- `findings`, each with `code`, `failure_class`, `severity`, `blocking`, and `message`.

Blocking failure classes:

- `dirty_worktree`
- `conflict_markers`
- `wrong_repo`
- `wrong_branch`
- `stale_base`
- `shared_worktree_collision`
- `detached_head`
- `merge_or_rebase_in_progress`

Warning classes:

- `missing_upstream`
- `missing_base_ref`
- `git_signal_unavailable`

## Session Health

`switchboard.session_health.v1` is the operator-facing verdict for one Work Session. It is
derived from Work Session fields plus `hygiene.repo_preflight`, not from chat.

Unsafe findings block mission progress:

- active session expired
- Work Session status is `blocked` or `expired`
- active session has no workspace path
- dirty worktree
- conflict markers
- failed repo preflight
- blocking preflight findings such as wrong repo, wrong branch, stale base, shared worktree
  collision, detached HEAD, or merge/rebase in progress

Warnings stay visible but do not block by themselves:

- active session has `dirty_status=unknown`
- missing preflight
- warning-only preflight findings such as missing upstream or missing base ref
- active task claim is not bound to a Work Session

Task detail and task lists include `switchboard.task_session_health.v1`, which rolls up active
sessions, unsafe findings, warnings, PR/branch/head evidence, and the next recommended repair.
Mission status promotes blocking findings to `blockers[]` with `kind=unsafe_session`, so a
deliverable page can say exactly which workspace is dirty, conflicted, stale, or otherwise unsafe.
Completed and archived sessions remain historical and do not make a mission red.

Adapters should treat `deny` as a hard stop and repair the named condition before continuing.
`warn` is visible but not automatically blocking; policy may still require a human or coordinator
ack for warning overrides.

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
- Enforcement is controlled by named session policy profiles, exposed by `get_working_agreement`,
  `get_project_contract`, and `work_session_contract.policy_profiles`.
- When `require_work_session=true`, `session_policy_profile=code_strict`, or the task/project
  default resolves to `code_strict`, a valid Work Session is required before assignment.
- Successful strict claims return `work_session_id` and include `dispatch_reason.work_session`.
- Missing sessions fail with `failure_class=missing_data`.
- Dirty workspaces and conflict markers fail with `failure_class=failed_gate`.
- Wrong task branches and expired sessions fail with `failure_class=stale_branch`.
- Wrong agent/session identity fails with `failure_class=unbound_identity`.
- `claim_next` skips unsafe candidates and reports `dispatch_reason.skipped.work_session` plus
  `dispatch_reason.work_session_findings`.

Later tasks deepen enforcement:

- `SESSION-3`: populate hygiene from repo preflight and conflict-marker scans. Done here:
  `preflight_work_session` stores `hygiene.repo_preflight`, updates `dirty_status`, branch,
  upstream, `base_sha`, `head_sha`, and `conflict_marker_count`.
- `SESSION-4`: add `pre_tool_check` for file writes and shell commands.
- `SESSION-5`: gate `complete_claim` on pushed clean branch/session proof. Done here:
  code-strict completion requires matching branch/head SHA, PR/push/offline proof, recorded tests,
  and clean `git diff --check`; dirty completion needs explicit allowance evidence, conflict
  markers block, and refused completion leaves the claim active with a visible failure class.
- `SESSION-6`: gate merge on session/branch/provenance consistency. Done here:
  `merge_gate` verifies canonical repo role, PR mergeability, target branch, required CI/status
  contexts, external-CI evidence when required, and clean Work Session preflight before a merge can
  be requested. The gate returns structured pass/blocked findings and records `merge.gate`; it never
  marks `Done`, which remains reserved for GitHub webhook/reconcile provenance.
- `SESSION-7`: create managed worktrees/clones from repo topology. Done here:
  `create_managed_work_session` allocates task-scoped branch/path/base/env namespace/session token
  from project repo topology, creates a real git worktree or clone, claims the workspace lease,
  stores clean `repo_preflight` hygiene, and returns a normal Work Session for strict claim binding.
  `archive_work_session_workspace` archives managed sessions and can remove owned workspace paths
  after merge cleanup.
- `SESSION-8`: surface session health in Work Session rows, task detail, task lists, mission
  status, mission blockers, generated mission briefs, and the board truth UI so humans and
  coordinator agents trust typed health over stale prose.
- `SESSION-9`: make session enforcement profile-driven. Built-in profiles are `code_strict`,
  `docs_review`, `offline_evidence`, `ui_preview`, and `no_repo`. Profiles define whether a Work
  Session is required, missing-session behavior for `pre_tool_check`, allowed storage modes,
  deny-vs-warn hygiene, test/diff evidence requirements, and merge authority. Helm code-like tasks
  default to `code_strict`; docs/review/offline work can opt into relaxed profiles explicitly.
