# How a CLI agent tells you it has a question

**The premise, tested:** a coding-agent CLI does not "print a question to the
terminal and hope someone reads it." When it needs a human it emits a
**structured machine event** on a side channel, and the host process that owns
the terminal receives it. Detecting questions is therefore not screen-scraping
or classification — it is *receiving events the runtime is built to send.*

This doc covers all three runtimes Switchboard dispatches to — **Claude Code**,
**Codex**, **Cursor** — with the exact mechanism, the exact event, and how the
host runner turns each into one row in the operator **question queue** (the
thing behind the bell / the "Needs you" list).

Reproduce any of it: `scripts/cli_question_probe/run_probe.sh {claude|codex|cursor}`.

| Runtime | Status here | Mechanism | The event |
|---|---|---|---|
| Claude Code | ✅ **live-tested** | `--permission-prompt-tool` (+ `AskUserQuestion`) | a tool call into a tool the runner owns, with the action as JSON |
| Codex | ⬤ event stream proven; full turn blocked on `OPENAI_API_KEY` | `--ask-for-approval` + `exec --json` (or `mcp-server`) | `item/commandExecution/requestApproval` JSONL event |
| Cursor | ⬤ blocked on `CURSOR_API_KEY` | `-p --output-format stream-json` + `--force`/`--trust` policy | gated tool-call event in the stream |

---

## 1. Claude Code — live-tested

**Mechanism.** Start the agent with `--permission-prompt-tool <mcp_tool>`. Any
time the agent wants to use a tool it is not pre-allowed to use, the CLI **calls
that MCP tool** with `{tool_name, input}` and *blocks* until the tool returns a
decision. The runner owns that tool, so the request lands in the runner's code.
Open-ended questions ("which database?") use the built-in `AskUserQuestion`
tool, which appears in `--output-format stream-json` as an `assistant` message
containing a `tool_use` block — same "tool call = event" principle.

**Tested.** We ran a real Claude Code agent (v2.1.214) headless, pointed at the
30-line gate in `scripts/cli_question_probe/perm_mcp.py`, and asked it to write a
file. It did **not** print a question — it called the gate. Captured verbatim:

```json
{"kind":"permission_request","tool_name":"Write",
 "input":{"file_path":".../out.txt","content":"hello"}}
```

That line was written to `questions-queue.jsonl` by the gate. That file **is** the
queue. The gate answered `{"behavior":"deny","message":"parked: waiting on operator"}`
— which is exactly what an operator's "redirect" answer would send; `allow`
would let the agent proceed.

**Runner contract.**
- receive: the `approval_prompt` tool call → enqueue `{host, session, task, tool_name, input}`
- answer: return `{"behavior":"allow","updatedInput":{…}}` or `{"behavior":"deny","message":"…"}`
- park: while unanswered, the agent blocks — lease held, nothing else in that session moves.

---

## 2. Codex — event stream proven, full turn needs a key

**Mechanism.** `--ask-for-approval <untrusted|on-failure|on-request|never>`
chooses when the model must ask before running a command; `-s/--sandbox` bounds
what runs without asking. Non-interactive: `codex exec --json` streams typed
**JSONL** events. Codex can also run as an MCP server (`codex mcp-server`) and
raise approvals via MCP **elicitation** — the same shape as Claude's tool gate.

**The event.** A proposed command that needs approval surfaces as
**`item/commandExecution/requestApproval`** — carrying `itemId`, `threadId`,
`command`, `cwd`, an optional `reason`, and `availableDecisions`. The client
(host runner) answers `accept` / `acceptForSession` / `decline` / `cancel`. The
final state arrives as `item.completed` with `status: completed | failed | declined`.
(Schema: [Codex app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
[app-server guide](https://developers.openai.com/codex/app-server),
[exec --json cheatsheet](https://takopi.dev/reference/runners/codex/exec-json-cheatsheet/).)

**Tested here.** `codex exec --json` (v installed) emits the real envelope —
`thread.started`, `turn.started`, `item.completed`, `error` — so the JSONL
channel is confirmed. Driving it to an actual `requestApproval` needs
`OPENAI_API_KEY` (this sandbox has none → `401` before the model runs). With a
key, `run_probe.sh codex` prints the approval events.

**Runner contract.** subscribe to `codex exec --json` (or the app-server socket);
on `item/commandExecution/requestApproval` → enqueue; answer with an approval
decision; treat `item.completed` as authoritative.

---

## 3. Cursor — mechanism confirmed, full turn needs a key

**Mechanism.** `cursor-agent -p --output-format stream-json` runs headless and
emits stream events: `system` (init: session id, model, `permissionMode`),
`user`, `assistant` (incremental), and **tool-call** events. Shell/tool actions
need an explicit policy: `--force`/`--yolo` (allow), `--trust` (trusted
workspace), `--approve-mcps` (auto-approve MCPs). Without an allow policy a tool
call is **gated** — it surfaces as a tool-call event the runner must approve
before the agent proceeds. (Refs: [Cursor output-format](https://docs.cursor.com/en/cli/reference/output-format),
[headless CLI](https://cursor.com/docs/cli/headless),
[parameters](https://cursor.com/docs/cli/reference/parameters).)

**Tested here.** `cursor-agent -p --output-format stream-json` reaches a clean
auth wall: `Authentication required … set CURSOR_API_KEY`. With a key,
`run_probe.sh cursor` captures the gated tool-call events.

**Runner contract.** parse the stream-json; a gated tool-call event → enqueue;
the operator's answer maps to allow/deny for that call.

---

## The fourth signal: stdin-block (the net under all three)

An agent that ignores every structured path and just prints `Proceed? (y/n)` has
one physical tell: the process **blocks on stdin and output goes quiet**. The PTY
host can see that state directly (alive · reading · silent — UI-25's snapshot
already grabs the screen). When it trips, enqueue a *low-confidence* question
pointing at the transcript offset. It's the last-resort catch, not the primary
path — the three mechanisms above are.

## How this maps to Switchboard

- **One queue.** Every runner writes into one `agent_messages`-style store
  (`kind='question'`), tagged with host · session · task · the proposed action.
  Dozens of hosts → one ranked list, not per-terminal hunting.
- **Rank by blast radius.** `mission_graph` already knows how much each task
  blocks; a question on the keystone outranks a cosmetic one. FIFO is wrong at
  fleet scale.
- **A question parks one lane, never the fleet.** The asking session holds its
  lease in `waiting_on_operator`; `claim_next` keeps serving everything else; the
  map node goes amber `?`.
- **Default + deadline.** Each question carries a recommended default and an
  `auto_proceed_at`; silence becomes a logged answer so the fleet never
  deadlocks on an away operator.
- **Answer in place.** The operator opens the task panel (terminal in context),
  types the answer; the runner returns it as the approval decision / stdin reply.

Surfaces: toolbar bell (count) → ranked "Needs you" queue → amber `?` on the map
node → task-panel composer. All already in the wireframes; this doc is the
backend contract they render.
