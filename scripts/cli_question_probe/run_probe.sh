#!/usr/bin/env bash
# Reproducible probe: prove that each CLI agent surfaces "I need a human"
# as a MACHINE EVENT, not screen text. Run per agent; the captured event
# lands in questions-queue.jsonl — that file IS the operator question queue.
#
#   ./run_probe.sh claude    # live-tested, no extra creds needed here
#   ./run_probe.sh codex     # needs OPENAI_API_KEY / codex login
#   ./run_probe.sh cursor    # needs CURSOR_API_KEY / cursor-agent login
#
# See docs/CLI-QUESTION-EVENTS.md for what each event looks like and how the
# host runner turns it into a Switchboard queue row.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export QUESTION_QUEUE="$here/questions-queue.jsonl"
: > "$QUESTION_QUEUE"
agent="${1:-claude}"

case "$agent" in
  claude)
    # Claude Code: point it at a permission-prompt tool we own. When it wants
    # to Write a file it isn't pre-allowed to, it CALLS our tool with the exact
    # action — that call is the question. perm_mcp.py logs it and denies (park).
    cat > "$here/mcp-config.json" <<JSON
{"mcpServers":{"permgate":{"command":"python3","args":["$here/perm_mcp.py"]}}}
JSON
    claude -p "Write the text 'hello' into out.txt, then read it back." \
      --output-format stream-json --verbose \
      --permission-mode default \
      --mcp-config "$here/mcp-config.json" \
      --permission-prompt-tool mcp__permgate__approval_prompt \
      < /dev/null > "$here/claude-stream.jsonl" 2>"$here/claude.err"
    ;;
  codex)
    # Codex: exec --json emits typed JSONL. With --ask-for-approval on-request
    # a proposed command surfaces as item/commandExecution/requestApproval,
    # which the client (host runner) answers accept/decline. Codex can also run
    # as `codex mcp-server` and raise approvals via MCP elicitation.
    codex exec --json --skip-git-repo-check -s read-only \
      --ask-for-approval on-request \
      "Run the shell command 'whoami'." \
      < /dev/null > "$here/codex-stream.jsonl" 2>"$here/codex.err"
    echo "Approval events in the stream:"
    grep -E 'requestApproval|"type":"error"' "$here/codex-stream.jsonl" | head -5
    ;;
  cursor)
    # Cursor: cursor-agent -p --output-format stream-json emits system/user/
    # assistant/tool-call events. Shell commands need an allow/deny policy;
    # without --force a tool call is gated and surfaces as a tool-call event
    # the runner must approve.
    cursor-agent -p --output-format stream-json \
      "Run the shell command 'whoami'." \
      < /dev/null > "$here/cursor-stream.jsonl" 2>"$here/cursor.err"
    ;;
  *) echo "usage: $0 {claude|codex|cursor}"; exit 2 ;;
esac

echo "=== questions-queue.jsonl (machine events captured) ==="
cat "$QUESTION_QUEUE" 2>/dev/null || echo "(empty — see the *-stream.jsonl for the raw event stream / auth wall)"
