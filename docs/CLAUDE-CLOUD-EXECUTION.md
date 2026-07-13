# Claude Code Cloud Execution Adapter

- **Task:** ADAPTER-18
- **Contract:** `switchboard.cloud_execution_adapter.v1`
- **Vendor:** Anthropic Claude Code on the web
- **Trigger:** `claude --cloud <dev-brief>` through an interactive PTY
- **Compute boundary:** Anthropic-hosted VM; the Switchboard host is trigger/receipt coordination only
- **Sources checked:** 2026-07-13

## Current provider truth

Anthropic's supported programmatic launch surface is the Claude Code CLI bridge:

```bash
claude --cloud "Fix the authentication bug and open a PR"
```

The flag creates a new hosted session for the current pushed repository and branch. The older
`--remote` spelling remains a deprecated alias. The CLI requires an interactive terminal; a
non-TTY invocation exits rather than silently creating local work. `PtyCloudLauncher` therefore
allocates a pseudo-terminal and never invokes a shell.

Official sources:

- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web) — launch,
  GitHub access, hosted environment, session URL/ID, security, limits, and pricing behavior.
- [Claude Code MCP](https://code.claude.com/docs/en/mcp) — project `.mcp.json` and environment
  variable expansion.
- [Pro/Max access and billing](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
  — subscription authentication, shared limits, and optional API-credit billing.
- [Claude plan prices](https://support.claude.com/en/articles/11049762-choose-a-claude-plan) — current
  published plan prices.

### Access

The trigger host needs:

1. Claude Code CLI **2.1.195 or newer**. Anthropic documents the current cloud provisioning
   checklist from that version.
2. An active `claude.ai` subscription login. API-key, Bedrock, Vertex, and Foundry auth are not
   treated as subscription cloud-session auth.
3. GitHub access through the Claude GitHub App or `/web-setup`. The selected account must be able
   to clone and push `6th-Element-Labs/projectplanner`.
4. A selected Claude cloud environment whose network policy can reach GitHub and
   `https://plan.taikunai.com/mcp`.
5. A provider-side `SWITCHBOARD_TOKEN` secret. `.mcp.json` references the variable; it never
   contains a literal credential.

Cloud sessions run in fresh Anthropic-managed VMs. Repository configuration is available only
when committed; local user MCP settings are not copied. Anthropic currently documents no dedicated
cloud secret store, so environment values are visible to people who can edit that Claude
environment. Use a project-scoped, revocable principal and rotate it independently of the repo.
Task/expiry-bounded token vending remains follow-on hardening; a reusable production-admin token
is not an acceptable substitute.

### Session receipt

Each hosted session exposes `CLAUDE_CODE_REMOTE_SESSION_ID` with a `cse_` prefix. Its app URL uses
the same suffix with a `session_` prefix:

```text
cse_abc123  ->  https://claude.ai/code/session_abc123
```

The adapter adopts a provider request only after reading a matching URL from the PTY output. It
then stores:

```json
{
  "vendor_id": "claude-code-cloud",
  "provider_session_id": "cse_abc123",
  "session_url": "https://claude.ai/code/session_abc123",
  "runner_session_id": "cloud/claude-code-cloud/cse_abc123",
  "provider_status": "running"
}
```

There is no documented non-interactive cloud-session status API. `/tasks` is an interactive CLI
surface. V1 therefore treats the initial app-visible receipt as the running proof, exposes the
URL for operator readback, and lets existing PR webhooks/reconcile become the canonical progress
and completion signal. It does not invent provider polling.

### Pricing and Tally

Claude Code on the web shares the account's Claude/Claude Code usage limits. Anthropic documents
no separate charge for the hosted VM. Current individual list prices are Pro $20/month, Max 5x
$100/month, and Max 20x $200/month; plan availability and limits may change.

Switchboard cannot honestly allocate a subscription fee to one task. At adoption it writes an
idempotent Tally record with:

```json
{
  "source": "agent_report",
  "confidence": "unknown",
  "provider": "anthropic",
  "runtime": "claude-code",
  "cost_usd": 0,
  "metadata": {"billing_mode": "subscription"}
}
```

If the account explicitly switches to API credits and provider-measured spend becomes available,
a later reconciliation receipt may record that charge. The adapter never infers it from plan
price or token estimates.

## Switchboard flow

```text
Task Dev tab / dispatch_to_claude_code
  -> wake(runtime=claude-code, capability=vendor_cloud, mode=vendor_cloud)
  -> trigger-only Claude cloud host claims wake
  -> fetch origin/master; create/push claude/<task>-cloud
  -> clean temporary clone at the exact pushed SHA
  -> preflight CLI/auth/repo/branch/.mcp.json/concurrency
  -> PTY: claude --cloud <dev brief>
  -> parse session URL and bind runner_session + wake
  -> write subscription/unknown Tally receipt
  -> Dev tab shows Open Claude session
  -> Claude claims task, works, tests, pushes, opens PR
  -> existing PR webhook/reconcile projects PR and Done-on-merge
```

The host-local receipt store is keyed by `wake_id`. A retry returns the existing receipt instead
of creating a second provider session.

## Deployment

Install `deploy/switchboard-claude-cloud-host.service.example` as the template unit
`switchboard-claude-cloud-host@.service`, and enable an instance for the local user whose Claude
CLI has the subscription login. Install it only on a trigger host with:

- a clean canonical repo checkout;
- GitHub push access for task-branch creation;
- a current Claude CLI and active subscription login;
- a configured Claude cloud environment containing `SWITCHBOARD_TOKEN`;
- lane-scoped `PM_HOST_LANES` and a bounded `PM_HOST_MAX_SESSIONS`.

The production `plan.taikunai.com` VM remains coordination-only. Do not install a local coding
work module on the Claude cloud trigger service.

## Fail-closed behavior

| Failure | Visible result |
|---|---|
| CLI missing/older than 2.1.195 | `claude_cloud_preflight_failed` |
| Subscription login absent/expired | `claude_cloud_subscription_auth_required` |
| Non-TTY launch | `claude_cloud_tty_required` |
| Wrong repo/branch, dirty clone, or SHA not pushed | preflight failure; no provider call |
| Literal token in `.mcp.json` | `project_mcp_config_contains_literal_bearer` |
| Provider-side token reference missing | `scoped_mcp_token_ref_not_provider_bound` |
| Provider request returns no session URL | `adoption_receipt_incomplete` |
| Runner binding fails | wake fails `runner_binding_failed`; no optimistic running state |
| Concurrency cap reached | `provider_concurrency_cap_reached` |

No failure falls back to `run_agent`, a self-hosted Agent Host, browser automation, or a fabricated
session/Tally receipt.
