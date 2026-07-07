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

# App
sudo git clone <projectplanner-remote> /opt/projectplanner
cd /opt/projectplanner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r deploy/gateway/requirements.txt
sudo mkdir -p /var/lib/projectplanner/runner /var/lib/projectplanner/repo-hygiene-archive
sudo chown -R ubuntu:ubuntu /var/lib/projectplanner
cp .env.example .env   # set OPENAI_API_KEY + LLM_GATEWAY_MASTER_KEY (==PM_LLM_KEY)
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
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-gateway projectplanner projectplanner-mcp
sudo systemctl enable --now projectplanner-monitors.timer
sudo systemctl enable --now projectplanner-reconcile.timer
# Optional but recommended for Switchboard dogfood: consumes message-only wake intents.
# It uses PM_HOST_LANES=__MESSAGE_ONLY__ so it will not claim lane-scoped work.
sudo systemctl enable --now projectplanner-agent-host
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy
```
Caddy fetches the TLS cert automatically once DNS resolves. Visit https://plan.taikunai.com/.
The app will present the login screen in required mode. On first startup, the bootstrap admin
is created only if no password-backed admin exists for the project. After confirming login,
remove `PM_BOOTSTRAP_ADMIN_PASSWORD` from `.env` and restart `projectplanner`.

## 5. Verify
```bash
curl -s http://127.0.0.1:8110/health            # {"status":"ok",...}
curl -s http://127.0.0.1:8095/v1/models -H "Authorization: Bearer $LLM_GATEWAY_MASTER_KEY"
systemctl list-timers projectplanner-monitors.timer
systemctl list-timers projectplanner-reconcile.timer
systemctl list-timers projectplanner-ci-gate.timer
systemctl is-active projectplanner-agent-host
systemctl is-active actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service
```

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
```

## VM-backed GitHub PR gate

Switchboard's canonical PR gate is a VM-backed GitHub commit status named
`Switchboard CI / VM gate`. GitHub Actions is intentionally disabled while the hosted workflow
records `startup_failure` before creating jobs. The Plan VM still has a self-hosted runner
installed at `/opt/actions-runner-projectplanner` for future experiments:

```bash
sudo systemctl status actions.runner.6th-Element-Labs-projectplanner.plan-vm-switchboard-ci.service
cd /opt/actions-runner-projectplanner && sudo ./svc.sh status
```

If GitHub Actions is re-enabled later, prove it with one green PR and one green `master` push run
before making it a merge gate again.

## PR gate timer

The Plan VM keeps PR checks visible with `projectplanner-ci-gate.timer`. It runs:

```bash
/opt/projectplanner/.venv/bin/python /opt/projectplanner/jobs.py ci_gate_prs
```

The job checks out open non-draft PRs into `/var/lib/projectplanner/ci-gate`, runs
`scripts/switchboard_ci.sh` in strict mode, and posts a commit status named
`Switchboard CI / VM gate` to each PR head SHA. It needs a token with commit-status write access in
`PM_GITHUB_TOKEN`, `GITHUB_TOKEN`, or `SWITCHBOARD_CI_GITHUB_TOKEN`.

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
