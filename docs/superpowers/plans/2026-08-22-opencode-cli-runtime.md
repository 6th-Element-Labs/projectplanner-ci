# OpenCode CLI Runtime Implementation Plan

> Implemented in-session for ADAPTER-60. Tests: `tests/test_adapter60_opencode_runtime.py`.

**Goal:** Launch OpenCode through the same MCP `start_task` Connect path as Codex, Claude, and Cursor.

**Architecture:** Runtime `opencode` maps to provider `opencode-zen`. Agent Host starts `opencode --auto` with session-scoped `OPENCODE_CONFIG_CONTENT` that requires Switchboard MCP. Host-bound Zen login is first; portable API key is the second lane.

**Spec:** `docs/superpowers/specs/2026-08-22-opencode-cli-runtime-design.md`

## Files

- Registry: `constants.py`, `execution_context.py`, `connect_dispatch.py`, `session_boot.py`, `policy.py`, `placement.py`, `delivery.py`, `runtime_profile.py`, `autopilot_scopes.py`, `task_execution.py`, `agent_host_enrollments.py`, `coordination.py`
- Auth: `capabilities.py`, `provider_credentials.py`, `provider_runtime_auth.py`, `provider_credentials` REST enroll-api-key
- Host: `adapters/agent_host.py`, `adapters/agent_host_enrollment.py`
- Pack: `adapters/opencode/`, `adapters/marketplace.py`, `fixtures/runtime_wake_capabilities.v1.json`
- Docs: `PROVIDER-AUTH-POLICY.md`, `AGENT-HOST-ENROLLMENT.md`, `RUNTIME-WAKE-CAPABILITY-MATRIX.md`, `MCP.md`

## Operator after deploy

1. Add `opencode` to the Switchboard execution-policy allow-list and an `opencode-zen` selector.
2. Install OpenCode CLI and run `opencode auth login` (Zen) on this Mac.
3. Enroll a second personal host with `--runtime opencode`.
4. `start_task(task_id=..., runtime="opencode", project=...)`.
5. Pin Ox Alpha on the task as `opencode/x-preview-f-free` while that window is open.
