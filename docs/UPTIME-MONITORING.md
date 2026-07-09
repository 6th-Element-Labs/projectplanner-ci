# External off-box uptime + latency alerting (HARDEN-44)

**Status:** Live. **Runs:** GitHub Actions on the public `6th-Element-Labs/projectplanner-ci`
sandbox, every 5 minutes. **Alerts:** GitHub-native (issue + failed run → email + mobile push).

## Why this exists

The only prior "monitoring" was [`scripts/plan_uptime_recover.sh`](../scripts/plan_uptime_recover.sh)
— a **manual, on-box** diagnostic/recovery tool. Nothing watched from **off** the box, so
outages of `plan.taikunai.com` were found by hand. An on-box watcher is also useless in the
exact case that matters most: the box itself being dead.

This adds an **off-box** probe that keeps watching from GitHub's infrastructure and pages
within minutes when the site is down or slow.

| | `plan_uptime_recover.sh` | this probe |
|---|---|---|
| Runs where | on the prod VM (manual) | off-box, GitHub-hosted, on a schedule |
| Survives the box dying | no | **yes** |
| Purpose | diagnose + recover | **detect + alert** |

## What it checks

Every run (`scripts/uptime_probe.py`, stdlib-only, no `pip install`):

1. **Liveness** — `GET /health`. One warm-up request pays the TLS handshake (that
   handshake *is* the reachability signal), then a 5-request burst is timed over the warm
   socket. Fails on any non-200 or when the burst **p95 latency > 2.0s**. Timing over a warm
   connection measures *server* responsiveness rather than the runner's TLS/RTT distance, so
   it doesn't flap.
2. **Login round-trip** — `POST /api/auth/login` → `GET /api/auth/session` with the returned
   `taikun_session` cookie, as `atlas@taikunai.com`. This exercises the full
   **web → auth → registry-DB** path, catching DB/auth outages a static `/health` never sees.
   Fails on a bad login, a missing session cookie, a session that doesn't resolve to
   `authenticated`, or either leg exceeding the 2.0s budget.

Exit code is non-zero on any failure; the workflow turns that into an alert.

### The probe account

`atlas@taikunai.com` is a **dedicated, zero-privilege** account (self-service signup:
`is_superadmin=false`, no project grants → deny-by-default). It exists only so the probe can
prove the auth path end-to-end. Its password lives **only** as the `PROBE_PASSWORD` Actions
secret on the sandbox repo — never the root/`PM_ROOT_PASSWORD` credentials, which must not be
scattered into a public repo.

## Alerting (GitHub-native, no external secrets)

On failure the workflow:

- opens a `uptime-outage`-labelled issue (or comments on the open one, so a sustained outage
  doesn't spam a new issue every 5 minutes), assigned to the operator, and
- **fails the run**.

Both an assigned/opened issue and a failed Actions run cause GitHub to email the operator and
push to the GitHub mobile app — the "page." On the next green run the probe **auto-closes** the
outage issue with a recovery comment.

This route was chosen over Slack/email-SMTP specifically because it needs **no external
credentials in a public repo**: `GITHUB_TOKEN` is enough.

## Configuration

Set on the sandbox repo `6th-Element-Labs/projectplanner-ci`:

| Kind | Name | Default | Purpose |
|---|---|---|---|
| **secret** | `PROBE_PASSWORD` | — | `atlas@taikunai.com` password |
| variable | `PROBE_EMAIL` | — | probe account (`atlas@taikunai.com`) |
| variable | `PROBE_BASE_URL` | `https://plan.taikunai.com` | target origin |
| variable | `PROBE_LATENCY_BUDGET_S` | `2.0` | p95 / per-leg latency budget |
| variable | `UPTIME_ALERT_ASSIGNEE` | `StevenRidder` | issue assignee (the page target) |

```bash
gh secret   set PROBE_PASSWORD           --repo 6th-Element-Labs/projectplanner-ci --body '<atlas password>'
gh variable set PROBE_EMAIL              --repo 6th-Element-Labs/projectplanner-ci --body 'atlas@taikunai.com'
```

If `PROBE_EMAIL`/`PROBE_PASSWORD` are unset the login check is **skipped** (health-only), so
the probe degrades gracefully rather than false-alarming.

## Testing the alert (acceptance drill)

Force a failure without any real outage — the workflow injects `PROBE_SIMULATE_OUTAGE`:

```bash
gh workflow run uptime-probe.yml --repo 6th-Element-Labs/projectplanner-ci -f simulate_outage=true
```

Expected: a red run, a new/updated `uptime-outage` issue, and a GitHub notification. A normal
run (`-f simulate_outage=false`, or the schedule) then closes the issue on recovery.

## Deployment note

The workflow's `schedule` only fires from the repo's **default branch** on the sandbox
(Actions are disabled on the private canonical repo). The probe is self-contained
(`scripts/uptime_probe.py` imports nothing from the rest of the tree), so publishing these two
files to the sandbox default branch is all that's needed:

```bash
# from a canonical checkout, publish the two files to the sandbox default branch
scripts/uptime_probe.py  .github/workflows/uptime-probe.yml
```

The canonical repo holds these files as the source of truth (code review + Done provenance);
the sandbox copy is what actually runs.

## Tests

`test_uptime_probe.py` exercises the probe hermetically (a local `http.server` stand-in) —
p95 math, latency-budget failure, 5xx, login 401, missing cookie, unauthenticated session,
health-only skip, and the simulated-outage path. It runs in the standard suite
(`scripts/switchboard_ci.sh`).
