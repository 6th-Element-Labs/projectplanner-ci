# Mission Bot v5 — simple ADR-0008 pager

- **Status:** Current implementation contract
- **Authority:** [ADR-0008 — three-plane separation](decisions/0008-three-plane-separation.md)
- **Replaces:** Mission Bot v4 as the production scoped coordinator

## Purpose

Mission Bot v5 pages a fresh LLM when an operator-armed mission has unhandled work.
It does not classify the work.

The complete controller is:

```text
scope inactive?                       -> WAIT
verified terminal provenance stored? -> DONE
dependencies unmet?                  -> WAIT
mission state is HUMAN?               -> WAIT
runner_sessions reports live?         -> WAIT
unhandled mission event?              -> start_task(stored role, event sequence)
otherwise                             -> WAIT
```

No other decision is permitted.

## State and roles

`mission_items` has four states only:

- `ACTIVE`
- `WAITING`
- `HUMAN`
- `DONE`

There is no `RUNNING` mission state. `runner_sessions` alone owns physical liveness.

The stored requested role is one of:

- `implementation`
- `review_merge`
- `remediation`

Mission Bot never derives a role from CI, review, GitHub, task status, a message, or a
host. Operator Start creates `implementation`. The C3 completion handoff stores
`review_merge`. A current fenced LLM may yield the next role. One narrow projector may
store `remediation` when persisted GitHub evidence reports a failure for the exact
current PR head. It permits two remediation rounds, then parks the mission in `WAITING`.

## Retained boundaries

- `autopilot_scopes` is the sole scoped drive authority.
- `runner_sessions` is the sole physical liveness authority.
- `start_task` is the sole door into Capacity.
- Live `runner_sessions` limits V5 to three concurrent executions by default.
- The execution lease reaper is the sole automatic stop executor.
- Messages have no lifecycle authority.
- GitHub required checks and the native merge queue own landing.
- Canonical merge provenance or verifier-stamped offline provenance alone owns Done.
- The exact W2 scope lease is the V5 launch identity. The V5 coordinator does not
  register as an agent and does not acquire a claim, grant, provider identity, trusted
  host identity, or personal execution connection.

## Removed from the production controller

V5 does not contain or call:

- the stuck-mission release classifier;
- pending wake or claim state as a mission state;
- Work Sessions, claims, messages, registrations, or host state as liveness;
- general CI/review classification beyond the bounded exact-head failure projector;
- automatic human escalation;
- copied dossiers, findings, diagnoses, or provider payloads in launch instructions;
- remediation launch-pointer classification;
- unbounded retry or alternate start fallback; or
- a competing merge, completion, stop, or Done owner.

The launch instruction contains only project, task, and event sequence. The new LLM calls
`get_mission_context` and reads current facts through the configured MCP/API boundary.

## Small operational controls

- `mission_launch_attempts` stores `reason`, `start_error`, `retry_count`, and
  `next_retry_at` for one mission key.
- Launch retry uses exponential backoff. Three failed admissions exhaust the mission key
  by default. There is no fallback launcher.
- `PM_MISSION_BOT_V5_MAX_CONCURRENCY` defaults to `3` and counts only live
  `runner_sessions`.
- `PM_MISSION_BOT_V5_MAX_CI_REMEDIATIONS` defaults to `2`.
- `PM_MISSION_BOT_V5_RUNTIME` and `PM_MISSION_BOT_V5_CODEX_PROFILE` select the CLI
  runtime and profile at the one `start_task` seam.

## Direct CLI launch modes

The local CLI supervisor has one generic launch contract with two modes. The modes
share process admission, `runner_sessions` liveness, heartbeat, terminal state,
bounded retry, cancellation, and duplicate prevention. They do not share repository
requirements.

### Task mode

`task` is the default mode. Its only required input is `prompt`.

```text
launch_agent(prompt="Spell train", profile="luna-simple", mode="task")
```

Task mode:

- does not require a repository, branch, worktree, GitHub connection, CI, or merge;
- does not require a Switchboard project, task, deliverable, scope, or mission;
- accepts an optional working directory;
- may run one agent or a bounded parallel batch;
- stores process identity and liveness in `runner_sessions`; and
- reports exit status and an output reference without storing the prompt in telemetry.

Switchboard context is optional. When `project` and `task_id` are supplied, the
launcher adds the current task context and reporting instructions to the boot prompt.
Switchboard can then observe the run. Switchboard is not launch authority for this
mode and its absence must not block admission.

```text
launch_agent(
  prompt="Complete the assigned document work",
  profile="luna-simple",
  mode="task",
  project="maxwell",
  task_id="DOC-17",
)
```

### Coding mode

`coding` is the repository workflow layer. It requires a repository supplied by the
caller or resolved from the supplied Switchboard task.

```text
launch_agent(
  prompt="Implement the assigned change",
  profile="luna-simple",
  mode="coding",
  project="maxwell",
  task_id="API-42",
  repository="ActionEngine",
)
```

Coding mode adds repository resolution, an isolated worktree and branch, commits,
tests, PR handling, CI remediation, review, and merge. These behaviors must not leak
into task mode.

### Common launch rules

- `prompt` is required and must not be copied into activity, authorization, or runner
  telemetry. Store a digest and a protected local assignment reference instead.
- `profile`, `working_directory`, `project`, `task_id`, and `repository` are optional
  in task mode.
- A supervisor restart attaches to the existing live `runner_sessions` row. It does
  not create a duplicate process.
- No launch requires coordinator registration, provider authorization, trusted-host
  enrollment, a claim, a grant, or a personal execution connection.
- A remotely exposed launcher may retain one outer service access boundary. That
  boundary does not become per-run authorization or lifecycle state.
- Process exit is terminal execution evidence. It is not, by itself, authority to
  mark a Switchboard coding task Done or to declare a PR merged.

## Remaining transport subtraction

The V5 pager and its scope launch boundary no longer require coordinator registration,
claims, grants, provider authorization, or trusted-host identity. The current production
`start_task` implementation can still deliver its admitted request through the older
Connect wake and Agent Host transport. That transport is not part of the V5 controller
and is not a host-free CLI supervisor. Replace it with one local CLI supervisor before
claiming that the complete runtime has no host component.

## Production composition

`coordinator_daemon.py` constructs `V5ScopedCompletionCoordinator` when
`PM_COORDINATOR_AUTOPILOT_ACT=1`. The coordinator may drive only an exact live
`autopilot_scopes` lease. It performs at most one `start_task` call per task tick and
revalidates the mission cursor and scope fence immediately before that call.

Mission Bot v4 remains temporarily importable for compatibility tests. It is not selected
by the production entrypoint. Delete it after V5 canary evidence and rollback expiry.
