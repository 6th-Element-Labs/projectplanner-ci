# OpenCode CLI runtime (Connect + Switchboard MCP)

**Date:** 2026-08-22
**Status:** Approved for implementation
**Board:** project=`switchboard`, workstream=`ADAPTER`

## Outcome

Switchboard launches OpenCode the same way it launches Codex, Claude Code, and Cursor:
`start_task` on the Switchboard MCP server is the only launch door. An enrolled personal
Agent Host starts the native OpenCode CLI with required `taikun-plan` MCP. Ox Alpha is a
per-task model pin (`opencode/x-preview-f-free`), not a runtime id.

## Problem

Agent Host `SUPPORTED_HOST_RUNTIMES` and `CONNECT_RUNTIME_DEFAULTS` only know `codex`,
`claude-code`, and `cursor`. Live Switchboard execution policy allows only `codex` and
`claude_code`. Provider auth has no `opencode-zen` row. `start_task(runtime="opencode")`
therefore refuses before a process exists.

OpenCode Zen currently offers Ox Alpha as a free, zero-retention model with a 1M context
window. That window is time-bounded. The durable product object is the OpenCode CLI.

## Decisions (approved)

1. First-class runtime now, not a one-week generic_cli hack.
2. Runtime `opencode`, provider `opencode-zen`, model chosen per task.
3. Dual auth: host-bound `opencode auth login` on this Mac first; portable Zen API key in
   the vault for later hosts. No silent fallback between those paths.
4. First host is a second personal Agent Host on this Mac (`--runtime opencode`). The
   existing Codex host stays Codex-only.
5. Launch path is Connect: MCP `start_task` → wake → host-owned process → native CLI with
   required Switchboard MCP and a minted task principal.

## Architecture

```text
operator / Autopilot
  MCP start_task(runtime="opencode", task_id=..., project=...)
        │
        ▼
Connect assignment + wake (same lease as Codex)
        │
        ▼
Agent Host (runtime=opencode, one runtime per host)
  materialize worktree
  mint SWITCHBOARD_CONNECT_SESSION_TOKEN
  strip host bearer from child env
  start: opencode --auto [--model <id>]
  inject session-scoped OpenCode MCP config
        │
        ▼
OpenCode CLI (T3 process owned by host)
  talks to https://plan.taikunai.com/mcp?project=<project>
  uses exact Connect agent_id
```

### Identity

| Role | Id | Notes |
|---|---|---|
| Runtime | `opencode` | Aliases: `opencode-cli`, `zen` |
| Host runtime | `opencode` | Same string in Connect `_RUNTIMES` and `CONNECT_RUNTIME_DEFAULTS` |
| Policy registry | `opencode` | `PROJECT_EXECUTION_RUNTIMES`; no hyphen/underscore split |
| Provider | `opencode-zen` | One runtime, one vendor; never a Codex or Claude connection |
| Model | per task | Example: `opencode/x-preview-f-free` (Ox Alpha). Absent model → OpenCode logged-in default. No hidden Ox Alpha default. |

`start_task` already accepts `runtime`. v1 does not add a new MCP `model` field. If the
wake selector or lifecycle carries `model`, the host adds `--model`. Operators pin Ox Alpha
on the task or host env `PM_OPENCODE_MODEL` as an explicit override, never as a silent
fallback that hides a missing pin.

### Dual auth

**Host-bound (this Mac first)**

- Bootstrap: operator runs `opencode auth login` and selects OpenCode Zen.
- Credential stays in the host OpenCode auth file (`~/.local/share/opencode/auth.json`).
- Switchboard does not copy, vault, or broker that file.
- Capability: `supported_host_bound`, `host_class=user_owned_persistent`, exclusive,
  `max_parallel=1`.
- Preflight: `opencode --version` plus `opencode auth list` after stripping metered env
  (`OPENCODE_API_KEY` included). Redacted fingerprint only.
- Auth mode: `zen_host_login` (aliases: `host_login`, `local_login_session`,
  `oauth_personal`, `zen_login`).

**Portable Zen API key**

- Bootstrap: host CLI `enroll-api-key --provider opencode-zen --api-key-stdin`.
- Vault envelope; inject at wake into the isolated process; never argv, logs, or browser.
- Capability: `supported`, `host_class=managed_or_user_owned_worker`.
- LiteLLM: not eligible. OpenCode Zen is already the gateway. Native CLI stays the path.

**Unavailable portable login export**

- Copying host `auth.json` onto another worker is `unavailable`, same posture as Cursor
  personal-session portability.

### Host enrollment and process

- Hosts remain one runtime. Enroll with `--runtime opencode`.
- Preflight proves native CLI on PATH (including `~/.local/bin`) and Zen login.
- Connect default argv: `opencode --auto`.
- `--auto` is the unattended equivalent of Codex
  `--dangerously-bypass-approvals-and-sandbox` and Claude `--dangerously-skip-permissions`.
- Child cwd is the private worktree.
- Child receives the existing Connect note (exact `agent_id`, then
  `Do <TASK> in project <project> via Switchboard.`).
- Capacity owns the process (T3): kill, heartbeat, PTY, workspace.
- Inside OpenCode, MCP is T1 until a plugin can deny tools. A Done-deny plugin is
  follow-on and does not block first enroll.

### MCP injection

After `claim_wake`, mint a task MCP token. Set `SWITCHBOARD_CONNECT_SESSION_TOKEN` and
`PM_MCP_TOKEN` to that token. Remove `SWITCHBOARD_TOKEN` / host bearer from the child.

Do not edit the operator global OpenCode config. Set `OPENCODE_CONFIG_CONTENT` (or a
session-scoped `OPENCODE_CONFIG` file with no secret bytes) to:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "taikun-plan": {
      "type": "remote",
      "url": "https://plan.taikunai.com/mcp?project=<project>",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:SWITCHBOARD_CONNECT_SESSION_TOKEN}"
      }
    }
  }
}
```

MCP is required. The token stays in the environment. The config must not contain the raw
bearer. The MCP URL uses the wake project, not a hardcoded `PM_PROJECT`.

### Components

| Unit | Responsibility |
|---|---|
| `connect_dispatch._RUNTIMES` | Admit `opencode` / aliases; map to host runtime + `opencode-zen` |
| `execution_context` aliases + `_RUNTIME_PROVIDERS` | Policy registry and one-vendor rule |
| `constants.PROJECT_EXECUTION_RUNTIMES` | Allow `opencode` in execution policy schema |
| provider capability matrix | Host-bound, api_key, unavailable export rows |
| `CONNECT_RUNTIME_DEFAULTS` | `("opencode", "--auto")` |
| Agent Host launch | MCP config env, model flag, local-auth probe |
| enrollment CLI | `--runtime opencode`, preflight, config executable, enroll-api-key provider |
| wake matrix | `opencode-cli` row |
| marketplace pack | `adapters/opencode/` README + example config |
| session_boot advertised runtimes | Include `opencode` once Connect argv tests prove the CLI |

## Error handling

| Condition | Result |
|---|---|
| `start_task(runtime="opencode")` before this lands on the server | Existing `unsupported_runtime` |
| No enrolled online host advertising `opencode` | Existing capacity / no-eligible-host refusal |
| Host runtime not in `CONNECT_RUNTIME_DEFAULTS` | Host refuses; does not guess argv |
| OpenCode binary missing | Enrollment and service-run fail closed |
| Host not logged into Zen | Preflight fails closed; launch does not start |
| Host-bound login missing and vault key missing | Fail closed; no cross-auth fallback |
| Host-bound login present but vault key also in env | Strip `OPENCODE_API_KEY` on the host-bound path so login is not silently replaced |
| MCP token mint denied | Do not start the child |
| Workspace materialization fails | Existing typed workspace refusal |
| Model string present but empty | Omit `--model`; do not pass `--model ""` |
| Live project policy omits `opencode` after deploy | `runtime_not_authorized` until the operator adds it |

Fallbacks are named. Missing OpenCode never looks like a green Codex or Claude launch.

## Testing

Hermetic tests (no live OpenCode, no live Zen, no live enroll):

1. Connect admits `opencode` / `opencode-cli` / `zen` and refuses unknown runtimes.
2. Execution context maps `opencode` → provider `opencode-zen` and refuses another vendor's
   connection.
3. Capability matrix: host-bound allowed on `user_owned_persistent`; api_key allowed;
   auth.json export unavailable; LiteLLM false on all three.
4. `CONNECT_RUNTIME_DEFAULTS` includes `opencode`. Launch argv is interactive
   `opencode --auto`, not a guessed `--prompt` wrapper.
5. Launch env has `OPENCODE_CONFIG_CONTENT` with required `taikun-plan` MCP, wake project
   in the URL, `{env:SWITCHBOARD_CONNECT_SESSION_TOKEN}`, and no raw token in the JSON.
6. `--model` appears only when selector/lifecycle/env pin is non-empty.
7. Enrollment `--runtime opencode` is an allowed choice; preflight succeeds on mocked
   version + auth list that names `opencode`; fails when CLI missing or auth list has no
   Zen provider.
8. Wake matrix includes `opencode-cli` and fails closed with empty setup.
9. Existing Codex/Claude/Cursor Connect tests still pass.

## Out of scope (follow-on)

- OpenCode plugin that denies MCP `update_task` Done (T2).
- `start_task(model=...)` MCP field.
- Long-lived `opencode serve` + `--attach`.
- Live `set_project_execution_policy` on plan.taikunai.com (operator step after deploy).
- Signing and installing the host bundle on this Mac (operator step after merge/deploy).
- Multi-runtime hosts.

## Operator steps after merge

1. Deploy the control plane that contains these rows.
2. Add `opencode` to the Switchboard project execution-policy allow-list and an
   `opencode-zen` selector.
3. Install OpenCode CLI; `opencode auth login` (Zen).
4. `begin_agent_host_enrollment` then install the signed bundle with `--runtime opencode`.
5. `start_task(task_id=..., runtime="opencode", project=...)`.
6. Pin Ox Alpha on that task while the free window is open
   (`opencode/x-preview-f-free`).
