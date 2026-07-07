# External CI Mirror Runs

Switchboard supports a public CI mirror pattern for cases where the source repository cannot spend
private GitHub Actions minutes.

The rule is:

```text
Private repo = source of truth
Public CI repo = disposable verification mirror
Switchboard = provenance/control plane
```

Agents should not develop or fix code in the public CI repository. They fix the private branch,
request a new mirror run, and let Switchboard link the public workflow result back to the private
source SHA.

## Why Agents Thrash Without This

The public CI repository looks like a second development repo unless the protocol says otherwise.
Common failure modes:

- Agents treat the public CI branch as editable source code.
- Private branch SHAs and public mirror SHAs are not mapped.
- A mirror/push failure looks like a test failure.
- A workflow-trigger failure looks like code failure.
- Switchboard sees one repo while GitHub checks run in another.
- Agents wait for private checks that will not run because Actions quota is exhausted.

External CI mirror runs make that state explicit.

## Data Model

`external_ci_runs`

- `run_id`: Switchboard run id, such as `ecir-...`
- `source_project`: Switchboard project that owns the private source repo
- `source_repo`: private/source-of-truth GitHub repo, resolved from
  `repo_topology.roles.canonical.repo`
- `source_branch`
- `source_sha`: private source SHA being verified
- `mirror_repo` / `ci_repo`: public CI repo resolved from
  `repo_topology.roles.public_ci.repo`
- `mirror_branch`: disposable branch, default `ci/<task-id>/<source-sha-prefix>`
- `workflow`: public workflow file or ref
- `status_context`: required status context from
  `repo_topology.roles.public_ci.required_status_contexts`, or an explicit context selected from
  that role
- `status`: `requested`, `mirrored`, `triggered`, `running`, `success`, `failure`, `cancelled`, or `error`
- `conclusion`: provider conclusion, such as `success`, `failure`, or `cancelled`
- `run_url`
- `logs_url`
- `artifacts_json`
- `failure_class`: one of `mirror_sync_failed`, `workflow_trigger_failed`, `workflow_poll_failed`, `workflow_failed`
- `failure_reason`
- `task_id`
- `claim_id`
- `agent_id`
- `actor`
- `principal_id`
- `effect_key`: linked `external_side_effects` key
- `request_json`
- `result_json`
- `requested_at`, `mirrored_at`, `triggered_at`, `completed_at`, `updated_at`

Creating an external CI run reserves an `external_side_effects` row with:

- `effect_type`: `external_ci_mirror`
- `target`: public mirror repo
- `resource`: public mirror branch
- `payload`: source project, source repo, source branch, source SHA, CI repo, mirror branch,
  workflow, status context, task id, and claim id

That side-effect key gives `CI-MIRROR-2` idempotency before it pushes or triggers anything.

## State Flow

```text
requested
  -> mirrored
  -> triggered
  -> running
  -> success | failure | cancelled | error
```

Meaning:

- `requested`: Switchboard recorded the desired verification and reserved the side effect.
- `mirrored`: the exact source tree/SHA was pushed to the public mirror branch.
- `triggered`: the public workflow was started.
- `running`: the public workflow is in progress.
- `success`: the public workflow passed.
- `failure`: the public workflow ran and failed.
- `cancelled`: the public workflow was cancelled.
- `error`: Switchboard could not complete the mirror/trigger/poll workflow.

## Failure Classes

- `mirror_sync_failed`: public branch was not created or did not match the source tree/SHA.
- `workflow_trigger_failed`: GitHub Actions workflow could not be started.
- `workflow_poll_failed`: Switchboard could not read the workflow run state.
- `workflow_failed`: workflow completed red.

These classes are intentionally separate so agents know whether to fix code, fix mirror plumbing,
or wait/retry provider state.

## Operational Tool Contract

`CI-MIRROR-2` adds an executable mirror runner plus MCP/API tools.

Normal agent path:

```text
private/source checkout
  -> request_external_ci_mirror_run(...)
  -> Switchboard creates external_ci_runs row and side-effect reservation
  -> git push <source_sha>:refs/heads/ci/<task-id>/<sha-prefix> to mirror repo
  -> gh workflow run <workflow> --repo <mirror_repo> --ref <mirror_branch>
  -> gh run list/view polling
  -> external_ci_runs result + external_side_effects readback updated
```

MCP tools:

- `request_external_ci_mirror_run`: create/resume a mirror run, push, dispatch, poll, and record
  the result.
- `poll_external_ci_mirror_run`: resume polling an existing run after a runtime/session
  interruption.
- `list_external_ci_runs`: list tracked mirror runs by task, source project/SHA, or status.
- `get_external_ci_run`: read one mirror run.

REST/API endpoints:

- `GET /ixp/v1/external_ci_runs`
- `GET /ixp/v1/external_ci_runs/{run_id}`
- `POST /ixp/v1/external_ci_mirror/request`
- `POST /ixp/v1/external_ci_mirror/poll`

Required request fields for execution:

- `source_path`: local private/source-of-truth Git checkout path.
- `source_project`
- `source_sha`
- `workflow`
- `task_id` when attaching to task evidence.

Optional request fields:

- `source_repo`: defaults from `source_project` when that project has a configured GitHub repo.
- `mirror_repo` / `ci_repo`: defaults from `source_project` repo topology `roles.public_ci.repo`.
- `source_branch`
- `mirror_branch`: defaults to `ci/<task-id>/<source-sha-prefix>`.
- `mirror_remote_url`: defaults to `https://github.com/<mirror_repo>.git`.
- `status_context`: defaults from `roles.public_ci.required_status_contexts`.
- `workflow_inputs`: passed as sorted `-f key=value` arguments to `gh workflow run`.
- `poll_interval_seconds` and `timeout_seconds`.

The runner requires local `git` and `gh` provider access. Missing credentials, missing local SHAs,
failed pushes, failed workflow dispatch, polling failures, and red workflows are all written back
as visible `external_ci_runs.failure_class` values. A public CI run may verify a private SHA, but it
does not mark a task `Done`.

## Evidence Semantics

An external CI run is verification evidence for a private source SHA. It is not merge provenance.

Successful external CI can satisfy a review or claim gate once configured, but it must not mark a
task `Done` by itself. `Done` still requires default-branch merge provenance or verified offline
evidence.

## Task And Mission Surfaces

`CI-MIRROR-3` makes external CI proof visible in the same surfaces agents already read:

- task detail includes `external_ci.status`, `source_repo`, `source_sha`, `ci_repo`, `run_url`,
  `status_context`, `conclusion`, recent runs, and an `external_ci_passed` review gate.
- MCP `get_task` and REST `GET /api/tasks/{task_id}` expose the same `external_ci` payload.
- board rows expose compact external CI status so operators can scan proof state.
- deliverable/mission task links include each linked task's external CI proof summary.
- deliverable progress includes external CI required/passed/blocked counts.

Configured review gates:

- A task or claim can require external CI by including `external_ci_passed` in task gate text,
  `agent_state.review_gate`, `agent_state.proof_requirements`, or claim evidence
  `required_gates` / `review_gates`.
- `complete_claim` records a visible `task.review_gate` activity payload when external CI is
  required.
- Missing required external CI blocks review/merge confidence, but it does not promote or demote
  `Done`. Merge provenance remains the only code-task Done signal.

## Example

```json
{
  "source_project": "helm",
  "source_repo": "StevenRidder/Helm",
  "source_branch": "codex/WX-22-four-frame-bake",
  "source_sha": "abcdef1234567890abcdef1234567890abcdef12",
  "mirror_repo": "StevenRidder/public-CI",
  "mirror_branch": "ci/WX-22/abcdef123456",
  "workflow": "strict.yml",
  "status_context": "helm-ci/full-suite",
  "task_id": "WX-22",
  "claim_id": "taskclaim-...",
  "agent_id": "codex/WX-22-four-frame-bake"
}
```

After `CI-MIRROR-2`, the same run should include:

```json
{
  "status": "success",
  "conclusion": "success",
  "run_url": "https://github.com/StevenRidder/public-CI/actions/runs/...",
  "logs_url": "https://github.com/StevenRidder/public-CI/actions/runs/.../logs",
  "result": {
    "tested_public_sha": "...",
    "source_repo": "StevenRidder/Helm",
    "source_sha": "abcdef1234567890abcdef1234567890abcdef12",
    "ci_repo": "StevenRidder/public-CI",
    "status_context": "helm-ci/full-suite"
  }
}
```

## Next Tasks

- `CI-MIRROR-2`: mirror push, workflow trigger, run polling, and artifact capture.
- `CI-MIRROR-3`: roll external CI evidence into task detail, claim/review gates, and mission pages.
