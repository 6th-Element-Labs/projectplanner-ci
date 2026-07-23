# CO-fleet runtime config (`ssm:/switchboard/co/runtime-config`)

The ephemeral AWS workers build their entire worker command line from a single
SSM SecureString. It is referenced by the wake policy as `runtime_config_ref`
and consumed by the CO-3 bootstrap in [`co_fleet.py`](../co_fleet.py) (see
`render_runtime_extension`), which filters it through an allowlist and writes
`/etc/switchboard-co/agent-host.env`.

**This file is the schema of record.** The parameter itself is not in version
control (it holds a token), so the shape, the required values and the deployed
version must be recorded here. BUG-91 was caused by a wrong value in this
parameter that nothing in the repo described, so nothing could catch it.

## Schema

| key | required | notes |
|---|---|---|
| `PM_MCP_TOKEN` | **yes** | bootstrap fails closed without it |
| `PM_AGENT_WORK_MODULE_CODEX` | **yes** for codex pools | see below — this is what BUG-91 got wrong |
| `PM_AGENT_WORK_MODULE_CLAUDE_CODE` | for claude-code pools | |
| `PM_AGENT_WORK_MODULE_CURSOR` | for cursor pools | |
| `PM_AGENT_WORK_MODULE` | fallback | used when no runtime-specific key is set |
| `PM_AUTO_WORK_SESSION` | **yes** for `code_strict` lanes | without it a code_strict task is never claimed |
| `PM_BASE`, `PM_PROJECT` | yes | |
| `PM_VERIFY_COMPLETION_PUSH` | recommended | see the push-verify memo |
| `PM_WORK_SESSION_TEST_CMD` | optional | |
| `AWS_REGION`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_HOST` | optional | |

Anything outside the allowlist in `co_fleet.py` is **silently dropped** — if a
setting appears to have no effect, check the allowlist before debugging the host.

These keys are rejected outright (personal-subscription fleet, no API-key
fallbacks): `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `OPENAI_API_KEY`,
`CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`.

## Correct values

```json
{
  "PM_BASE": "https://plan.taikunai.com",
  "PM_PROJECT": "switchboard",
  "PM_MCP_TOKEN": "<secret>",
  "PM_AGENT_WORK_MODULE_CODEX": "adapters.codex_local_worker:run",
  "PM_AUTO_WORK_SESSION": "1",
  "PM_VERIFY_COMPLETION_PUSH": "1",
  "AWS_REGION": "us-east-1"
}
```

## What BUG-91 found deployed (2026-07-20)

The observed AWS worker command line was:

```
run_agent.py --runtime codex --max-tasks 1 --lanes SEG --idle-seconds 6 \
             --work-module claude_personal_worker:run
```

versus the working Mac:

```
run_agent.py --runtime codex ... --work-module adapters.codex_local_worker:run \
             --auto-work-session
```

Two defects, both in this parameter:

1. **`PM_AGENT_WORK_MODULE_CODEX` was `claude_personal_worker:run`** — the Claude
   worker running a Codex runtime. `codex_local_worker` is the module that sets
   `heartbeat_ttl_s: 180`, runs a heartbeat thread, stamps
   `credential_admission_phase: claim_bound`, and tees output into the supervisor
   PTY. None of that happens with the wrong module.
2. **`PM_AUTO_WORK_SESSION` was unset** — and could not have been set, because it
   was missing from the bootstrap allowlist until this change. SEG lanes are
   `code_strict`, and `adapters/switchboard_core.py` skips the claim when a
   code_strict task needs a Work Session and `auto_work_session` is off. Result:
   80 of 84 measured `claim_next` runner rows had no `claim_id` and no
   `work_session_id`, so they could never satisfy the Watch/Chat bind contract.

Separately, those hosts ran `agent_host_version 0.1.0` against the Mac's `0.2.24`;
all 84 of their runner rows had `pty=false`, `stream_bind=false`,
`runner_open=false`, `runner_inject=false` — zero Watch capability. That is an AMI
/ installed-build problem, not a value in this parameter.

## Deployment record

Update this table on every change. Parameter Store keeps versions; the version
number here is what makes rollback a one-liner rather than an investigation.

| date | version | change | applied by |
|---|---|---|---|
| 2026-07-23 | pending rollout | SIMPLIFY-20 default-on execution lease enforcement; record the applied SSM version before promotion | SIMPLIFY-20 |
| _(pre-BUG-91)_ | _unrecorded_ | `PM_AGENT_WORK_MODULE_CODEX=claude_personal_worker:run`, no `PM_AUTO_WORK_SESSION` | unknown — this is the gap this file closes |

### Applying a change

```bash
# 1. Record the current version first — this is the rollback target.
aws ssm get-parameter --name /switchboard/co/runtime-config --with-decryption \
  --query 'Parameter.Version' --output text

# 2. Apply (SecureString; --overwrite creates a new version).
aws ssm put-parameter --name /switchboard/co/runtime-config \
  --type SecureString --overwrite --value file://runtime-config.json

# 3. Record the new version in the table above, then canary ONE host before
#    rolling the fleet.
```

### Rolling back

```bash
aws ssm get-parameter --name /switchboard/co/runtime-config:<PREVIOUS_VERSION> \
  --with-decryption --query 'Parameter.Value' --output text > rollback.json
aws ssm put-parameter --name /switchboard/co/runtime-config \
  --type SecureString --overwrite --value file://rollback.json
```

Running instances do **not** re-read this parameter — it is consumed once at
boot. A rollback therefore only affects newly launched hosts; drain or terminate
the instances started with the bad version.

## Verifying a canary host

```bash
# The rendered env the worker actually uses:
sudo cat /etc/switchboard-co/agent-host.env | grep -E 'WORK_MODULE|AUTO_WORK_SESSION'
```

Then confirm from the board that the host advertises watch capability and that
its runner binds — a host that cannot serve Watch will not advertise
`runner_watch`, and placement can be made to refuse it via
`PM_COORD_REQUIRE_RUNNER_WATCH=1`:

```
list_agent_hosts(project="switchboard")   -> runtimes[].capabilities includes "runner_watch"
list_runner_sessions(task_id="<canary>")  -> claim_id and metadata.work_session_id both set,
                                             metadata.pty true, control.runner_open true
```

`list_agent_hosts` also exposes `runtime_profile.hash` and
`runtime_profile.components`. Hybrid dispatch now requires the effective runtime module,
automatic Work Sessions for `code_strict`, and the finishing binaries before selecting a host.
Set `PM_EXPECTED_AGENT_HOST_VERSION` or `PM_EXPECTED_AGENT_HOST_PROFILE_HASH` on the coordinator
when a rollout must fence placement to one exact Agent Host build/profile. These values are
expectations only; they do not mutate the SSM parameter or any running host.
