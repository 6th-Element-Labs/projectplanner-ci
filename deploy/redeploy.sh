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
#   HEALTH_TIMEOUT_SECONDS=30  bounded post-restart health window
#   HEALTH_INTERVAL_SECONDS=1  delay between health probes
set -euo pipefail

ROOT="${PLAN_ROOT:-/opt/projectplanner}"
CADDY_UNIT="${PLAN_CADDY_UNIT:-caddy}"
CADDY_LIVE="/etc/caddy/Caddyfile"
# Core web tier — always on; a redeploy must restart these and they must come back healthy.
# ARCH-MS-76: switchboard-auth is required once Caddy routes /api/auth* → :8121.
APP_SERVICES=(projectplanner-gateway projectplanner projectplanner-mcp switchboard-auth)
# Auxiliary units (timers + agent host). Restarted only if currently active, so unit-file
# changes take effect without force-starting a unit an operator deliberately stopped (e.g.
# timers halted during a HARDEN-32 wedge). A brand-new unit still needs a one-time
# `sudo systemctl enable --now <unit>` — this is a redeploy, not first-time provisioning.
# First Auth cutover: enable switchboard-auth BEFORE reloading Caddy (see PROVISION.md).
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
#    Auth (:8121) must be healthy BEFORE Caddy reloads /api/auth* onto it (ARCH-MS-76).
section "restart services"
sudo systemctl enable switchboard-auth >/dev/null 2>&1 || true
sudo systemctl restart "${APP_SERVICES[@]}"
for u in "${AUX_UNITS[@]}"; do
    if systemctl is-active --quiet "$u"; then
        sudo systemctl restart "$u"
    fi
done

# 6. Prove Auth + monolith health before touching the edge.
section "health (pre-Caddy)"
if ! bash "$ROOT/deploy/wait-for-health.sh"; then
    echo "!! /health is not 200 after restart — inspect: journalctl -u projectplanner -n 60 --no-pager" >&2
    exit 1
fi
if ! HEALTH_URL=http://127.0.0.1:8121/health \
    bash "$ROOT/deploy/wait-for-health.sh"; then
    echo "!! Auth /health is not 200 — inspect: journalctl -u switchboard-auth -n 60 --no-pager" >&2
    exit 1
fi

# 7. Sync the Caddyfile into /etc and reload Caddy — the step a bare `git pull` skips.
#    Validate the repo copy BEFORE overwriting the live one: never reload a broken edge.
if [ "${SKIP_CADDY:-0}" != "1" ] && command -v caddy >/dev/null 2>&1; then
    section "Caddyfile"
    if caddy validate --adapter caddyfile --config deploy/Caddyfile; then
        sudo cp deploy/Caddyfile "$CADDY_LIVE"
        # reload is graceful (no dropped connections, no cert re-fetch); fall back to restart.
        sudo systemctl reload "$CADDY_UNIT" || sudo systemctl restart "$CADDY_UNIT"
    else
        echo "!! deploy/Caddyfile failed validation — leaving live $CADDY_LIVE untouched" >&2
        exit 1
    fi
else
    echo "-- skipping Caddy sync (SKIP_CADDY=1 or caddy not installed)"
fi

echo "redeploy complete."
