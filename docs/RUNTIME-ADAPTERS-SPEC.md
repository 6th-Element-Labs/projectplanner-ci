# Runtime Adapters Spec - Switchboard agent packs

- **Status:** Draft v0.1
- **Date:** 2026-06-28
- **Product:** Switchboard
- **Protocol target:** `IXP-core` plus optional `+IRQ`, `+TXP`, and `+OXP` hooks
- **Purpose:** make the coordination protocol automatic inside real agent runtimes, so the
  operator does not depend on every prompt remembering to register, poll, claim, and release.

> MCP tools are necessary but not sufficient. The product works only when each runtime
> reliably performs the session lifecycle: register presence, drain inbox, claim resources,
> poll deltas, honor stop/redirect, report usage, and release on exit. Runtime adapters are
> the small pieces of glue that make that lifecycle the default path.

---

## 1. Product thesis

Switchboard is cross-LLM and cross-runtime only if agents can join from Claude Code, Codex,
Cursor, LangGraph, raw OpenAI loops, local scripts, and future cloud agents without a human
copying a protocol checklist into every prompt.

An adapter pack is the "load us first" kit for one runtime:

- credential and identity setup;
- `IXP-core` handshake automation;
- boundary polling for messages, signals, and deltas;
- resource-claim helpers around risky writes;
- advertised interrupt/control fidelity;
- optional spend/outcome reporting into Tally;
- a conformance smoke test proving the pack does the minimum.

The goal is not to own agent execution. The goal is to make Switchboard the coordination
control plane every runtime can safely call into.

In the open-core packaging, adapter packs are public adoption infrastructure. A team should be
able to inspect the adapter, run the conformance smoke test, and trust that the runtime really
does the Switchboard handshake. The paid product begins above that layer: hosted governance,
roles, entitlements, Tally analytics, managed runners, audit history, and operator policy.

---

## 2. Adapter contract

An adapter is conformant when it can perform this lifecycle without relying on the model to
remember it:

1. Build a stable `agent_id`.
2. Authenticate with a per-agent credential.
3. Call `get_working_agreement` and verify `protocol.version`.
4. Call `register_agent` with the adapter's `protocol` envelope.
5. Drain `inbox` and handle or ack pending signals.
6. Call `get_delta` from the last cursor.
7. Claim required resources before writes or task execution.
8. Call `pre_tool_check` before side-effectful tools when the runtime has a hook, wrapper, or
   managed runner boundary.
9. Heartbeat and poll at every supported boundary.
10. Release owned leases on normal completion.
11. Report completion, abandon, and usage where supported.
12. Call `merge_gate` before any authorized merge or merge request.
13. Advertise the runtime's actual control fidelity.

The adapter may be a hook bundle, SDK middleware, wrapper script, local daemon, library, or
MCP configuration. The packaging differs per runtime; the behavioral contract does not.

An adapter starts working only after its runtime process exists. The separate Agent Host layer
is responsible for keeping runtimes warm or waking one when a message/claim needs attention;
see [`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md).

When a runtime is launched by an Agent Host for code work, the preferred startup input is a
Switchboard-managed Work Session created by `create_managed_work_session`. The adapter should use
the returned branch, workspace path, session id, and session token as its execution boundary, then
pass `work_session_id` into `claim_task`, `pre_tool_check`, `complete_claim`, and `merge_gate`.
Adapters should not silently continue in a shared checkout if managed workspace creation fails.

---

## 3. Universal session lifecycle

### 3.1 Required configuration

Every adapter pack must support these values by environment variable, config file, or runtime
settings:

| Setting | Required | Meaning |
|---|---:|---|
| `SWITCHBOARD_URL` | yes | Base URL for REST or MCP transport |
| `SWITCHBOARD_TOKEN` | yes | Bearer credential for the authenticated principal |
| `SWITCHBOARD_PROJECT` | yes | Workspace/project boundary |
| `SWITCHBOARD_AGENT_ID` | optional | Explicit session id; generated if absent |
| `SWITCHBOARD_RUNTIME` | yes | Runtime name, e.g. `claude-code`, `codex`, `cursor` |
| `SWITCHBOARD_MODEL` | optional | Current model name when known |
| `SWITCHBOARD_LANE` | optional | Board lane/workstream |
| `SWITCHBOARD_TASK_ID` | optional | Active task |
| `SWITCHBOARD_POLL_INTERVAL_S` | optional | Advisory background poll interval |
| `SWITCHBOARD_CONTROL_FIDELITY` | optional | Declared or detected interrupt tier |

Generated `agent_id` format:

```text
<runtime>/<lane-or-task>#<short-session-id>
```

Examples:

```text
claude-code/ENGINE-11#a7c4
codex/CHART-8#b12e
cursor/REVIEW#9f31
langgraph/triage#run-487
openai-loop/RFP-12#20260628T1512
```

### 3.2 Startup sequence

On session start:

```text
detect runtime/model/task/lane
derive agent_id
authenticate
get_working_agreement(project)
fail_closed_if_protocol_incompatible
register_agent(project, agent_id, runtime, model, lane, task, ttl_s, protocol)
inbox(to_agent=agent_id, unacked=true)
ack or surface pending heads_up/redirect/stop
get_delta(since_cursor=last_cursor)
advertise control fidelity
```

If a startup `stop` is pending, the adapter must prevent work from beginning when it has a
hook-level or runner-level enforcement mechanism. If it only has advisory polling, it must
surface the stop prominently and ack only after the agent or wrapper confirms handling.
If no runtime is alive, no adapter can run this sequence; Switchboard must rely on an Agent Host
wake intent or report that no eligible host is online.

### 3.3 Boundary sequence

At every supported boundary:

```text
heartbeat(agent_id)
inbox(to_agent=agent_id, unacked=true, priority_desc=true)
if stop or redirect:
  save state when supported
  deny/redirect/kill according to control fidelity
  ack with handling status
get_delta(since_cursor)
refresh or release leases as needed
optionally report usage
```

Boundaries include pre-tool hooks, post-tool hooks, SDK step boundaries, graph node
boundaries, command wrappers, model-turn boundaries, or timed background polls.

### 3.3.1 Pre-tool check

Adapters with a tool boundary call `pre_tool_check` before file writes, git commands, PR
creation, `complete_claim`, merge, server start/kill, or other external effects:

```text
pre_tool_check(project, task_id, agent_id, work_session_id, tool_name, tool_input, action?)
→ decision: allow | warn | deny
→ remediation[]
```

The server validates the active Work Session, task/agent binding, dirty/conflict state, branch
shape, and file lease conflicts. Hook-capable runtimes must block locally on `deny`. Advisory
runtimes must surface the warning/deny and mark reduced control fidelity if the human/model can
still ignore it. Denied shared-token writes are audited as `principal.unbound_write`; denied
unsafe sessions are audited as `work_session.unsafe_session`.

### 3.4 Exit sequence

On normal exit:

```text
run repo preflight and refresh the Work Session
call complete_claim with branch, head_sha, PR/push/offline proof, tests, and git diff --check
report usage if available
release all owned leases
ack any handled terminal signals
write final heartbeat/state
```

For `code_strict` sessions, Switchboard refuses `complete_claim` when the Work Session is dirty
without an explicit allowance, has conflict markers, has a mismatched branch/head SHA, lacks
PR/push/offline proof, or does not record tests and `git diff --check`. A refusal keeps the claim
active and returns a typed `work_session_gate` failure for repair-and-retry.

### 3.4.1 Pre-merge sequence

Adapters that can merge or request merges must call `merge_gate` after `complete_claim` has moved
the task to `In Review` and before running `gh pr merge`, merge-queue enqueue, or an equivalent
merge command:

```text
merge_gate(
  project,
  task_id,
  agent_id,
  claim_id,
  work_session_id,
  pr_url or pr_number,
  repo,
  target_branch,
  branch,
  head_sha,
  status_contexts_json,
  require_work_session=true
)
→ status: passed | blocked
→ findings[]
```

Hook-capable adapters must deny merge commands when the gate returns `blocked`. Advisory adapters
must surface the blocking findings and avoid claiming the merge is safe. A passing gate means the
canonical PR is ready to merge; it does not mark the task `Done`. `Done` remains controlled only by
GitHub webhook/reconcile evidence that the intended default branch contains the merge provenance.
Public CI/mirror repos can satisfy required status evidence but cannot pass as the code merge repo.

On crash or forced kill, TTL expiry is the fallback cleanup path. Runner-aware adapters
should emit a final "killed" or "crashed" status from the supervisor process when possible.

---

## 4. Adapter pack contents

Each runtime pack should ship:

| File or component | Purpose |
|---|---|
| `README.md` | install and runtime-specific behavior |
| `switchboard.config.example.*` | minimal config/env example |
| `adapter` library or script | common lifecycle calls |
| hook/middleware/wrapper | runtime integration point |
| `conformance.*` | smoke test against a local Switchboard |
| `control-profile.json` | advertised control fidelity capabilities |
| examples | smallest working session |

The pack should be small enough to inspect. It should not hide a new agent framework inside
the adapter.

---

## 5. Control fidelity advertisement

Every `register_agent` call from an adapter must include a `protocol` object matching the
version it verified from `get_working_agreement`:

```json
{
  "version": "ixp.v1",
  "profile": "p0-dogfood",
  "profiles": {"ixp_core": "1.0", "txp_dispatch": "0.1"}
}
```

The response includes `protocol_compatibility`. If it is false, or if the working agreement
advertises a known-incompatible protocol, the adapter must fail closed before claiming work.

Every `register_agent` call from an adapter must include a `control` object:

```json
{
  "mode": "hook_deny",
  "poll": true,
  "poll_interval_s": 10,
  "hook_deny": true,
  "runner_kill": false,
  "state_save": "adapter",
  "max_signal_latency": "next_tool_call",
  "verified_by": "adapter-smoke:claude-code-pretool-v0.1"
}
```

Allowed `mode` values:

| Mode | Meaning |
|---|---|
| `observe_only` | Registers presence but cannot reliably interrupt |
| `advisory_poll` | Polls and surfaces messages; cannot deny a pending action |
| `hook_deny` | Can block a tool/action at the next boundary |
| `runner_kill` | Can terminate the managed process/session out of band |
| `managed` | Supports hook deny plus runner kill and supervised restart/resume |

The adapter must not overclaim. If Codex, Cursor, or any runtime cannot currently expose a
pre-tool deny hook, the adapter advertises `advisory_poll` or `runner_kill`, not `hook_deny`.

---

## 6. Resource claim helpers

Adapters should make claims ergonomic because humans and models will forget exact lease
calls under pressure.

Minimum helpers:

- `claim_files(paths[], task_id?)`
- `claim_port(port, task_id?)`
- `claim_worktree(path, task_id?)`
- `create_managed_work_session(task_id, storage_mode?, workspace_root?)`
- `archive_work_session_workspace(work_session_id, remove_workspace?)`
- `release_all()`
- `with_claim(resource_type, names[], fn)`

Hook-capable adapters should detect high-risk write tools and require a claim first:

| Action | Suggested resource |
|---|---|
| edit repo file | `file:<repo-relative-path>` |
| run dev server | `port:<port>` |
| rebuild shared binary | `binary:<name>` or `build_dir:<path>` |
| switch branch | `branch:<branch>` |
| mutate shared workspace | `worktree:<path>` |

Enforcement may begin as warnings, but hook-capable packs should eventually deny writes that
lack a valid lease for configured protected resources.

---

## 7. Runtime pack targets

### 7.1 Claude Code adapter

Integration shape:

- MCP server config for tool access.
- `CLAUDE.md`/runtime instruction snippet for human-readable behavior.
- hook bundle for pre-tool and post-tool boundaries.
- optional launcher wrapper that owns process metadata and release-on-exit.

Expected fidelity:

- `hook_deny` when a pre-tool hook can deny pending tool calls.
- `runner_kill` only when launched under a Switchboard-managed runner.

Required behaviors:

- pre-tool: heartbeat, inbox, stop/redirect handling, resource-claim check;
- post-tool: delta cursor update, usage report when available, heartbeat;
- stop: deny pending tool with the stop reason and ack;
- redirect: deny pending tool or inject/surface new instruction according to runtime support.

### 7.2 Codex adapter

Integration shape:

- MCP config when available, REST fallback always available.
- wrapper script or local daemon that registers the session and supervises command execution.
- instruction snippet for the agent-facing protocol.
- optional CLI/plugin hook if the host exposes one.

Expected fidelity:

- default: `advisory_poll`;
- upgrade: `hook_deny` only after a real pre-tool/pre-command deny surface is verified;
- `runner_kill` when the adapter starts and owns the process.

Required behaviors:

- register on session start;
- poll before long-running shell/tool work when the integration point exists;
- surface pending stop/redirect in the session;
- release leases on wrapper-managed exit;
- never claim mid-token or hook-level guarantees unless verified in the runtime.

### 7.3 Cursor adapter

Integration shape:

- MCP config for agent/tool access.
- project rules snippet for the handshake.
- optional extension or command wrapper for boundary polling.

Expected fidelity:

- `advisory_poll` with MCP-only usage;
- `hook_deny` only if an extension can reliably intercept tool/file operations;
- `runner_kill` for managed background agents or external process supervisors.

Required behaviors:

- register active editing/review sessions;
- claim files before edits where file-operation interception exists;
- surface directed messages in the IDE/workspace;
- send usage and outcome reports when Cursor exposes token/cost metadata or the user enters
  estimates.

### 7.4 LangGraph adapter

Integration shape:

- Python middleware or node wrapper around graph steps.
- checkpointer integration for state save/restore.
- REST client library; MCP optional.

Expected fidelity:

- `hook_deny` at graph node/tool boundaries;
- `managed` when the graph runner is owned by Switchboard and can cancel runs.

Required behaviors:

- register one `agent_id` per graph run or worker;
- call inbox/delta before node execution;
- map `redirect` to graph state update or supervisor route;
- map `stop` to cancellation before the next node/tool;
- report usage from model callbacks;
- record outcomes when a run completes verified work.

Implementation:

- Pack: [`adapters/langgraph/`](../adapters/langgraph/)
- Entry points: `LangGraphSwitchboardAdapter.on_graph_start()`,
  `wrap_node(node_name)`, `guard_tool(tool_name, args)`, and
  `run_claim_loop(compiled_graph, lanes=...)`.
- Smoke:

```bash
python3 test_langgraph_adapter.py
python3 adapters/langgraph/langgraph_adapter.py conformance --json
```

The adapter imports without LangGraph installed. A real LangGraph app supplies the compiled graph
or node callable; Switchboard supplies only the coordination boundary.

### 7.5 Raw OpenAI loop adapter

Integration shape:

- TypeScript and Python libraries wrapping the model/tool loop.
- REST-first transport.

Expected fidelity:

- `hook_deny` for tool execution inside the loop because the adapter owns the dispatch point;
- `runner_kill` only when the parent process is supervised.

Required behaviors:

- register at loop start;
- check inbox before each model call and before each tool dispatch;
- deny tool execution on stop;
- convert redirect into a new developer/user instruction at the next turn;
- report usage from API responses with request ids;
- link usage to task/outcome metadata.

### 7.6 Generic REST adapter

Integration shape:

- minimal shell/Python/Node clients for runtimes with no MCP support.

Expected fidelity:

- `advisory_poll` unless embedded around a real execution boundary.

Required behaviors:

- expose simple commands: `switchboard register`, `switchboard inbox`, `switchboard claim`,
  `switchboard release`, `switchboard report-usage`;
- return deterministic JSON for easy model consumption;
- never require a runtime-specific plugin.

---

## 8. Adapter conformance

Each pack must pass a smoke test against a clean local Switchboard:

1. Invalid token cannot register.
2. Valid token registers presence with correct runtime/model.
3. Startup drains an existing inbox message and acks it.
4. Adapter claims and releases a test file.
5. Adapter persists and reuses a delta cursor.
6. Adapter advertises truthful control fidelity.
7. A `stop` signal is handled according to the advertised fidelity.
8. Exit releases leases or proves TTL cleanup.
9. Optional: usage report lands in Tally with `source=agent_report`.

The result should be written as an adapter capability statement:

```json
{
  "adapter": "claude-code",
  "version": "0.1.0",
  "ixp_core": true,
  "control_mode": "hook_deny",
  "txp_claim_next": false,
  "tally_usage_report": true,
  "verified_at": "2026-06-28T00:00:00Z"
}
```

The shared fixture lives in [`adapters/conformance.py`](../adapters/conformance.py). The
reference command uses isolated temporary SQLite databases and requires no background server:

```bash
python3 adapters/conformance.py
```

Adapter authors should keep the same checks and swap only the client transport. That lets a
Claude Code, Codex, Cursor, LangGraph, or raw-loop pack prove the same P0 behavior instead of
shipping runtime-specific smoke tests with different meanings.

---

## 9. Implementation order

1. Shared adapter SDK: auth, REST client, deterministic JSON, lifecycle helpers.
2. Raw OpenAI loop adapter: easiest full-control reference.
3. Claude Code adapter: strongest current hook story and most urgent customer surface.
4. Codex adapter: REST/MCP plus truthful fidelity discovery.
5. LangGraph middleware: proves Switchboard composes with orchestration frameworks.
6. Cursor adapter: IDE/team adoption path.
7. Pack registry UI: show connected sessions and their verified fidelity.

---

## 10. Exit criteria

Runtime adapters are product-ready when:

- two different LLM runtimes can start from clean config and perform the full handshake;
- at least one hook-capable adapter proves `stop` is denied before the next tool action;
- every active session displays runtime, task, heartbeat, leases, and control fidelity;
- a missing adapter is visible as a lower-fidelity session, not invisible risk;
- adapter smoke tests run in CI for the shared SDK and at least one reference pack;
- docs say exactly which guarantees each runtime has today.
