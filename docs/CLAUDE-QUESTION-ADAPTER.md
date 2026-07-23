# Claude Code structured question adapter

ADAPTER-24 supports one provider-native attention capability: Claude Code
`AskUserQuestion` through a `PreToolUse` command hook using
`permissionDecision: "defer"`. The supported image is pinned to Claude Code
`2.1.202`; the adapter refuses an unprobed version. Claude Code added deferred
tool calls in `2.1.89`.

This is deliberately narrower than the old `--permission-prompt-tool` probe in
`scripts/cli_question_probe`. Version 2.1.202 does not advertise that CLI flag.
Generic permission prompts, `ExitPlanMode`, arbitrary tool approvals, terminal
text classification, and stdin scraping are unsupported by this adapter. They
fail closed and remain visible as a hook denial.

## Native round trip

1. Claude calls `AskUserQuestion`. `PreToolUse` supplies the native
   `session_id`, `tool_use_id`, `tool_name`, and unmodified `tool_input`.
2. The hook creates one durable Switchboard attention request. Its context
   preserves the complete provider payload and binds it to project, task, Work
   Session, runner session, and host.
3. The hook returns `permissionDecision: "defer"`. In print mode Claude exits
   with `stop_reason: "tool_deferred"` and a `deferred_tool_use` object.
4. The operator records a decision whose `choice.answers` object is keyed by
   the exact provider question text.
5. The runner resumes with `claude -p --resume <session_id>`. The same
   `PreToolUse` hook fires for the preserved `tool_use_id`; the adapter claims
   the decision and returns `permissionDecision: "allow"` with the original
   questions plus `updatedInput.answers`.
6. Only after a non-deferred result from the same `session_id` does the adapter
   record the Switchboard delivery receipt and resolve the request.

Cancellation before delivery leaves the request deferred. Reconnect replays the
same provider request id and journal entry. If the process loses the first
`allow` hook output after claiming the decision, the next same-session,
same-tool invocation replays the journaled `updatedInput` without trying to
claim the one-shot decision again. A completion from another Claude session is
rejected. A malformed or partial answer is denied rather than silently
defaulted.

## Hook configuration

Generate a settings fragment with
`adapters.claude_question_adapter.hook_settings(...)`, or configure the
equivalent command directly:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "AskUserQuestion",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/adapters/claude_question_adapter.py"
      }]
    }]
  }
}
```

The command requires `PM_PROJECT`, `PM_TASK_ID`, `PM_WORK_SESSION_ID`,
`PM_RUNNER_SESSION_ID`, `PM_HOST_ID`, and an absolute
`PM_CLAUDE_QUESTION_JOURNAL`. `PM_CLAUDE_EXECUTABLE` may select the Claude
binary and defaults to `claude`. Every hook invocation runs that executable
with `--version` and denies the request before any Switchboard HTTP call unless
the result is exactly the probed `2.1.202` version. The journal is written
atomically with mode `0600`. The command emits only the structured Claude hook
reply. On internal failure it emits a typed denial without environment,
request bodies, credentials, or traceback content.

## Proof

The replayable fixture is
`tests/fixtures/claude_code_2_1_202_question_roundtrip.json`. Run:

```bash
python3 -m pytest -q tests/test_claude_question_adapter.py
python3 tests/test_proto8_attention_api.py
```

The fixture and tests cover capture schema, queue normalization, exact provider
ids and payload, decision delivery, reconnect, cancellation/session fencing,
completion receipts, version pinning, journal permissions, and fail-closed
unsupported request kinds.

The host probe on 2026-07-24 confirmed the exact binary/version and structured
stream envelope, but the local Claude account was unauthenticated
(`authMethod: none`). That probe therefore did not claim a successful model
round trip; a credentialed Agent Host run is still required for the task's live
exit proof.
