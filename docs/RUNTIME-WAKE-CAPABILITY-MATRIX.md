# Runtime Wake and Conversation Continuity Matrix

- **Status:** ADAPTER-12 contract
- **Date:** 2026-07-13
- **Scope:** Claude Code, Codex CLI/app, Cursor, LangGraph, OpenAI and Anthropic
  loops, and generic shell/CI runners
- **Machine-readable contract:**
  [`fixtures/runtime_wake_capabilities.v1.json`](../fixtures/runtime_wake_capabilities.v1.json)
- **Evaluator:** [`adapters/wake_capabilities.py`](../adapters/wake_capabilities.py)

## 1. Decision

Switchboard supports two different wake outcomes. They must never be collapsed into one
"resumed" status:

1. **Fresh start:** start a new runtime or worker process, bind it to durable Switchboard state,
   register it, drain its inbox, and continue the task without claiming that vendor conversation
   history survived.
2. **Conversation resume:** reopen a recorded vendor session/thread or a checkpointed logical
   conversation, then prove which handle was resumed.

There are four continuity modes:

| Mode | Meaning |
|---|---|
| `exact_vendor_session` | Reopen the provider/runtime's recorded conversation or thread ID. |
| `checkpoint_resume` | Rehydrate a deterministic worker/graph checkpoint. |
| `reconstructed_history` | Start a new provider call/process with externally persisted messages and tool state. |
| `fresh_switchboard_state` | Start a new conversation from task, claim, inbox, Work Session, git, and evidence state held by Switchboard. |

`fresh_switchboard_state` is always the portability floor. It is useful continuity, but it is not
the same conversation.

## 2. Researched capability matrix

The "runtime support" column describes the vendor or framework capability. "Switchboard path"
describes what the Agent Host must do. A capability is not product-ready until both sides exist
and a live receipt proves the requested continuity mode.

| Runtime ID | Fresh start | Conversation/session resume | Required handle and Switchboard path |
|---|---|---|---|
| `claude-code` | Yes: launch a new supervised CLI process and bootstrap from Switchboard. | Exact vendor session is supported by `claude --resume <session-id>` (or `--continue` for the latest local conversation). | Capture the Claude session ID from structured output, bind it to host + repo + task, validate the local session store, then launch an allowlisted resume profile. [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage) |
| `codex-cli` | Yes: current Codex supervisor starts a new managed process. | Exact vendor session is supported by `codex resume <session-id>` and non-interactive `codex exec resume <session-id>`. | Persist the Codex session UUID/name and originating host/cwd, verify it is locally readable, then invoke a resume profile. Do not use `--last` in automation because cwd-relative recency is ambiguous. [Codex developer commands](https://developers.openai.com/codex/cli/reference) |
| `codex-app` | Yes through App Server `thread/start`. | Exact thread resume is supported through `thread/resume`; `thread/fork` is a third, explicitly different outcome. | Persist `thread.id`, connect to the authenticated host-side App Server, call `thread/resume`, then start a turn. Paginated-history resume currently fails closed. [Codex App Server](https://developers.openai.com/codex/app-server) |
| `cursor-agent` | Yes with a registered, allowlisted Cursor CLI launcher. | Exact saved chat resume is supported by `cursor-agent --resume=<chat-id>` or `cursor-agent resume` for the latest. | Persist the chat ID and host/cwd; automation must use the explicit ID, not "latest". [Cursor CLI overview](https://docs.cursor.com/en/cli/overview) |
| `langgraph-worker` | Yes: start a new worker and new `thread_id`. | Logical thread/checkpoint resume is first-class when a durable checkpointer and `thread_id` exist. | Persist `thread_id`, optional `checkpoint_id`, graph version, and checkpoint store locator. Refuse resume across an incompatible graph version. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) |
| `openai-responses-loop` | Yes: start a new loop and new OpenAI Conversation or send full context. | Exact provider conversation is supported with a durable Conversations API ID; a shorter chain can use `previous_response_id`. | Prefer `conversation_id` for long-lived work. Persist and read back the conversation before wake. Response objects have a default retention window, while Conversation items are durable until managed under their own lifecycle. [OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state) |
| `anthropic-messages-loop` | Yes: start a new loop from Switchboard state. | Raw Messages API loops reconstruct continuity by storing and resending message/tool history; this is not a provider session ID. | Encrypt the external message/tool checkpoint, version it, and label the result `reconstructed_history`. Anthropic describes the raw-loop responsibility as maintaining and passing the history array on every turn. [Anthropic Managed Agents migration](https://platform.claude.com/docs/en/managed-agents/migration) |
| `anthropic-managed-agent` | Yes: create a new managed session. | Exact server-side session resume is supported by sending a new event to the existing session ID; conversation history persists and an idle sandbox is checkpointed. | Persist `session_id`, agent/environment versions, and checkpoint availability. This API currently requires Anthropic's managed-agents beta header. [Managed session events](https://platform.claude.com/docs/en/managed-agents/events-and-streaming) |
| `shell-ci-runner` | Yes: dispatch a new job/process from a fixed template and Switchboard task state. | No same-process conversation exists. A rerun is a new execution that may reuse the same commit/ref and external artifacts. | Persist run/job ID, commit SHA, artifact hashes, and provider control handle; label restart `fresh_switchboard_state`, never exact resume. [GitHub reruns](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs), [workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts) |

The local acceptance environment also confirms `codex-cli 0.144.0-alpha.4` exposes
`codex resume [SESSION_ID]`, `codex exec resume [SESSION_ID]`, and `codex fork [SESSION_ID]`.
That local observation is supporting evidence; the official command reference above is the public
contract.

## 3. Current Switchboard truth

ADAPTER-13 proved active delivery plus acknowledgement, mailbox-only unreachable delivery, and a
message-only Claude-compatible wake through an eligible Agent Host. ADAPTER-14 proved no-host
failure, a new Codex managed process, and a checkpointed raw API-loop resume. Those pilots did not
attempt the vendor resume commands now documented above.

Therefore the current product state is:

- **Fresh start:** implemented in the generic Agent Host/supervisor path for registered launchers.
- **Checkpoint resume:** contract-proven for a raw API loop and architecturally available for
  LangGraph.
- **Exact vendor session resume:** vendor-supported for Claude Code, Codex, Cursor, OpenAI
  Conversations, and Anthropic Managed Agents, but not yet wired to the Switchboard wake executor.
- **Codex app thread resume:** supported by App Server, but Switchboard does not yet operate a
  host-side App Server bridge.

## 4. Status vocabularies

### `delivery_status`

| Value | Meaning |
|---|---|
| `active` | A non-stale agent registration exists. Mailbox handling still requires poll/ack proof. |
| `unreachable` | The target is missing or stale. The message may be stored, but runtime delivery is not proved. |
| `identity_unbound` | The write/runtime identity is not bound strongly enough to accept delivery or takeover claims. |

The richer delivery receipt may use `active_session`, `supervised_wake_available`,
`wake_queue_available`, `dormant_registered_host`, `wake_queued`, `wake_claimed`, or
`mailbox_only`. These modes describe mailbox/presence/wake state; only acknowledgement proves the
recipient handled the message.

### `wake_status`

| Value | Meaning |
|---|---|
| `pending` | Durable intent exists and awaits an eligible host. |
| `claimed` | One host owns the launch/resume attempt. |
| `completed` | The requested runtime registered or another explicit ready-state receipt was verified. |
| `failed` | Launch/resume could not satisfy the requested policy. |
| `cancelled` | Operator, dedupe, expiry, or supersession stopped the intent. |

A completed wake also needs a continuity receipt. `completed` by itself does not say whether the
host resumed an old conversation or started fresh.

## 5. Continuity policy

Every wake that can start work carries one policy:

| Policy | Behavior |
|---|---|
| `resume_required` | Resume the named handle or fail. Never silently start fresh. |
| `resume_preferred` | Validate and attempt resume first. A fresh start is allowed only after a typed, audited `resume_fallback` receipt. |
| `fresh_only` | Ignore old conversation handles and start from durable Switchboard state. |

Recommended default: `resume_preferred` for human-directed follow-ups and `fresh_only` for clean
CI/reviewer workers. Use `resume_required` only when exact conversation continuity is part of the
task's correctness contract.

Example wake selector and policy:

```json
{
  "selector": {
    "runtime": "codex",
    "agent_id": "codex/ADAPTER-12",
    "lane": "ADAPTER"
  },
  "policy": {
    "continuity": "resume_preferred",
    "runtime_handle_id": "rth_01J...",
    "start_if_absent": true,
    "reuse_existing": true
  }
}
```

## 6. Runtime handle contract

Switchboard needs a typed runtime-handle registry. The handle is a routing capability, not freeform
metadata:

```json
{
  "schema": "switchboard.runtime_handle.v1",
  "runtime_handle_id": "rth_01J...",
  "project": "switchboard",
  "task_id": "ADAPTER-12",
  "agent_id": "codex/ADAPTER-12",
  "runtime": "codex-cli",
  "handle_type": "codex_session_id",
  "handle_ref": "encrypted-or-host-local-reference",
  "host_id": "host/steve-mbp",
  "repo": "6th-Element-Labs/projectplanner",
  "cwd_fingerprint": "sha256:...",
  "runtime_version": "0.144.0-alpha.4",
  "state_version": "adapter12-v1",
  "status": "available",
  "last_verified_at": 1783884000
}
```

Allowed `handle_type` values initially:

- `claude_code_session_id`
- `codex_session_id`
- `codex_thread_id`
- `cursor_chat_id`
- `langgraph_thread_id` plus optional checkpoint ID
- `openai_conversation_id`
- `anthropic_managed_session_id`
- `external_history_checkpoint`
- `ci_run_id`

`handle_ref` should normally be encrypted at rest or be a host-local opaque reference. Public
task activity receives a hash and type, not the raw ID.

## 7. Agent Host algorithm

```text
claim wake
  -> resolve requested continuity policy
  -> if fresh_only: launch fresh profile
  -> otherwise load typed runtime handle
  -> verify project/task/agent/runtime/host/repo/cwd ownership
  -> verify handle exists and runtime/checkpoint version is compatible
  -> invoke fixed resume profile (never a command from the wake payload)
  -> wait for registration with the expected task and continuity handle
  -> child drains inbox and acknowledges the wake-specific message
  -> complete wake with continuity receipt
  -> if resume_preferred failed and fresh is allowed:
       emit visible resume_fallback
       launch fresh profile
       complete with actual_mode=fresh_switchboard_state
```

Required completion receipt:

```json
{
  "schema": "switchboard.runtime_continuity_receipt.v1",
  "wake_id": "wake_01J...",
  "requested_policy": "resume_preferred",
  "requested_handle_id": "rth_01J...",
  "actual_mode": "exact_vendor_session",
  "actual_handle_hash": "sha256:...",
  "runner_session_id": "run_01J...",
  "runtime_registered": true,
  "inbox_drained": true,
  "fallback": null
}
```

## 8. Fail-closed rules

- Unknown runtime or capability: reject with `invalid_input`.
- `resume_required` without a handle: `failed/resume_handle_missing`.
- Handle owned by another project/task/host: `failed/resume_handle_scope_mismatch`.
- Missing local session/checkpoint: `failed/resume_state_unavailable`.
- Runtime or graph version mismatch: `failed/resume_version_incompatible`.
- Resume process starts but registers a different/new handle: fail the resume attempt; do not
  relabel it exact.
- No eligible host: keep `pending` only when policy explicitly says wait; otherwise fail with
  `no_eligible_host`.
- Spawn without runtime registration: not completed.
- Mailbox insertion without acknowledgement: not delivered.
- Fresh fallback without an audited `resume_fallback` receipt: `hidden_fallback` violation.

The executable fixture test removes every declared requirement from every runtime/capability and
asserts that the evaluator denies the operation.

## 9. Security constraints

- Runtime handles can reveal local history or act like routing credentials. Scope them to project,
  task, agent, host, repo, and cwd; encrypt or tokenize them at rest.
- Resume launchers are host-owned templates. A wake payload cannot supply CLI flags or shell.
- Read back a provider thread/session before use where the provider exposes that operation.
- Record a handle hash in audit; keep the raw provider ID out of task comments and public logs.
- Preserve the permission/sandbox profile on resume. A conversation ID never grants broader tool
  authority.
- Reject resume when the old session's repo/cwd or policy profile conflicts with the current task.
- Retention is explicit: provider session lifetime, local session-store cleanup, checkpoint TTL,
  and Switchboard handle expiry must be independently visible.

## 10. Delivery plan

1. **ADAPTER-12 (this contract):** publish the researched matrix, machine-readable fixture,
   evaluator, and negative tests.
2. **Handle registry:** add `runtime_handles`, scoped create/read/revoke tools, encrypted/opaque
   references, and continuity policy fields on wake intents.
3. **Host profiles:** implement explicit `fresh` and `resume` profiles for Claude Code, Codex CLI,
   Codex App Server, Cursor CLI, LangGraph, OpenAI Conversations, and Anthropic sessions/history.
4. **Receipts:** require `runtime_continuity_receipt.v1` before a wake can claim exact or
   checkpoint continuity.
5. **Live proof:** for each runtime, run both a fresh wake and a resume wake, then run the missing,
   stale, wrong-host, wrong-repo, and incompatible-version cases.

The implementation order should start with Codex CLI and Claude Code because their explicit resume
commands are simple host profiles, then Codex App Server and Cursor, then provider/framework
session stores. Generic shell/CI remains fresh-only by design.
