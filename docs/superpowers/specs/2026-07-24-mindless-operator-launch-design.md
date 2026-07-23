# Operator Launch Mode for Agentic Workers

Date: 2026-07-24 · Board: `SESSION-17` · Approved by Steve in-session.

## Goal

Add a first-class launcher mode to `prepare_agent_session` that teaches MCP
clients to launch task-bound CLI workers through the existing `start_task`
command safely.

This change improves discovery and boot guidance. It does not introduce a
second execution path: `start_task -> Connect` remains the sole launch
primitive.

The existing worker boot contract must remain unchanged when launcher mode is
not explicitly selected.

## Non-goals

- Do not add a Connect runtime named `cli`.
- Do not add a batch `launch_tasks` command in v1.
- Do not change UI Start behavior.
- Do not create claims, Work Sessions, worktrees, prompts, or credentials in
  `prepare_agent_session`.
- Do not reject `claim_task` based only on a launcher-shaped agent ID in v1.
- Do not claim that a runtime is supported until its complete Agent Host and
  CLI path has been proven.

## Architecture

### One launch command

`start_task` remains the public MCP command that requests execution.

`prepare_agent_session` prepares one of two session modes:

- `worker`: current task/lane workflow, byte-stable from today's behavior.
- `launcher`: operator workflow whose normal actions are `start_task` and
  `get_task_execution`.

### Typed mode

Expose typed `mode` with values `worker | launcher`. Compatibility: `intent`
aliases `launch`, `operator`, and `start` normalize to `launcher`. Empty,
`work`, `implement`, unknown values remain `worker`.

**Conflict rule:** when both are set, `mode` wins.

### Launcher identity

When no explicit `agent_id` is supplied, launcher mode suggests
`<runtime>/launcher`. Default runtime label `cursor` is identity display only.

Launcher registers with `task_id=""`. The target task appears only in
`start_task`.

### Runtime selection

`launch_runtime` selects the CLI worker. Default: `codex`. Never inferred from
the launcher's own runtime. Advertised supported runtimes: `codex`, `claude`
(aliases may still be accepted; `cursor` is not advertised until E2E proven).

### Machine-readable launcher contract

```json
{
  "mode": "launcher",
  "allowed_actions": ["start_task", "get_task_execution"],
  "forbidden_actions": ["claim_task", "claim_next"],
  "launch_defaults": {
    "role": "implementation",
    "runtime": "codex"
  }
}
```

Worker payloads omit these keys (pre-change shape).

## Acceptance

See `tests/test_operator_launch_boot.py` and board task SESSION-17.
