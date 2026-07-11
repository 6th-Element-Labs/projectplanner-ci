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
sudo apt-get update && sudo apt-get install -y python3-venv git debian-keyring debian-archive-keyring apt-transport-https
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

# App
sudo git clone <projectplanner-remote> /opt/projectplanner
cd /opt/projectplanner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r deploy/gateway/requirements.txt
sudo mkdir -p /var/lib/projectplanner/runner /var/lib/projectplanner/repo-hygiene-archive
sudo chown -R ubuntu:ubuntu /var/lib/projectplanner
cp .env.example .env   # set OPENAI_API_KEY + LLM_GATEWAY_MASTER_KEY (==PM_LLM_KEY)
# UI-12: for real cost in the Economics panels, set PM_TALLY_INGEST_TOKEN to a
# DEDICATED least-privilege token — write:ixp only, bound to all boards (the
# gateway proxies every project's LLM calls). Mint it, do NOT reuse PM_MCP_TOKEN:
#   create_scoped_token(project="*", display_name="litellm-gateway-tally-ingest", scopes="write:ixp")
# The gateway's LiteLLM success callback (deploy/gateway/tally_callback.py) posts
# each call's spend to /tally/v1/spend/ingest. Without the token the ledger stays
# empty. Restarting projectplanner-gateway briefly interrupts in-flight LLM calls.
# The production units also force PM_AUTH_MODE=required; keep it explicit here for audits.
printf '\nPM_AUTH_MODE=required\n' >> .env
# First human admin bootstrap. Remove the password line after first successful startup/login.
printf '\nPM_BOOTSTRAP_ADMIN_LOGIN=admin\nPM_BOOTSTRAP_ADMIN_PASSWORD=<replace-me>\n' >> .env
sudo chown -R ubuntu /opt/projectplanner
```

## 4. Run (systemd + Caddy)
```bash
sudo cp deploy/projectplanner-gateway.service deploy/projectplanner.service \
  deploy/projectplanner-mcp.service deploy/projectplanner-monitors.service \
  deploy/projectplanner-monitors.timer deploy/projectplanner-reconcile.service \
  deploy/projectplanner-reconcile.timer deploy/projectplanner-ci-gate.service \
  deploy/projectplanner-ci-gate.timer deploy/projectplanner-agent-host.service \
  deploy/projectplanner-interactive.slice deploy/projectplanner-batch.slice \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-gateway projectplanner projectplanner-mcp
sudo systemctl enable --now projectplanner-monitors.timer
sudo systemctl enable --now projectplanner-reconcile.timer
# Optional but recommended for Switchboard dogfood: consumes message-only wake intents.
# It uses PM_HOST_LANES=__MESSAGE_ONLY__ so it will not claim lane-scoped work.
sudo systemctl enable --now projectplanner-agent-host
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy
# PERF-3: zram compressed-RAM swap (fast) instead of disk swap (100000x slower page faults).
sudo bash deploy/setup-zram-swap.sh
# PERF-4: interactive vs batch cgroup slices so timer jobs cannot starve the web app.
sudo bash deploy/apply-resource-guards.sh
bash scripts/verify_cgroup_slices.sh
bash scripts/verify_memory_isolation.sh
```

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
curl -s http://127.0.0.1:8095/v1/models -H "Authorization: Bearer $LLM_GATEWAY_MASTER_KEY"
systemctl list-timers projectplanner-monitors.timer
systemctl list-timers projectplanner-reconcile.timer
systemctl list-timers projectplanner-ci-gate.timer
systemctl list-timers projectplanner-backup.timer     # HARDEN-43: daily off-box snapshot
systemctl is-active projectplanner-agent-host
gh --version                                    # off-box CI mirror needs gh >= 2.6 (see step 6)
# The self-hosted Actions runner is DECOMMISSIONED (CI runs off-box now, see step 6);
# it should be inactive/disabled — a running one is a redundant idle drain:
systemctl is-active actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service  # expect: inactive
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
and `projectplanner-batch.slice` (reconcile/narrate/ci-gate: CPUQuota~40%, Nice=10, low IOWeight,
memory-capped). Install with `deploy/apply-resource-guards.sh`; verify with
`bash scripts/verify_cgroup_slices.sh` and `bash scripts/verify_memory_isolation.sh`. If a batch
job still wedges the box, stop the timers to recover fast:
```bash
sudo systemctl stop projectplanner-{narrate,monitors,inbox,reconcile,summarize,ci-gate}.timer
sudo pkill -9 -f jobs.py   # then restart the web app if needed
```
If wedges recur, bump the instance to `t4g.small` (2 GB) and/or move the GitHub Actions runner +
CI gate off this box (per HARDEN-40).

**Acceptance:** `curl -m5 https://plan.taikunai.com/health` returns `200` in under 2s.

## Update live code
```bash
cd /opt/projectplanner && git pull && .venv/bin/pip install -r requirements.txt
PYTHON=.venv/bin/python SWITCHBOARD_CI_PYTHON=.venv/bin/python SWITCHBOARD_CI_STRICT=1 scripts/switchboard_ci.sh
sudo systemctl restart projectplanner projectplanner-mcp
sudo systemctl restart projectplanner-monitors.timer
sudo cp deploy/projectplanner-reconcile.service deploy/projectplanner-reconcile.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-reconcile.timer
sudo systemctl restart projectplanner-reconcile.timer
sudo cp deploy/projectplanner-ci-gate.service deploy/projectplanner-ci-gate.timer /etc/systemd/system/
sudo mkdir -p /var/lib/projectplanner/ci-gate
sudo chown -R ubuntu:ubuntu /var/lib/projectplanner/ci-gate
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-ci-gate.timer
sudo systemctl restart projectplanner-ci-gate.timer
sudo cp deploy/projectplanner-agent-host.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-agent-host
sudo systemctl restart projectplanner-agent-host
sudo cp deploy/projectplanner-narrate.service deploy/projectplanner-narrate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-narrate.timer
sudo systemctl restart projectplanner-narrate.timer
```

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
cd /opt/projectplanner && sudo -u ubuntu .venv/bin/python jobs.py narrate_pending
```

## VM-backed GitHub PR gate

Switchboard's canonical PR gate posts a GitHub commit status named `Switchboard CI / VM gate`.
GitHub-hosted Actions on the *private* canonical repo record `startup_failure` before creating
jobs, and would spend the org's private minutes anyway, so **CI runs off-box on a public sandbox**
— Route A of [`docs/CI-STRATEGY.md`](../docs/CI-STRATEGY.md). When `repo_topology.roles.public_ci`
is configured, the gate calls `external_ci_mirror` to push the exact merge SHA to the public
sandbox, dispatch the workflow on free GitHub-hosted runners, poll, and record an `external_ci_run`;
it falls back to the local `switchboard_ci.sh` venv suite only when `public_ci` is unset or the
mirror cannot dispatch. Either way the box runs no heavy test execution.

Provision the off-box path once (after step 3 installed `gh`):

```bash
# 1. Let git authenticate github.com HTTPS via gh + the CI token, so external_ci_mirror can
#    `git push` to the public sandbox. Run as the ci-gate user (ubuntu):
export GH_TOKEN=$(grep -E '^(SWITCHBOARD_CI_GITHUB_TOKEN|PM_GITHUB_TOKEN)=' /opt/projectplanner/.env | head -1 | cut -d= -f2-)
gh auth setup-git   # sets credential.https://github.com.helper = !gh auth git-credential

# 2. Declare the repo roles so the gate routes to the sandbox (MCP set_project_repo_topology or
#    POST /api/projects/switchboard/repo_topology):
#    canonical_repo=6th-Element-Labs/projectplanner  (the ONLY Done / code-truth authority)
#    public_ci_repo=6th-Element-Labs/projectplanner-ci  (verification-only, public)
#    public_ci_required_status_contexts=projectplanner-ci/full-suite
```

`.github/workflows/backend-tests.yml` on the canonical repo is **dispatch-only** (no `on:push`,
which would self-cancel under `cancel-in-progress`) and declares `source_sha`/`status_context`
inputs — both required by `external_ci_mirror`.

The on-box **self-hosted Actions runner is decommissioned** — CI runs on GitHub-hosted runners in
the sandbox, Actions are disabled on the canonical repo, so nothing dispatches to it and leaving it
running is a redundant idle drain. Keep it off; do NOT re-enable:

```bash
sudo systemctl disable --now actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service
```

## PR gate timer

The Plan VM keeps PR checks visible with `projectplanner-ci-gate.timer`. It runs:

```bash
/opt/projectplanner/.venv/bin/python /opt/projectplanner/jobs.py ci_gate_prs
```

The job checks out open non-draft PRs into `/var/lib/projectplanner/ci-gate`, runs the provenance
preflight, then verifies the suite **off-box via `external_ci_mirror`** (the public sandbox) when
`public_ci` is configured — falling back to `scripts/switchboard_ci.sh` in a local venv otherwise —
and posts a commit status named `Switchboard CI / VM gate` to each PR head SHA. It needs a token
with commit-status write **and push access to the public sandbox** in `PM_GITHUB_TOKEN`,
`GITHUB_TOKEN`, or `SWITCHBOARD_CI_GITHUB_TOKEN` (the gate exports it as `GH_TOKEN` for `gh`).

The gate must create its test venv with Python 3.10+ because strict CI installs `mcp>=1.9`.
`projectplanner-ci-gate.service` pins `SWITCHBOARD_CI_PYTHON=/opt/projectplanner/.venv/bin/python`;
if that interpreter is missing or unsupported, the gate posts a red status with the checked
candidate list instead of silently falling back to ambient `python3`.

## Rename safety

The live deployment should keep these compatibility names until Switchboard aliases are
implemented and verified:

- `/opt/projectplanner`
- `/var/lib/projectplanner`
- `projectplanner*.service` and `projectplanner*.timer`
- `PM_*` environment variables
- GitHub remote `6th-Element-Labs/projectplanner`

For the rename migration, first add aliases such as `/opt/switchboard -> /opt/projectplanner`
and `switchboard*.service` wrappers. Verify health, MCP, Agent Host, reconcile, and Tally
before making any alias canonical.

## Bootstrap direct-default provenance backfill
Use this only for legacy dogfood commits that landed directly on the default branch before the
PR webhook flow was enforced. Normal agent work still goes through `complete_claim` → PR merge
webhook → `Done`.

```bash
cd /opt/projectplanner
PM_BACKFILL_PROJECT=switchboard PM_BACKFILL_DRY_RUN=1 \
  .venv/bin/python jobs.py backfill_default_branch_provenance
# If the candidates are correct:
PM_BACKFILL_PROJECT=switchboard \
  .venv/bin/python jobs.py backfill_default_branch_provenance
```

## Scheduled reconcile projects

`projectplanner-reconcile.timer` runs `jobs.py reconcile_alerts`. By default that job now checks
all registered boards, not only `switchboard`, so project-scoped boards such as Helm can backfill
GitHub merge provenance from PR evidence even if their repo webhook is missing or delayed. To narrow
the scheduled surface deliberately, set `PM_RECON_ALERT_PROJECTS=switchboard` or a comma-separated
project list in `/opt/projectplanner/.env`, then restart `projectplanner-reconcile.timer`.

## Rebase timeline
```bash
# rebase kickoff (regenerates seed_plan.json); apply to the LIVE db with a dates-only UPDATE:
.venv/bin/python build_plan_artifacts.py 2026-06-01
.venv/bin/python - <<'PY'
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
