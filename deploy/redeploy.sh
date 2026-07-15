#!/usr/bin/env bash
# Redeploy projectplanner on the Plan VM in one idempotent command: pull latest code, sync
# the systemd units AND the Caddyfile into /etc, then restart the services + reload Caddy.
#
# Why this exists: the repo carries deploy/Caddyfile and deploy/*.service, but the LIVE copies
# are /etc/caddy/Caddyfile and /etc/systemd/system/*. A bare `git pull` updates neither — so
# edge-config changes (security headers, timeouts, new routes) silently never reached prod
# until someone remembered the extra `cp`. This script makes that impossible to forget.
#
# Run on the VM as the app user (ubuntu); privileged steps use sudo:
#     cd /opt/projectplanner && bash deploy/redeploy.sh
#
# Env overrides:
#   PLAN_ROOT     deploy checkout (default /opt/projectplanner)
#   PLAN_CADDY_UNIT   caddy systemd unit (default caddy)
#   RUN_CI=1      run the local switchboard CI gate before restarting (CI normally runs off-box)
#   SKIP_CADDY=1  don't touch the Caddyfile / caddy this run
#   SKIP_RUNTIME_PROOF=1  skip post-deploy exact-SHA / runtime evidence check
#   HEALTH_TIMEOUT_SECONDS=30  bounded post-restart health window
#   HEALTH_INTERVAL_SECONDS=1  delay between health probes
#   CANONICAL_SHA  expected master SHA for runtime proof (default: origin/master)
set -euo pipefail

ROOT="${PLAN_ROOT:-/opt/projectplanner}"
CADDY_UNIT="${PLAN_CADDY_UNIT:-caddy}"
CADDY_LIVE="/etc/caddy/Caddyfile"
# Core web tier — always on; a redeploy must restart these and they must come back healthy.
# ARCH-MS-76: switchboard-auth is required once Caddy routes /api/auth* → :8121.
# ARCH-MS-101: switchboard-tasks is required once Caddy routes Mode A Tasks → :8122.
APP_SERVICES=(projectplanner-gateway projectplanner projectplanner-mcp switchboard-auth switchboard-tasks)
# Local health URLs that must be 200 BEFORE any live Caddyfile overwrite.
# Order: monolith, then every process-cut routed by the edge.
REQUIRED_HEALTH_URLS=(
    http://127.0.0.1:8110/health
    http://127.0.0.1:8121/health
    http://127.0.0.1:8122/health
)
# Auxiliary units (timers + agent host). Restarted only if currently active, so unit-file
# changes take effect without force-starting a unit an operator deliberately stopped (e.g.
# timers halted during a HARDEN-32 wedge). A brand-new unit still needs a one-time
# `sudo systemctl enable --now <unit>` — this is a redeploy, not first-time provisioning.
# First Auth/Tasks cutover: enable cut units BEFORE reloading Caddy (see PROVISION.md).
AUX_UNITS=(projectplanner-agent-host.service
    projectplanner-monitors.timer projectplanner-reconcile.timer
    projectplanner-coordinator-audit.timer projectplanner-claim-gate.timer
    projectplanner-narrate.timer projectplanner-digest.timer projectplanner-inbox.timer
    projectplanner-summarize.timer projectplanner-backup.timer)

section() { printf '\n== %s ==\n' "$1"; }

cd "$ROOT"

# 1. Pull, then re-exec the freshly-pulled copy of this script. bash reads a script
#    incrementally, so pulling a new version mid-run could execute a half-old/half-new
#    file; the re-exec (guarded against looping) runs the updated script cleanly.
if [ -z "${_REDEPLOY_PULLED:-}" ]; then
    section "git pull"
    # HARDEN-55: the code tree is root-owned/read-only to the runtime, so pull as root.
    sudo git pull --ff-only
    exec env _REDEPLOY_PULLED=1 bash "$ROOT/deploy/redeploy.sh" "$@"
fi

# 2. Python deps (app + LLM gateway). Root-owned venv (HARDEN-55) → install as root.
section "pip install"
sudo .venv/bin/pip install -q -r requirements.txt -r deploy/gateway/requirements.txt

# 3. Optional local CI gate. CI runs off-box now (public sandbox); opt in with RUN_CI=1
#    if you want the on-box strict gate to guard this deploy.
if [ "${RUN_CI:-0}" = "1" ]; then
    section "CI gate"
    PYTHON=.venv/bin/python SWITCHBOARD_CI_PYTHON=.venv/bin/python SWITCHBOARD_CI_STRICT=1 \
        scripts/switchboard_ci.sh
fi

# 4. Sync systemd units into /etc and pick up unit-file changes.
section "systemd units"
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
# HARDEN-55: re-assert the least-privilege posture (dedicated service account, root-owned
# read-only code tree, service-owned data dir incl. the CI-12 source clone). Idempotent.
sudo bash deploy/apply-least-privilege.sh
sudo systemctl daemon-reload

# 5. Restart the web tier (strict) + any active auxiliary units so new code/units take effect.
#    Auth (:8121) and Tasks (:8122) must be healthy BEFORE Caddy reloads their edge handles.
section "restart services"
sudo systemctl enable switchboard-auth switchboard-tasks >/dev/null 2>&1 || true
sudo systemctl restart "${APP_SERVICES[@]}"
for u in "${AUX_UNITS[@]}"; do
    if systemctl is-active --quiet "$u"; then
        sudo systemctl restart "$u"
    fi
done

# 6–7. Prove every routed service healthy, then sync Caddy fail-closed.
#     A failed health check preserves the prior live Caddyfile (ARCH-MS-101).
section "Caddy (fail-closed)"
export PLAN_ROOT="$ROOT"
export PLAN_CADDY_UNIT="$CADDY_UNIT"
export CADDY_LIVE
bash "$ROOT/deploy/sync_caddy_fail_closed.sh" "${REQUIRED_HEALTH_URLS[@]}"

# 8. Exact-SHA / runtime evidence for subsequent service cuts (reusable harness).
if [ "${SKIP_RUNTIME_PROOF:-0}" != "1" ]; then
    section "runtime proof"
    CANONICAL_SHA="${CANONICAL_SHA:-$(git rev-parse origin/master 2>/dev/null || git rev-parse HEAD)}"
    PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
    if [ ! -x "$PYTHON" ]; then
        PYTHON=python3
    fi
    "$PYTHON" "$ROOT/scripts/verify_runtime_deploy.py" \
        --root "$ROOT" \
        --canonical-sha "$CANONICAL_SHA" \
        --caddy-live "$CADDY_LIVE" \
        --service switchboard-auth:8121 \
        --service switchboard-tasks:8122 \
        --edge-owns '/api/auth*:8121' \
        --edge-owns '/api/tasks*:8122'
fi

echo "redeploy complete."
