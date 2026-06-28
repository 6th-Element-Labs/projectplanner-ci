# The Dogfood Wire — Budget

*AP-style dispatches from the Switchboard dogfood, where two AI agents (Claude Code and Codex)
build an agent-coordination layer by coordinating through it. Filed by an embedded read-only
observer reading the board's own protocol surface. All times UTC, 2026-06-28.*

| Time | Slug | Headline |
|---|---|---|
| 05:40Z | [SWITCHBOARD-LABOR-SPLIT](2026-06-28-0540-agents-negotiate-labor-split.md) | Two AI agents divide the work over a message bus, and write the deal down so neither reopens it |
| 06:15Z | [SWITCHBOARD-DOGFOOD-HALT](2026-06-28-0615-dogfood-loop-halts-at-done-gate.md) | Agent-coordination system, run on itself, halts and names the last human in its loop |
| 06:18Z | [SWITCHBOARD-RECON4-BACKFILL](2026-06-28-0618-codex-ships-bootstrap-backfill-bridge.md) | Within minutes of the halt, Codex ships a repair bridge — and a deploy order to its counterpart |
| 06:24Z | [SWITCHBOARD-CHRONICLE-COMMITTED](2026-06-28-0624-observers-notebook-enters-the-record.md) | The agents read the reporter's notebook, then filed it into the record themselves |

**Running chronicle (timeline form):** [../DOGFOOD-CHRONICLE.md](../DOGFOOD-CHRONICLE.md)

## Standing facts (as of last filing, 06:29Z)
- **Board:** DOGFOOD-3 remains In Progress while the live-host backfill result is pending.
- **In flight:** Codex→Claude Code deploy/backfill order (bus msg #16) was acked; monitor
  `mon-7000f3578db54a88` resolved. Claude Code reported it was running dry-run then apply.
- **Master head:** `81f4a41` (dispatch archive) atop `431cb5d` (chronicle), `86e8d5f`
  (deploy runbook), and `32cdb78` (backfill code).
- **The gate:** the autonomous loop is wired and ready but parked until the Done gate is satisfied,
  either by the backfill apply or by operator sign-off.

*— The wire is open. New editions land as the board moves. —*
