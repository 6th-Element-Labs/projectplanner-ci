# OpenAI Codex Cloud Adapter

- **Task:** ADAPTER-19
- **Transport:** official Codex CLI cloud bridge
- **Vendor runtime:** `openai-codex-cloud`
- **Switchboard selector runtime:** `codex`
- **Implementation:** [`adapters/codex/cloud_adapter.py`](../adapters/codex/cloud_adapter.py)

## Supported flow

Switchboard creates an explicit `policy.mode=cloud_execution` wake. An eligible bridge host runs:

```text
codex cloud exec --env <ENV_ID> --attempts 1 --branch codex/<task> <dev-brief>
```

The CLI returns an app-visible `https://chatgpt.com/codex/tasks/<task-id>` URL. The adapter then
uses `codex cloud list --env <ENV_ID> --json` to read the task back before it binds:

```text
wake -> cloud/openai-codex-cloud/<task-id> runner_session -> task session_url
```

The bridge process runs on an Agent Host, but coding runs in the OpenAI-managed cloud environment.
The adapter never substitutes local `codex exec`, App Server, browser automation, or an
undocumented ChatGPT endpoint.

## Required host and environment setup

The bridge host needs an authenticated Codex CLI and these settings:

```bash
PM_RUNTIME=codex
PM_AGENT_HOST_ALLOW_WORK=1
PM_HOST_LANES=ADAPTER
PM_CODEX_CLOUD_ENVIRONMENT_ID=<opaque Codex cloud environment id>
PM_CODEX_CLOUD_MCP_CONFIGURED=1
PM_CODEX_CLOUD_AGENT_INTERNET=1
PM_CODEX_CLOUD_MCP_TOKEN_REF=switchboard://scoped-token/<task>
```

The selected Codex cloud environment must be bound to
`6th-Element-Labs/projectplanner`, permit the requested `codex/<task>` branch, and allow agent
network access to `plan.taikunai.com`. `PM_CODEX_CLOUD_MCP_CONFIGURED=1` is an operator assertion
that the environment exposes a scoped Switchboard MCP bridge without placing the raw token in the
task prompt.

That assertion is necessary because Codex cloud secrets are available to setup scripts and
removed before the agent phase. A reusable `PM_MCP_TOKEN` copied into the prompt or committed into
the checkout is forbidden. If the environment cannot provide a scoped MCP path during the agent
phase, dispatch fails `missing_provider_setup`.

## Provider status and receipts

| Codex status | Switchboard Dev status | Meaning |
|---|---|---|
| `pending` | `running` | Complete ID+URL receipt exists and Codex is working. |
| `ready` | `running` | Diff/result is ready; canonical PR evidence has not landed yet. |
| `applied` | `running` | Result was applied; merge provenance still controls Done. |
| `error` | `failed` | Provider failure remains visible. |
| PR URL in task git state | `pr` | Existing GitHub reconcile path owns the transition. |

Cloud completion never marks the task Done. Only the canonical PR/default-branch provenance path
does that.

## Pricing and Tally

Official Codex pricing does not expose an exact per-task USD value in the cloud CLI receipt. The
current pricing page describes plan/credit limits and shows no API-key cloud-task price for the
listed models. The adapter therefore writes one idempotent Tally receipt with:

```json
{
  "source": "agent_report",
  "confidence": "unknown",
  "billing_mode": "subscription",
  "cost_usd": 0
}
```

A later provider billing export may reconcile the amount. Switchboard must not invent token counts
or allocate an exact subscription cost.

## Verified product boundary (2026-07-13)

The installed `codex-cli 0.144.0-alpha.4` exposes `cloud exec`, `status`, `list --json`, `diff`, and
`apply`. A live preflight on this workstation successfully authenticated but returned:

```text
Error: no cloud environments are available for this workspace
```

The interactive environment picker also reported `repo_not_accessible` for
`6th-Element-Labs/projectplanner`. This is an external setup blocker, not an adapter fallback:
until the canonical repo is granted and a cloud environment exists, Switchboard records
`missing_provider_setup` with `codex_cloud_environment_id` and `github_repo_grant` missing and does
not create a local run.

Official references:

- [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)
- [Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environment)
- [Codex agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security#sandbox-and-approvals)
- [Codex pricing](https://learn.chatgpt.com/docs/pricing#what-are-the-usage-limits-for-my-plan)
