# Cloud-Execution Adapter Contract

- **Status:** ADAPTER-17 shared contract
- **Date:** 2026-07-13
- **Scope:** Switchboard dispatch to vendor-hosted Claude Code, Codex, and Cursor sessions
- **Machine contract:**
  [`fixtures/cloud_execution_adapter.v1.json`](../fixtures/cloud_execution_adapter.v1.json)
- **Shared interface/evaluator:**
  [`adapters/cloud_execution.py`](../adapters/cloud_execution.py)

## 1. Decision

Cloud execution is a different backend for the existing wake lifecycle. Switchboard remains the
coordination and evidence authority, while the vendor supplies the compute and app-visible session.
The shared flow is:

```text
task + claim + wake
  -> provider preflight and Switchboard concurrency reservation
  -> outbound vendor trigger
  -> provider session readback
  -> bind session id + session URL to wake and runner_session
  -> provider polling/webhook + inbox/claim/evidence through scoped MCP
  -> PR webhook/reconcile
  -> Tally usage receipt
```

The trigger response is not enough to claim that work is running. `running` starts only after the
adapter reads back a provider session ID and a URL the operator can open in the vendor app. Provider
completion does not make the task `Done`; canonical PR/default-branch provenance still does.

This contract does **not** fall back to a self-hosted Agent Host. A cloud dispatch that cannot be
proved remains queued or visibly failed.

## 2. Current vendor truth

| Vendor adapter | Programmatic outbound trigger | App-visible receipt | V1 decision |
|---|---|---|---|
| `claude-code-cloud` | Conditional through the official `claude --cloud` CLI bridge. Each invocation creates a new cloud session from the current GitHub repo/branch. | Claude documents a remote session ID and a `claude.ai/code/...` transcript URL. | Implement a small launcher bridge; capture/read back both values before adoption. |
| `openai-codex-cloud` | Conditional through the official `codex cloud exec --env <ENV_ID> --branch <branch> <prompt>` CLI bridge. | The command returns `https://chatgpt.com/codex/tasks/<task-id>`; `codex cloud list --env ... --json` provides authoritative status readback. | Implemented by ADAPTER-19. Fail closed unless CLI auth, a repo-bound cloud environment, the canonical repo grant, scoped MCP bridge, and agent internet allowlist are all proven. |
| `cursor-background-agent` | Implemented through Cursor's beta Cloud Agents v1 API (`POST /v1/agents`), authenticated with a Cloud Agents API key and backed by an explicit GitHub repository grant. | The adapter captures and reads back the durable `bc-...` agent ID, `cursor.com/agents/...` URL, and latest run status. | Use `adapters/cursor/cloud_execution.py`; exact same-run resume fails closed, while a new run on the same durable agent is labeled a follow-up. |

Sources checked on 2026-07-13:

- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web) documents
  `claude --cloud`, GitHub access, cloud-session configuration, remote session IDs, transcript
  URLs, and cloud MCP behavior. The older `--remote` spelling remains a deprecated alias and is
  not used by this contract.
- [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)
  documents `codex cloud exec`, `--env`, `--branch`, and `--attempts`; the current OpenAI CLI
  source prints the app-visible task URL on successful creation. Environment discovery remains
  interactive, so automation must be configured with an environment ID.
- [Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environment)
  documents the two-phase runtime: secrets exist only during setup and agent internet is off by
  default. ADAPTER-19 therefore requires an explicit scoped-MCP environment bridge plus access to
  `plan.taikunai.com`; it never puts a raw token in the prompt.
- [Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints) documents the
  current run-based v1 create/read/follow-up/usage endpoints, API-key authentication, inline
  remote MCP servers, encrypted session environment variables, GitHub repositories, app-visible
  agent URLs, and explicit run statuses. The legacy v0 API is not used by ADAPTER-20.
- [Cursor pricing](https://docs.cursor.com/account/pricing) states that Cloud/Background Agents
  consume selected-model inference at API pricing. Cursor documents a provider ceiling of up to
  256 active agents per key; Switchboard deliberately keeps its cap at eight.

Vendor capabilities change. Each per-vendor implementation must pin a tested API/CLI version and
refresh its source links; the shared fixture records capability semantics, not an eternal endpoint
promise.

## 3. Shared adapter interface

Every vendor implementation supplies:

```python
class CloudExecutionAdapter(Protocol):
    vendor_id: str

    def preflight(self, dispatch: dict) -> dict: ...
    def trigger(self, dispatch: dict) -> dict: ...
    def get_session(self, provider_session_id: str) -> dict: ...
```

`preflight` proves authentication, entitlement, GitHub access, base branch visibility, project MCP
configuration, scoped token availability, and capacity. `trigger` performs exactly one idempotent
provider launch. `get_session` supplies authoritative adoption/status readback.

The provider-neutral dispatch envelope is:

```json
{
  "schema": "switchboard.cloud_dispatch.v1",
  "project": "switchboard",
  "task_id": "ADAPTER-17",
  "claim_id": "taskclaim_...",
  "wake_id": "wake_...",
  "dev_brief": "Read the selected task through Switchboard, implement it, test it, and open a PR.",
  "canonical_repo": "6th-Element-Labs/projectplanner",
  "branch": "cursor/adapter-17-cloud-execution",
  "continuity": "fresh_only",
  "mcp_access": {
    "endpoint": "https://plan.taikunai.com/mcp",
    "token_ref": "vault://switchboard/task/ADAPTER-17",
    "scopes": ["read:task", "write:claim", "write:evidence"],
    "expires_at": 1783890000
  }
}
```

The raw MCP token is never part of this envelope. `token_ref` names a secret the provider adapter
injects through its supported secret/environment mechanism. The credential is task-scoped,
short-lived, revocable, and insufficient to merge or mark work `Done`.

Claude cloud does not currently provide a dedicated secrets store; its documented environment
variables are visible to people who can edit that environment. The Claude adapter therefore uses
an especially short-lived single-task token, never a reusable production `PM_MCP_TOKEN`, and must
revoke it as soon as adoption fails or the provider session becomes terminal. Cursor documents
KMS-backed environment secrets, but receives the same least-privilege token shape.

V1 creates fresh cloud sessions. Cursor v1 can create another run on the same durable agent, which
retains conversation and workspace state. That is a **same-agent follow-up**, not an exact same-run
resume: terminal/cancelled runs are not resumed in place. An adapter must not relabel a follow-up,
fork, reconstructed context, or new task as exact resume.
The continuity vocabulary remains defined by
[`RUNTIME-WAKE-CAPABILITY-MATRIX.md`](RUNTIME-WAKE-CAPABILITY-MATRIX.md).

## 4. Outbound trigger

Before any provider call, the adapter must:

1. Confirm `project=switchboard` and the canonical repo is exactly
   `6th-Element-Labs/projectplanner`.
2. Confirm the requested branch is a provider/task branch and is not `main` or `master`.
3. Resolve a short-lived MCP credential reference with only `read:task`, `write:claim`, and
   `write:evidence` scope.
4. Prove provider auth/entitlement and GitHub repository write access.
5. Count non-terminal bound sessions for the vendor and reserve capacity under the Switchboard
   cap. The reservation and launch share an idempotency key derived from project/task/wake/vendor.
6. Send the development brief and fixed repo/branch/environment inputs through the vendor's
   supported transport.

Claude's adapter is a CLI bridge because `claude --cloud` is the documented launch surface. The
Codex adapter likewise uses the official `codex cloud exec` bridge. In both cases the bridge is a
short-lived trigger process and the coding compute remains vendor-hosted. Cursor uses
`POST /v1/agents`. Browser automation, reverse-engineered endpoints, local `codex exec`, and
Codex App Server are not acceptable hidden substitutes for cloud execution.

For Cursor, Switchboard first proves that the task branch is already pushed, then supplies it as
`repos[].startingRef` with `workOnCurrentBranch=true` and `autoCreatePR=true`. A deterministic
agent ID derived from project/task/wake makes a retry safe: Cursor's `409 agent_id_conflict`
causes readback of that exact agent rather than a second launch. The scoped Switchboard token is
resolved outside the dispatch envelope and injected only into the inline `taikun-plan` MCP header.

## 5. Adoption and binding receipt

The adapter may bind a cloud run only after provider readback returns both the stable session ID
and app-visible URL:

```json
{
  "schema": "switchboard.cloud_session_binding.v1",
  "wake_id": "wake_...",
  "task_id": "ADAPTER-17",
  "claim_id": "taskclaim_...",
  "vendor_id": "cursor-background-agent",
  "provider_session_id": "bc_...",
  "session_url": "https://cursor.com/agents/...",
  "runner_session_id": "cloud/cursor-background-agent/bc_...",
  "provider_status": "running",
  "bound_at": 1783890000
}
```

Switchboard stores the URL in runner-session metadata so the Dev tab can open the vendor app. The
runner session is vendor-managed and must not advertise local process actions such as PID health,
snapshot, or kill unless the provider adapter implements an equivalent API and proves it.

Concurrency is enforced on bound non-terminal sessions plus in-flight reservations:

| Vendor | Switchboard default | Provider signal |
|---|---:|---|
| Claude Code cloud | 4 | Account/workspace rate limit; no stronger fixed limit is assumed. |
| Codex cloud | 4 | Workspace usage limit; environment ID and repo access must already be configured. |
| Cursor Background Agents | 8 | Cursor documents up to 256 active agents per API key. |

Configuration uses a normalized variable such as
`PM_CLOUD_MAX_SESSIONS_CLAUDE_CODE_CLOUD` or the fixture default. The provider's larger allowance
never overrides the Switchboard cap.

## 6. Receipt and status path

The Dev-tab projection is intentionally small:

| Dev status | Evidence |
|---|---|
| `queued` | Wake pending/claimed, capacity reserved, or provider accepted a trigger but session readback is incomplete. |
| `running` | Complete binding receipt plus a current non-terminal provider readback. |
| `pr` | Canonical GitHub webhook/reconcile recorded a PR URL for the task. |
| `failed` | Auth/repo/trigger/readback/session failure remains visible and actionable. |

`pr` wins over a still-running provider session. A claimed wake stays `queued` until adoption; this
prevents a provider HTTP 202 from being presented as an active agent.

PR provenance reuses the existing GitHub webhook/reconcile path. Branch/head/PR evidence moves a
claim to `In Review`; only merge/default-branch provenance marks `Done`.

## 7. Tally usage

Cloud adapters report usage through the existing `report_usage`/Tally surface:

- Usage-metered provider/API: `source=agent_report`, `confidence=reported`; later billing exports
  may add `source=provider_reconcile`.
- Subscription-bundled usage: token counts may be reported when exposed, but per-task cost remains
  `0`/unknown. A subscription allocation cannot claim `confidence=exact`.
- Missing provider usage is a visible unknown receipt, not an invented estimate.

Every receipt includes vendor ID, provider session ID hash, task, claim, runner session, billing
mode, tokens when available, and the evidence/readback timestamp.

## 8. Fail-closed behavior

| Failure | Required result |
|---|---|
| Missing/invalid provider auth | `failed/missing_provider_setup`; no vendor call. |
| Repository not granted | `failed/missing_provider_setup`; identify `github_repo_grant`. |
| Default branch requested | `failed/invalid_dispatch_envelope`. |
| Raw MCP token in payload | `failed/invalid_dispatch_envelope`; never log the value. |
| Switchboard concurrency cap reached | `failed/provider_concurrency_cap_reached`; do not exceed the cap. |
| Vendor API/CLI error | `failed/vendor_api_error`; retain provider-safe error details. |
| Trigger accepted but session ID/URL absent | stay queued while bounded polling continues, then `failed/adoption_receipt_incomplete`. |
| Session unreadable/lost/expired | `failed/vendor_session_lost|expired`; revoke token and release capacity. |
| Unknown provider status | `failed/provider_status_unknown`; no optimistic mapping. |
| Codex CLI auth/environment/repo/MCP/network setup absent | `failed/missing_provider_setup`; include the missing requirement and provider-safe error. |

Retries reuse the same idempotency key. An operator may explicitly retry a terminal failure with a
new wake, but the adapter never silently creates a second provider session.

## 9. Implementation slices

1. Cursor adapter: **implemented by ADAPTER-20** against the run-based v1 API with account/repo/
   branch preflight, idempotent create, authoritative readback, explicit follow-up semantics, and
   token-usage receipts. A live launch still requires an operator-owned Cloud Agents API key,
   repository grant, pushed task branch, and scoped Switchboard token resolver.
2. Claude adapter: **implemented in ADAPTER-18** with PTY launch, exact-push/auth/MCP preflight,
   idempotent session ID/URL adoption, runner/wake binding, Dev-tab link, and honest subscription
   Tally receipt. See [`CLAUDE-CLOUD-EXECUTION.md`](CLAUDE-CLOUD-EXECUTION.md).
3. Codex adapter: **implemented by ADAPTER-19** via `adapters/codex/cloud_adapter.py`; configure a repo-bound environment ID,
   scoped MCP bridge, and agent internet allowlist; use the returned task URL plus JSON list
   readback as the adoption receipt. Do not use local App Server/SDK under the cloud label.
4. Add a cloud-session persistence surface and Dev-tab open action by extending runner-session
   metadata rather than creating a second session registry.
5. Add provider polling/webhook workers, capacity reservations, scoped-token revocation, and Tally
   reconciliation.
