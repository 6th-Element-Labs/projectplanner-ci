# Switchboard — OpenCode adapter (ADAPTER-60)

Connect launches the native OpenCode CLI the same way it launches Codex, Claude
Code, and Cursor. The Agent Host owns the process. MCP is required.

## Launch

```text
start_task(task_id=<TASK>, runtime="opencode", project=<project>)
```

The host starts:

```text
opencode --auto [--model <id>]
```

`--auto` is the unattended permission flag. Add `--model` only when the task or
wake names a model. Ox Alpha is `opencode/x-preview-f-free`. It is a model pin,
not a runtime id.

## MCP

The host injects session-scoped OpenCode config through `OPENCODE_CONFIG_CONTENT`.
It does not edit `~/.config/opencode`. The bearer stays in
`SWITCHBOARD_CONNECT_SESSION_TOKEN`. See [`mcp.example.json`](mcp.example.json).

Inside OpenCode, MCP is T1 until a plugin can deny tools. Host process control
is T3.

## Auth

Host-bound: `opencode auth login` on the enrolled machine (OpenCode Zen).
Portable: `enroll-api-key --provider opencode-zen` on a later host. No silent
fallback between those paths.

## Enroll

Install a second personal Agent Host. Do not add OpenCode onto a Codex host.

```bash
python adapters/agent_host_enrollment.py preflight --runtime opencode
# then install the signed bundle with --runtime opencode
```
