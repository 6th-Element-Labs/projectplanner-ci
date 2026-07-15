# Provision plan.taikunai.com (cheap standalone VM)

Two small Python processes + Caddy on one tiny ARM VM. ~$6/mo.

Switchboard is the product name. This provisioning guide still uses the historical
`projectplanner` repo, paths, systemd units, and `PM_*` env prefix because those are the
currently deployed compatibility surfaces. Do not rename them in-place without following
[`docs/SWITCHBOARD-RENAME-MIGRATION.md`](../docs/SWITCHBOARD-RENAME-MIGRATION.md).

## 1. Launch the VM (AWS, us-east-1)
- **Type:** `t4g.micro` (2 vCPU ARM Graviton, 1 GB RAM) — comfortable for app + gateway.
  `t4g.nano` (0.5 GB) is cheaper (~$3/mo) but tight once LiteLLM is running; micro is the safe pick.
- **AMI:** Ubuntu 22.04 LTS (arm64).
- **Disk:** 10 GB gp3.
- **Security group:** inbound 22 (SSH, your IP), 80 + 443 (world, for Caddy/Let's Encrypt). The
  app (8110) and gateway (8095) bind to 127.0.0.1 only — never exposed.
- Allocate an **Elastic IP** and associate it (stable IP for DNS).

CLI sketch (fill in your key/SG/subnet):
```bash
aws ec2 run-instances --region us-east-1 --image-id <ubuntu-2204-arm64-ami> \
  --instance-type t4g.micro --key-name <key> --security-group-ids <sg> \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":10,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=projectplanner}]'
```

## 2. DNS (Route 53)
Add an **A record**: `plan.taikunai.com` -> the Elastic IP. (Apex/other taikunai.com records unchanged.)

## 3. Install
```bash
ssh ubuntu@<eip>
sudo apt-get update && sudo apt-get install -y git debian-keyring debian-archive-keyring apt-transport-https software-properties-common
# Python 3.12 — REQUIRED. The app targets 3.12 (pyproject `requires-python>=3.12`) and the
# checked-in uv.lock / pinned requirements.txt resolve 3.12-only wheels (e.g. rpds-py has no
# cp310 build), so a 3.10 venv fails `pip install -r requirements.txt`. Ubuntu 22.04 ships only
# 3.10, so pull 3.12 from deadsnakes (has arm64/jammy). See HARDEN-66.
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
# Caddy
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

# Node.js 20 LTS — REQUIRED by the Switchboard PR CI gate, which runs
# `node --check static/*.js`. The distro's Node 12 cannot parse ES2020 syntax
# (optional chaining `?.`, nullish `??`) used in static/app.js, so the gate
# fails every PR with "SyntaxError: Unexpected token '.'". Do NOT rely on the
# Ubuntu `nodejs` package.
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs   # dpkg conflict? sudo apt-get remove -y libnode-dev libnode72 && sudo apt-get install -y nodejs
node --version   # expect v20.x

# GitHub CLI (gh) — REQUIRED by the off-box CI mirror (external_ci_mirror, Route A),
# which uses `gh workflow run` / `gh run list --branch` / `gh api` to dispatch + poll
# the public CI sandbox. Ubuntu's apt `gh` is 2.4.0 and LACKS `gh run list --branch`;
# install a modern build AND match the box arch (this t4g.micro is ARM64/aarch64).
GH_VER=2.63.2; GH_ARCH=arm64   # use amd64 on an x86 box
curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VER}/gh_${GH_VER}_linux_${GH_ARCH}.tar.gz"
sudo tar -xzf /tmp/gh.tgz -C /usr/local/bin --strip-components=2 "gh_${GH_VER}_linux_${GH_ARCH}/bin/gh"
sudo ln -sf /usr/local/bin/gh /usr/bin/gh && gh --version   # expect >= 2.6

# App. HARDEN-55: the code tree + venv are owned by root and never by the runtime,
# so the services can read/execute their code but can never rewrite it. Build them
# with sudo so every file lands root-owned.
sudo git clone <projectplanner-remote> /opt/projectplanner
cd /opt/projectplanner
sudo python3.12 -m venv .venv    # MUST be 3.12 (see above) — `python3` is 3.10 on jammy
sudo .venv/bin/pip install -r requirements.txt -r deploy/gateway/requirements.txt
sudo cp .env.example .env   # set OPENAI_API_KEY + LLM_GATEWAY_MASTER_KEY (==PM_LLM_KEY)
# UI-12: for real cost in the Economics panels, set PM_TALLY_INGEST_TOKEN to a
# DEDICATED least-privilege token — write:ixp only, bound to all boards (the
# gateway proxies every project's LLM calls). Mint it, do NOT reuse PM_MCP_TOKEN:
#   create_scoped_token(project="*", display_name="litellm-gateway-tally-ingest", scopes="write:ixp")
# The gateway's LiteLLM success callback (deploy/gateway/tally_callback.py) posts
# each call's spend to /tally/v1/spend/ingest. Without the token the ledger stays
# empty. Restarting projectplanner-gateway briefly interrupts in-flight LLM calls.
# The production units also force PM_AUTH_MODE=required; keep it explicit here for audits.
printf '\nPM_AUTH_MODE=required\n' | sudo tee -a .env >/dev/null
# First human admin bootstrap. Remove the password line after first successful startup/login.
printf '\nPM_BOOTSTRAP_ADMIN_LOGIN=admin\nPM_BOOTSTRAP_ADMIN_PASSWORD=<replace-me>\n' | sudo tee -a .env >/dev/null

# HARDEN-55: create the dedicated non-login service account, chown the DATA dir
# (/var/lib/projectplanner) to it, keep the CODE tree root-owned/read-only, and
# lock .env to root-only. Idempotent — redeploy.sh re-runs it on every deploy.
sudo bash deploy/apply-least-privilege.sh
```

## 4. Run (systemd + Caddy)
```bash
sudo cp deploy/projectplanner-gateway.service deploy/projectplanner.service \
  deploy/projectplanner-mcp.service deploy/projectplanner-monitors.service \
  deploy/projectplanner-monitors.timer deploy/projectplanner-reconcile.service \
  deploy/projectplanner-reconcile.timer deploy/projectplanner-coordinator-audit.service \
  deploy/projectplanner-coordinator-audit.timer deploy/projectplanner-claim-gate.service \
  deploy/projectplanner-claim-gate.timer deploy/projectplanner-agent-host.service \
  deploy/switchboard-auth.service \
  deploy/projectplanner-interactive.slice deploy/projectplanner-batch.slice \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-gateway projectplanner projectplanner-mcp switchboard-auth
sudo systemctl enable --now projectplanner-monitors.timer
sudo systemctl enable --now projectplanner-reconcile.timer
sudo systemctl enable --now projectplanner-coordinator-audit.timer
sudo systemctl enable --now projectplanner-claim-gate.timer
# Optional but recommended for Switchboard dogfood: consumes message-only wake intents.
# It uses PM_HOST_LANES=__MESSAGE_ONLY__ so it will not claim lane-scoped work.
sudo systemctl enable --now projectplanner-agent-host
# ARCH-MS-76: prove Auth is up on :8121 BEFORE reloading Caddy (which routes /api/auth*).
curl -sS http://127.0.0.1:8121/health   # expect {"status":"ok","service":"switchboard-auth"}
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy
# PERF-3: zram compressed-RAM swap (fast) instead of disk swap (100000x slower page faults).
sudo bash deploy/setup-zram-swap.sh
# PERF-4: interactive vs batch cgroup slices so timer jobs cannot starve the web app.
sudo bash deploy/apply-resource-guards.sh
bash scripts/verify_cgroup_slices.sh
bash scripts/verify_memory_isolation.sh
```

### Auth process-cut cutover checklist (ARCH-MS-76)

Live edge routes `/api/auth*` → `switchboard-auth` on `127.0.0.1:8121`. The monolith still
mounts the Auth router for **rollback** (green façade). Full drill:
[`docs/runbooks/auth-caddy-cutover-rollback.md`](../docs/runbooks/auth-caddy-cutover-rollback.md).

**First enable (or after a No-Go skip was reversed):**
1. `sudo systemctl enable --now switchboard-auth`
2. `curl -sS http://127.0.0.1:8121/health` → 200
3. Confirm repo `deploy/Caddyfile` contains `handle /api/auth*` → `:8121`
4. `sudo caddy validate --adapter caddyfile --config deploy/Caddyfile`
5. `sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`
6. Smoke through the public edge:
   - `curl -sS -o /dev/null -w '%{http_code}\n' https://plan.taikunai.com/api/auth/session` → 401
   - bad password `POST /api/auth/login` → **401** (never 403)
   - register / login / logout happy path
7. Prefer `bash deploy/redeploy.sh` thereafter — it starts Auth **before** reloading Caddy.

**Rollback (< ~60s):** remove the `/api/auth*` handle blocks from `/etc/caddy/Caddyfile`
(or restore the pre-cut file), `sudo systemctl reload caddy`, re-smoke against monolith
`:8110`, then optionally `sudo systemctl stop switchboard-auth`.
### Off-box backups (HARDEN-43)
Prod SQLite lives only on this box's disk. Set up daily off-box snapshots + a
tested restore path — full details in [`docs/BACKUP-RESTORE-RUNBOOK.md`](../docs/BACKUP-RESTORE-RUNBOOK.md).
```bash
# One-time, from an operator machine with admin AWS creds (creates a versioned
# private bucket + a put-only IAM user, and prints the /etc/projectplanner-backup.env block):
CREATE_ACCESS_KEY=1 scripts/provision_backup_s3.sh
# On the box: install that env block (mode 600), then enable the daily timer:
sudo cp deploy/projectplanner-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-backup.timer
sudo systemctl start projectplanner-backup.service   # prove it once now, don't wait for 07:19
```
Caddy fetches the TLS cert automatically once DNS resolves. Visit https://plan.taikunai.com/.
The app will present the login screen in required mode. On first startup, the bootstrap admin
is created only if no password-backed admin exists for the project. After confirming login,
remove `PM_BOOTSTRAP_ADMIN_PASSWORD` from `.env` and restart `projectplanner`.

## 5. Verify
```bash
curl -s http://127.0.0.1:8110/health            # {"status":"ok","service":"taikun-pm"}  (cheap liveness)
curl -s http://127.0.0.1:8110/health/deep      # ops readiness: task + project counts
curl -s http://127.0.0.1:8121/health            # ARCH-MS-76 Auth process-cut
curl -s http://127.0.0.1:8095/v1/models -H "Authorization: Bearer $LLM_GATEWAY_MASTER_KEY"
systemctl is-active switchboard-auth
systemctl list-timers projectplanner-monitors.timer
systemctl list-timers projectplanner-reconcile.timer
systemctl list-timers projectplanner-coordinator-audit.timer
systemctl list-timers projectplanner-claim-gate.timer
systemctl list-timers projectplanner-backup.timer     # HARDEN-43: daily off-box snapshot
systemctl is-active projectplanner-agent-host
gh --version                                    # off-box CI mirror needs gh >= 2.6 (see step 6)
# The self-hosted Actions runner is DECOMMISSIONED (CI runs off-box now, see step 6);
# it should be inactive/disabled — a running one is a redundant idle drain:
systemctl is-active actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service  # expect: inactive
```

## Runtime least-privilege (HARDEN-55)

The services do NOT run as the general `ubuntu` login account and cannot rewrite their own
code. `deploy/apply-least-privilege.sh` (run in step 3 and re-run on every `redeploy.sh`)
establishes and re-asserts the posture:

- **Dedicated identity.** A system account `projectplanner` (no login shell, home on the data
  dir) owns the runtime. Every `projectplanner-*.service` sets `User=projectplanner`.
- **Read-only code.** `/opt/projectplanner` and its `.venv` are root-owned and not
  group/other-writable. The runtime reads/executes but can never write its code. `.env` is
  `root:projectplanner` mode 640 (secret from other users; systemd reads it as root).
- **Confined writes.** Each unit declares `ProtectSystem=strict` + `ReadWritePaths=/var/lib/projectplanner`,
  so `/var/lib/projectplanner` (service-owned) is the ONLY writable tree; everything else is
  read-only. `reconcile` additionally gets a `RuntimeDirectory` for its flock.
- **Sandbox.** Every unit sets `NoNewPrivileges`, `PrivateTmp`, `ProtectHome`,
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`, plus
  `ProtectKernel*`/`ProtectControlGroups`/`RestrictRealtime`/`RestrictSUIDSGID`/`LockPersonality`.

Because the code tree is root-owned, `git pull` and `pip install` in `redeploy.sh` run under
`sudo`, and manual `jobs.py` maintenance runs as `sudo -u projectplanner` (see below).
The service-owned `/var/lib/projectplanner/ci-source` checkout is the exception: provisioning
and every redeploy run its Git operations as `projectplanner`, so repeated deploys do not need
a root-level `safe.directory` exception and remain safe under Git's ownership checks.

Verify the sandbox is actually applied to the running services:
```bash
systemctl show projectplanner -p User -p ProtectSystem -p ReadWritePaths -p NoNewPrivileges
systemd-analyze security projectplanner projectplanner-mcp   # lower exposure score = more locked down
# The runtime must NOT be able to write its own code:
sudo -u projectplanner test -w /opt/projectplanner && echo "FAIL: code tree writable" || echo "ok: code read-only"
```

## Site hung / 0-byte response (HARDEN-32)

Symptom: `curl https://plan.taikunai.com/health` completes TLS then hangs (0 bytes) for
20s+; local `curl -m5 http://127.0.0.1:8110/health` also hangs.

**Likely cause:** single uvicorn worker blocked on sync SQLite (heavy `/api/board` or the old
`/health` that called `list_tasks()`), or memory pressure on `t4g.micro` (swap thrash).

**On the VM:**
```bash
cd /opt/projectplanner
bash scripts/plan_uptime_recover.sh
# or manually:
curl -m5 -sS http://127.0.0.1:8110/health || sudo systemctl restart projectplanner projectplanner-mcp
free -h && journalctl -u projectplanner -n 60 --no-pager
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

After deploy, `/health` stays cheap (no DB walk). Caddy uses short timeouts on `/health*` and
active health checks on the main upstream so hung backends fail fast instead of holding clients
for minutes. PERF-3 routes swap through **zram** (`deploy/setup-zram-swap.sh`) so any spill stays
in compressed RAM instead of thrashing disk. PERF-4 splits services into
`projectplanner-interactive.slice` (web/MCP/gateway: high CPUWeight, memory reservations, no swap)
and `projectplanner-batch.slice` (reconcile/narrate/claim-gate: CPUQuota~40%, Nice=10, low IOWeight,
memory-capped). Install with `deploy/apply-resource-guards.sh`; verify with
`bash scripts/verify_cgroup_slices.sh` and `bash scripts/verify_memory_isolation.sh`. If a batch
job still wedges the box, stop the timers to recover fast:
```bash
sudo systemctl stop projectplanner-{narrate,monitors,inbox,reconcile,coordinator-audit,summarize,claim-gate}.timer
sudo pkill -9 -f jobs.py   # then restart the web app if needed
```
If wedges recur, bump the instance to `t4g.small` (2 GB) and/or move the GitHub Actions runner +
CI gate off this box (per HARDEN-40).

**Acceptance:** `curl -m5 https://plan.taikunai.com/health` returns `200` in under 2s.

## Update live code
One idempotent command pulls the latest code, syncs the systemd units **and the Caddyfile**
into `/etc`, then restarts the web tier + reloads Caddy (behind a `caddy validate` gate and a
`/health` check):
```bash
cd /opt/projectplanner && bash deploy/redeploy.sh
```
`deploy/redeploy.sh` is the single source of truth for a redeploy. It exists because a bare
`git pull` updates neither `/etc/caddy/Caddyfile` nor `/etc/systemd/system/*` — so edge and
unit changes used to reach prod only if someone remembered the extra `cp` (that is how the
Caddyfile drifted from the repo). It copies every `deploy/*.service`/`*.timer`, mirrors the
Caddyfile (validating it first — a broken edge is never reloaded), restarts
`projectplanner{,-gateway,-mcp}` **and** `switchboard-auth`, proves both `:8110` and `:8121`
`/health` are 200, **then** reloads Caddy, and restarts any auxiliary timer/service that is
currently active. Flags: `RUN_CI=1` runs the on-box strict CI gate first (CI otherwise runs
off-box); `SKIP_CADDY=1` leaves the edge untouched. The post-restart health gate retries for a
bounded 30-second window by default; set `HEALTH_TIMEOUT_SECONDS` and
`HEALTH_INTERVAL_SECONDS` to tune that window without replacing the fail-closed final result.

It restarts only units that are **already active**, so it won't fight timers you stopped during
a HARDEN-32 wedge. A brand-new unit still needs its one-time
`sudo systemctl enable --now <unit>` (see step 4).

## CEO-voice narrator timer

`projectplanner-narrate.timer` drains the CEO-voice narrator every ~45s (see
`docs/CEO-NARRATOR-CONTRACT.md`). It runs `jobs.py narrate_pending`, which narrates tasks
that changed status and re-narrates deliverable headers whose brief fingerprint moved.
It reuses the summarize LLM gateway and defaults to the cheap `taikun-summarize`
(gpt-4o-mini) model — no new env vars required. The fingerprint + activity-cursor guards
make idle cycles cost zero LLM calls.

```bash
systemctl list-timers projectplanner-narrate.timer
journalctl -u projectplanner-narrate.service -n 40 --no-pager
# one-shot backfill / manual drain:
cd /opt/projectplanner && sudo -u projectplanner .venv/bin/python jobs.py narrate_pending
```

## Scratchpad VM verification (CI-12) + claim gate (CI-7)

**VM verification** (`Switchboard CI / VM gate`) runs on `6th-Element-Labs/projectplanner-ci`
via the push-triggered scratchpad `verify.yml` workflow — not on the Plan VM. The canonical
PR webhook uses a service-owned coordination checkout to fetch the exact PR head and mirror it
to `ci/**`.
Because `/opt/projectplanner` is root-owned and read-only, `apply-least-privilege.sh` seeds a
writable coordination clone at `/var/lib/projectplanner/ci-source` and configures its GitHub
credential helper. Ensure `PM_GITHUB_TOKEN`, `SWITCHBOARD_CI_GITHUB_TOKEN`, or `GITHUB_TOKEN`
can fetch canonical PR refs, push to `projectplanner-ci`, and poll Actions. The runner promotes
that token to `GH_TOKEN` for `git`/`gh` subprocesses. The old pull relay flag is not primary:

```bash
sudo systemctl restart projectplanner
```

Declare repo roles (`set_project_repo_topology` or POST `/api/projects/switchboard/repo_topology`):

- `canonical_repo=6th-Element-Labs/projectplanner` (Done / code-truth authority)
- `public_ci_repo=6th-Element-Labs/projectplanner-ci` (verification-only, public)

The on-box **self-hosted Actions runner is decommissioned** — keep it off:

```bash
sudo systemctl disable --now actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service
```

After scratchpad verification holds, retire the old on-box VM gate with
`sudo bash deploy/ci7-teardown-box-ci.sh`.

## PR claim-gate timer (CI-7)

VM verification (`Switchboard CI / VM gate`) runs on projectplanner-ci via the scratchpad
`verify.yml` workflow. The Plan VM posts only the SESSION-12 claim gate separately:

```bash
/opt/projectplanner/.venv/bin/python /opt/projectplanner/jobs.py claim_gate_prs
```

`projectplanner-claim-gate.timer` polls open non-draft PRs across every configured canonical repo
and posts `Switchboard / claim gate` to each PR head SHA. It needs a token with commit-status
write in `PM_GITHUB_TOKEN`, `GITHUB_TOKEN`, or `SWITCHBOARD_CI_GITHUB_TOKEN`.

VM verification on projectplanner-ci runs `scripts/switchboard_ci.sh` inside `verify.yml` (see
[`docs/CI-STRATEGY.md`](../docs/CI-STRATEGY.md)).

The old CI-6 `SWITCHBOARD_CI_PULL_MODEL` dispatch remains a manual rollback bridge only.
After scratchpad CI holds, run `sudo bash deploy/ci7-teardown-box-ci.sh` to retire the old
on-box VM gate units and `/var/lib/projectplanner/ci-gate` state. Rollback copies live under
`deploy/retired/` for one week.

## Rename safety

The live deployment should keep these compatibility names until Switchboard aliases are
implemented and verified:

- `/opt/projectplanner`
- `/var/lib/projectplanner`
- `projectplanner*.service` and `projectplanner*.timer`
- `switchboard-auth.service` (ARCH-MS-76 Auth process-cut)
- `PM_*` environment variables
- GitHub remote `6th-Element-Labs/projectplanner`

For the rename migration, first add aliases such as `/opt/switchboard -> /opt/projectplanner`
and `switchboard*.service` wrappers. Verify health, MCP, Agent Host, reconcile, and Tally
before making any alias canonical.

## Merged PR branch retirement (BUG-29)

After a same-repo PR merges, the webhook handler can archive the head branch as
`refs/tags/archive/<branch>` at the PR head SHA, then delete `refs/heads/<branch>`.
PR records stay on GitHub; only branch refs are cleaned up.

Enable on the Plan VM once `PM_GITHUB_TOKEN` (or `GITHUB_TOKEN`) has `contents:write`
on `6th-Element-Labs/projectplanner`:

```bash
printf '\nPM_RETIRE_MERGED_BRANCHES=1\n' >> /opt/projectplanner/.env
sudo systemctl restart projectplanner
```

Backfill historical merged branches (dry-run first):

```bash
cd /opt/projectplanner
PM_RETIRE_MERGED_BRANCHES=1 .venv/bin/python scripts/backfill_retire_merged_branches.py --dry-run
PM_RETIRE_MERGED_BRANCHES=1 .venv/bin/python scripts/backfill_retire_merged_branches.py
```

Recover an archived branch locally: `git fetch origin refs/tags/archive/<branch>` then
`git checkout -b <branch> archive/<branch>`.

## Bootstrap direct-default provenance backfill
Use this only for legacy dogfood commits that landed directly on the default branch before the
PR webhook flow was enforced. Normal agent work still goes through `complete_claim` → PR merge
webhook → `Done`.

# HARDEN-55: run manual jobs.py commands as the `projectplanner` service account so any
# SQLite -wal/-shm files stay service-owned (running as root would leave root-owned journal
# files the service then can't write). That account can read .env (root:projectplanner 640).
```bash
cd /opt/projectplanner
sudo -u projectplanner env PM_BACKFILL_PROJECT=switchboard PM_BACKFILL_DRY_RUN=1 \
  .venv/bin/python jobs.py backfill_default_branch_provenance
# If the candidates are correct:
sudo -u projectplanner env PM_BACKFILL_PROJECT=switchboard \
  .venv/bin/python jobs.py backfill_default_branch_provenance
```

## Scheduled reconcile projects

`projectplanner-reconcile.timer` runs `jobs.py reconcile_alerts`. By default that job now checks
all registered boards, not only `switchboard`, so project-scoped boards such as Helm can backfill
GitHub merge provenance from PR evidence even if their repo webhook is missing or delayed. To narrow
the scheduled surface deliberately, set `PM_RECON_ALERT_PROJECTS=switchboard` or a comma-separated
project list in `/opt/projectplanner/.env`, then restart `projectplanner-reconcile.timer`.

## T0 coordinator audit loop (COORD-2)

`projectplanner-coordinator-audit.timer` runs `jobs.py coordinator_audit` every five minutes.
The job reads each selected board through SQLite `mode=ro` + `query_only`, ranks assignment,
review, merge-gate, reconcile, stale-claim, and escalation recommendations, and appends one
bounded `coordinator.audit.plan` artifact. It never executes those recommendations and its
service has no network address family. See
[`docs/COORDINATOR-AUDIT-LOOP.md`](../docs/COORDINATOR-AUDIT-LOOP.md).

The default surface is only `switchboard`. Set `PM_COORDINATOR_AUDIT_PROJECTS` to a comma list
or `all` to change it; set `PM_COORDINATOR_AUDIT_LOG=0` for a no-persistence preview.

```bash
systemctl list-timers projectplanner-coordinator-audit.timer
journalctl -u projectplanner-coordinator-audit.service -n 50 --no-pager
cd /opt/projectplanner && sudo -u projectplanner env PM_COORDINATOR_AUDIT_LOG=0 \
  .venv/bin/python jobs.py coordinator_audit
```

## Rebase timeline
```bash
# rebase kickoff (regenerates seed_plan.json); apply to the LIVE db with a dates-only UPDATE.
# HARDEN-55: build_plan_artifacts.py writes seed_plan.json into the root-owned code tree, so
# regenerate it as root; apply the DB UPDATE as the projectplanner service account.
sudo .venv/bin/python build_plan_artifacts.py 2026-06-01
sudo -u projectplanner .venv/bin/python - <<'PY'
import json, sqlite3, os
seed = json.load(open("/opt/projectplanner/seed_plan.json"))
c = sqlite3.connect(os.environ.get("PM_DB_PATH", "/var/lib/projectplanner/plan.db"))
for w in seed["workstreams"]:
    for t in w["tasks"]:
        c.execute("UPDATE tasks SET start_date=?,finish_date=?,duration_days=?,start_day=? WHERE task_id=?",
                  (t["start_date"], t["finish_date"], t["duration_days"], t["start_day"], t["task_id"]))
for k in ["schedule_start","schedule_note","generated"]:
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (k, json.dumps(seed[k])))
c.commit(); print("rebased")
PY
sudo systemctl restart projectplanner projectplanner-mcp
```

## Cost
t4g.micro ~$6/mo + 10 GB gp3 ~$0.80/mo + Elastic IP (free while attached) + LLM usage (gpt-5.5 +
text-embedding-3-small; low volume, usage-based). Call it **~$7/mo + token usage**.
