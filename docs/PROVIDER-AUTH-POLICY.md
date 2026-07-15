# Provider authentication policy

CO-15 makes provider authentication a server-authoritative capability, not a UI promise.
Enrollment, credential leases, scheduler admission, native CLI launch, REST, MCP, Settings, and
the CO-14 proof console all consume the matrix in
`src/switchboard/domain/provider_credentials/capabilities.py`. Unknown modes, missing records,
stale evidence, missing host bindings, and unapproved vendor states fail closed.

## Current capability matrix

| Provider and mode | State | Allowed execution boundary | Bootstrap | Why |
|---|---|---|---|---|
| Codex with ChatGPT subscription | `supported` | Trusted private worker, one exclusive account lease | Encrypted opaque `auth.json` capsule; isolated `CODEX_HOME`; fenced refresh writeback; purge | OpenAI documents ChatGPT subscription sign-in and a trusted-private CI path that securely seeds and persists refreshed `auth.json`. |
| Codex with API key | `supported` | Managed or user-owned worker, separately budgeted | API-key login / API gateway | Official automation auth; usage is pay-as-you-go and separate from a ChatGPT plan. |
| Claude subscription OAuth / `setup-token` | `vendor_confirmation_required` | None through Switchboard | Disabled | Anthropic documents `setup-token`, but its legal guidance says third-party products must not offer Claude.ai login or route Free/Pro/Max credentials on users' behalf. A long-lived token is not permission to broker it. |
| Claude API key | `supported` | Managed or user-owned worker, separately budgeted | `ANTHROPIC_API_KEY` or API gateway | Official API/pay-as-you-go path; it is not a subscription fallback. |
| Cursor browser login | `supported_host_bound` | Exact registered, user-owned persistent Agent Host only | Run browser login on that host; credentials stay local | Cursor recommends browser login and says credentials are stored locally. No supported export/bootstrap contract is inferred. |
| Cursor portable personal session | `unavailable` | None | None documented | Headless/CI documentation uses `CURSOR_API_KEY`; Switchboard will not copy a browser session to an ephemeral worker. |
| Cursor API key | `supported` | Managed or user-owned worker, separately budgeted | `CURSOR_API_KEY` | Cursor documents API keys for scripts, headless operation, and CI. |

Official evidence reviewed 2026-07-16:

- OpenAI: [Codex authentication](https://learn.chatgpt.com/docs/auth) and [advanced CI authentication](https://learn.chatgpt.com/docs/auth/ci-cd-auth).
- Anthropic: [Claude CLI usage](https://code.claude.com/docs/en/cli-usage), [environment variables](https://code.claude.com/docs/en/env-vars), and [legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
- Cursor: [CLI authentication](https://docs.cursor.com/en/cli/reference/authentication) and [headless CLI](https://docs.cursor.com/en/cli/headless).
- LiteLLM: [gateway and SDK documentation](https://docs.litellm.ai/).

The evidence has a dated revalidation boundary in code. After it expires, previously enabled
records become effectively `unavailable` until the reviewed evidence is updated. This is a
safety control, not a claim of legal clearance; written vendor terms and approval govern.

## LiteLLM boundary

LiteLLM is eligible only on records representing API/pay-as-you-go authentication. It may route
API calls, virtual keys, budgets, and provider fallbacks. It is never allowed to collect Google
OAuth, browser sessions, `auth.json`, Claude subscription OAuth, setup tokens, or Cursor personal
sessions. A personal-mode failure cannot silently fall back to LiteLLM or a metered key. Metered
lanes remain disabled unless the existing explicit credential, budget, attribution, and audited
opt-in policy passes.

## Per-user isolation

Every stored connection remains bound to tenant, Switchboard user, provider, provider account,
project allowlist, task, claim, host, runner, and Work Session. The capability matrix can only
remove authority; it cannot weaken those bindings. There are no shared subscription accounts,
cross-user leases, session-cookie scraping, token export from a host-bound login, or background
API substitution. Each person uses only their own connection and receives only their own usage.

## Consumer contract

The public record includes `provider`, `auth_mode`, `host_class`, `portability`,
`bootstrap_method`, `concurrency`, `state`, `approval_state`, `disable_reason`, evidence sources
and freshness, execution path, and LiteLLM eligibility. REST exposes it at
`GET /api/projects/{project}/provider-auth-capabilities`; MCP exposes
`list_provider_auth_capabilities`. Settings and CO-14 render those responses rather than keeping
their own provider allowlists.

The policy decision contract has only two enabled states: `supported` and an exact-host match for
`supported_host_bound`. `vendor_confirmation_required`, `unavailable`, stale, unknown, ambiguous,
or host-mismatched records deny enrollment, lease admission, scheduling, materialization,
activation, and launch with a stable reason code.
