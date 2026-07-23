# Cursor attention live probe

This probe server emits one MCP `elicitation/create` request with a two-choice
JSON schema. It exists to test the Cursor client, not to provide a production
question bridge.

For an isolated probe workspace, add this server to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "cursor-attention-probe": {
      "command": "python3",
      "args": ["/absolute/repo/scripts/cursor_attention_probe/probe_mcp.py"]
    }
  }
}
```

Then run the pinned Cursor build with `--approve-mcps` and ask it to call
`ask_rollout`. Record the exact CLI version before and after the run because
Cursor auto-updates. Never record credential files, auth tokens, account
details, or unredacted workspace paths.

On `2026.07.23-e383d2b`, both print and interactive modes returned
`ELICITATION_REPLY:{"action":"decline"}` without accepting a human response.
That is the expected replay result until a newer pinned probe proves otherwise.
