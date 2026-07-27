# GitHub App tokens for the REST API

Why the fleet stopped reading GitHub on 2026-07-26, and the cutover that fixes it.

## The failure

Every server-side GitHub caller read the same personal access token out of the
environment. **A user PAT's 5,000 requests/hour is billed to the GitHub account, not
to the token** — so reconcile, the Fleet
dock's sweeps, push verification and an operator's local `gh` all drew on one budget.

The budget ran out and stayed out. Real evidence from prod:

```
x-ratelimit-limit: 5000
x-ratelimit-remaining: 0
x-ratelimit-used: 5000
403 {"message": "API rate limit exceeded for user ID 176963715"}
```

143 rate-limit errors in 24 hours, 130 of them from `projectplanner-claim-gate`. The
Fleet dock's Pull requests and Deployments panes went blank because the endpoints
behind them could not reach GitHub at all; the dock itself was healthy.

Two traps this incident taught:

- **`/rate_limit` lied.** It reported `remaining: 4962` while real calls returned
  `remaining: 0`. Trust the `x-ratelimit-*` headers on an actual request.
- **The dominant cost was redundant.** Each sweep ran both the claim gate and the
  merge-authorization gate over every PR, and each fetched the same PR file list
  independently — two identical GETs per PR, 30 sweeps an hour, four repos.

## The fix

`github_app_auth` mints **installation access tokens**: App JWT (RS256, ≤10 min) →
`POST /app/installations/{id}/access_tokens` → a ~1-hour `ghs_` token, cached
in-process and refreshed 5 minutes before expiry. An installation token is billed to
the installation, so the fleet no longer competes with a human account.

Budget — **measure it, don't quote the docs.** Installation limits vary by account
type and plan, and the published tiers are easy to mis-apply. Measured on this
deployment 2026-07-26, straight off `x-ratelimit-limit` on a real request:

| Installation | Limit/hr |
|---|---|
| `6th-Element-Labs` (org, id 149220171) | **15,000** |
| `StevenRidder` (personal, id 149220068 — Helm) | **5,000** |

20,000/hr across two independent buckets, versus one shared 5,000 before. The
structural win is that they are *separate* — separate from each other and from every
human's account — so one greedy consumer can no longer starve the rest. The
redundant-call fix in the gate still matters: headroom is not a licence to waste it.

Nothing is required to keep working: with no App configured, every caller resolves the
same PAT chain it always did.

That compatibility statement applies to the fleet REST clients described here. It does
**not** apply to the public CI callback. `projectplanner-ci/verify.yml` uses the dedicated
`switchboard-ci-status` App, installed only on the canonical projectplanner repository,
with metadata read and commit-status read/write permissions. Its
`SWITCHBOARD_APP_ID` and `SWITCHBOARD_APP_PRIVATE_KEY` secrets are mandatory; token
minting fails closed and there is no PAT fallback. Only the trusted default-branch
announce/report jobs can access those secrets; the scratchpad suite job is secret-free.

## Operator cutover

### 1. Create the App (org owner, once)

GitHub → your org → Settings → Developer settings → GitHub Apps → **New GitHub App**.
Own it under the org, not a personal account.

| Setting | Value |
|---|---|
| Name | `switchboard-fleet` (must be unique on GitHub) |
| Homepage | `https://plan.taikunai.com` |
| Webhook | not needed for this path — uncheck Active |

Repository permissions:

| Permission | Access | Used by |
|---|---|---|
| Metadata | Read | required by GitHub |
| Pull requests | Read | dock and scoped Autopilot |
| Contents | Read | commit / branch checks, push verification |
| Commit statuses | **Read & write** | the claim advisory and required CI callback |
| Checks | Read | CI state on PR cards |

Keep the **App ID**, and **Generate a private key** — the `.pem` downloads once.

### 2. Install it everywhere the fleet reads

Install on the org, and separately on any personal account that owns a canonical repo
(Helm is one). **Each install is its own installation id and its own budget.** Note
each id from the install URL:

```
https://github.com/organizations/<org>/settings/installations/<INSTALLATION_ID>
```

### 3. Put the key on the box

The PEM is a private key. It goes on the host, root-owned, mode 600 — never in the
repo, the board DB, MCP, wakes, or logs.

```bash
sudo install -o root -g root -m 600 /dev/null /etc/projectplanner/github-app.pem
sudo tee /etc/projectplanner/github-app.pem < ~/Downloads/<app>.private-key.pem >/dev/null
```

Then in `/opt/projectplanner/.env`:

```bash
PM_GITHUB_APP_ID=<app id>
PM_GITHUB_APP_PRIVATE_KEY_PATH=/etc/projectplanner/github-app.pem
PM_GITHUB_APP_INSTALLATIONS={"<org>": <org install id>, "<user>": <user install id>}
```

The installation map is optional — installations are discovered per repo and cached —
but it removes a lookup per owner per process.

### 4. Restart and verify

```bash
sudo systemctl restart projectplanner projectplanner-mcp
cd /opt/projectplanner && sudo bash -c 'set -a && . ./.env && set +a && \
    .venv/bin/python scripts/github_app_doctor.py'
```

The doctor prints, per canonical repo, which credential resolved and that
credential's **real** remaining budget. Exit 0 means every canonical repo is minting
installation tokens. Anything still reading `env:…` is not migrated — install the App
on that owner.

### 5. Retire the PAT

Once the doctor is green and a sweep has run clean, the old fleet PAT is only a
fallback for remaining host-side subprocess paths. This is separate from the public
workflow callback: `PRIVATE_READ_TOKEN` has been removed from projectplanner-ci and
must stay removed.

## Rotation

Replace the file at `PM_GITHUB_APP_PRIVATE_KEY_PATH` and restart the services; tokens
are cached in-process only. GitHub allows several private keys per App, so generate
the new key, deploy, then delete the old one from the App's settings.

## Related

- `github_app_auth.py` — the exchange, the caching, and the PAT fallback
- `scripts/github_app_doctor.py` — the verification above
- `docs/SCM-GITHUB-APP-ONBOARDING.md` — the *execution plane* App path (clone/push
  leases, ENFORCE-13/15). Same trust model, different consumer; this document covers
  only the REST read/status path.
