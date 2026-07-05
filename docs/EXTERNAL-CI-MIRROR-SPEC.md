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
- `source_repo`: private/source-of-truth GitHub repo
- `source_branch`
- `source_sha`: private source SHA being verified
- `mirror_repo`: public CI repo
- `mirror_branch`: disposable branch, default `ci/<task-id>/<source-sha-prefix>`
- `workflow`: public workflow file or ref
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
- `payload`: source project, source repo, source branch, source SHA, mirror repo, mirror branch,
  workflow, task id, and claim id

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

## Evidence Semantics

An external CI run is verification evidence for a private source SHA. It is not merge provenance.

Successful external CI can satisfy a review or claim gate once configured, but it must not mark a
task `Done` by itself. `Done` still requires default-branch merge provenance or verified offline
evidence.

## Example

```json
{
  "source_project": "helm",
  "source_repo": "StevenRidder/Helm",
  "source_branch": "codex/WX-22-four-frame-bake",
  "source_sha": "abcdef1234567890abcdef1234567890abcdef12",
  "mirror_repo": "6th-Element-Labs/helm-public-ci",
  "mirror_branch": "ci/WX-22/abcdef123456",
  "workflow": "strict.yml",
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
  "run_url": "https://github.com/6th-Element-Labs/helm-public-ci/actions/runs/...",
  "logs_url": "https://github.com/6th-Element-Labs/helm-public-ci/actions/runs/.../logs",
  "result": {
    "tested_public_sha": "...",
    "source_sha": "abcdef1234567890abcdef1234567890abcdef12"
  }
}
```

## Next Tasks

- `CI-MIRROR-2`: mirror push, workflow trigger, run polling, and artifact capture.
- `CI-MIRROR-3`: roll external CI evidence into task detail, claim/review gates, and mission pages.
