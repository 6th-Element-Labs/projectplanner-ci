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
APP_SERVICES=(projectplanner-gateway projectplanner projectplanner-mcp switchboard-auth switchboard-tasks switchboard-coord)
# Local health URLs that must be 200 BEFORE any live Caddyfile overwrite.
# Order: monolith, then every process-cut routed by the edge.
REQUIRED_HEALTH_URLS=(
    http://127.0.0.1:8110/health
    http://127.0.0.1:8121/health
    http://127.0.0.1:8122/health
    http://127.0.0.1:8123/health
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

# BUG-70 / ARCH-MS-106: snapshot every live process-cut topology before mutation.
# The authenticated proof runs after Caddy reload, so a failed proof must restore
# the prior edge and the monolith's prior dual-strip settings.
ROLLBACK_DIR="$(mktemp -d /tmp/projectplanner-redeploy.XXXXXX)"
TASKS_WAS_ACTIVE="$(systemctl is-active switchboard-tasks 2>/dev/null || true)"
TASKS_WAS_ENABLED="$(systemctl is-enabled switchboard-tasks 2>/dev/null || true)"
COORD_WAS_ACTIVE="$(systemctl is-active switchboard-coord 2>/dev/null || true)"
COORD_WAS_ENABLED="$(systemctl is-enabled switchboard-coord 2>/dev/null || true)"
PROJECTPLANNER_UNIT_LIVE="${PROJECTPLANNER_UNIT_LIVE:-/etc/systemd/system/projectplanner.service}"
TASKS_UNIT_LIVE="${TASKS_UNIT_LIVE:-/etc/systemd/system/switchboard-tasks.service}"
COORD_UNIT_LIVE="${COORD_UNIT_LIVE:-/etc/systemd/system/switchboard-coord.service}"
for snapshot in \
    "$CADDY_LIVE:Caddyfile" \
    "$PROJECTPLANNER_UNIT_LIVE:projectplanner.service" \
    "$TASKS_UNIT_LIVE:switchboard-tasks.service" \
    "$COORD_UNIT_LIVE:switchboard-coord.service"
do
    source_path="${snapshot%%:*}"
    snapshot_name="${snapshot#*:}"
    if sudo test -f "$source_path"; then
        sudo cp "$source_path" "$ROLLBACK_DIR/$snapshot_name"
        touch "$ROLLBACK_DIR/$snapshot_name.present"
    fi
done

cleanup_redeploy_snapshot() {
    sudo rm -rf "$ROLLBACK_DIR"
}

restore_tasks_cut_topology() {
    section "rollback failed runtime proof"
    local rollback_rc=0
    local monolith_ready=1
    local edge_ready=0
    local coord_tracked=0
    if [ "${COORD_WAS_ACTIVE+x}" = x ]; then
        coord_tracked=1
    fi
    set +e

    # Restore the monolith unit first while the new edge still has healthy cuts.
    if [ -f "$ROLLBACK_DIR/projectplanner.service.present" ]; then
        sudo cp "$ROLLBACK_DIR/projectplanner.service" \
            "$PROJECTPLANNER_UNIT_LIVE" || { rollback_rc=1; monolith_ready=0; }
    else
        rollback_rc=1
        monolith_ready=0
    fi
    if [ -f "$ROLLBACK_DIR/switchboard-tasks.service.present" ]; then
        sudo cp "$ROLLBACK_DIR/switchboard-tasks.service" \
            "$TASKS_UNIT_LIVE" || rollback_rc=1
    fi
    if [ -f "$ROLLBACK_DIR/switchboard-coord.service.present" ]; then
        sudo cp "$ROLLBACK_DIR/switchboard-coord.service" \
            "$COORD_UNIT_LIVE" || rollback_rc=1
    fi
    sudo systemctl daemon-reload || { rollback_rc=1; monolith_ready=0; }
    sudo systemctl restart projectplanner || { rollback_rc=1; monolith_ready=0; }
    if ! HEALTH_URL=http://127.0.0.1:8110/health \
        bash "$ROOT/deploy/wait-for-health.sh"
    then
        rollback_rc=1
        monolith_ready=0
    fi

    # Only after the prior monolith mode is healthy, restore the previous edge.
    if [ "$monolith_ready" -eq 1 ] && [ -f "$ROLLBACK_DIR/Caddyfile.present" ]; then
        if sudo cp "$ROLLBACK_DIR/Caddyfile" "$CADDY_LIVE" \
            && { sudo systemctl reload "$CADDY_UNIT" \
                || sudo systemctl restart "$CADDY_UNIT"; }
        then
            edge_ready=1
        else
            rollback_rc=1
            echo "!! prior Caddy edge could not be restored; preserving current cut services" >&2
        fi
    elif [ -f "$ROLLBACK_DIR/Caddyfile.present" ]; then
        echo "!! restored monolith is unhealthy; preserving the current Caddy edge" >&2
    else
        rollback_rc=1
        echo "!! prior Caddy snapshot is missing; preserving current edge and cut services" >&2
    fi

    # Restore the old Tasks lifecycle only after the old monolith/edge is safe.
    # Otherwise the current edge may still depend on the live :8122 process.
    if [ "$monolith_ready" -eq 1 ] && [ "$edge_ready" -eq 1 ]; then
        case "$TASKS_WAS_ACTIVE" in
            active)
                sudo systemctl restart switchboard-tasks || rollback_rc=1
                HEALTH_URL=http://127.0.0.1:8122/health \
                    bash "$ROOT/deploy/wait-for-health.sh" || rollback_rc=1
                ;;
            *) sudo systemctl stop switchboard-tasks || rollback_rc=1 ;;
        esac
        case "$TASKS_WAS_ENABLED" in
            enabled) sudo systemctl enable switchboard-tasks >/dev/null 2>&1 || rollback_rc=1 ;;
            *) sudo systemctl disable switchboard-tasks >/dev/null 2>&1 || rollback_rc=1 ;;
        esac
        if [ ! -f "$ROLLBACK_DIR/switchboard-tasks.service.present" ]; then
            sudo rm -f "$TASKS_UNIT_LIVE" || rollback_rc=1
            sudo systemctl daemon-reload || rollback_rc=1
        fi
        if [ "$coord_tracked" -eq 1 ]; then
            case "$COORD_WAS_ACTIVE" in
                active)
                    sudo systemctl restart switchboard-coord || rollback_rc=1
                    HEALTH_URL=http://127.0.0.1:8123/health \
                        bash "$ROOT/deploy/wait-for-health.sh" || rollback_rc=1
                    ;;
                *) sudo systemctl stop switchboard-coord || rollback_rc=1 ;;
            esac
            case "$COORD_WAS_ENABLED" in
                enabled) sudo systemctl enable switchboard-coord >/dev/null 2>&1 || rollback_rc=1 ;;
                *) sudo systemctl disable switchboard-coord >/dev/null 2>&1 || rollback_rc=1 ;;
            esac
            if [ ! -f "$ROLLBACK_DIR/switchboard-coord.service.present" ]; then
                sudo rm -f "$COORD_UNIT_LIVE" || rollback_rc=1
                sudo systemctl daemon-reload || rollback_rc=1
            fi
        fi
    fi

    set -e
    if [ "$rollback_rc" -ne 0 ]; then
        echo "!! automatic process-cut topology rollback was incomplete" >&2
        return 1
    fi
    echo "Process-cut topology rollback complete."
}

fail_runtime_proof() {
    local reason="$1"
    echo "!! $reason; restoring the pre-deploy process-cut topology" >&2
    exit 1
}

# Arm before the first unit mutation. Any error, INT, or TERM from here through
# the authenticated proof restores the complete pre-deploy topology. The guard
# clears its own traps before invoking rollback, so a rollback failure cannot recurse.
# shellcheck source=deploy/redeploy_rollback_guard.sh
source "$ROOT/deploy/redeploy_rollback_guard.sh"
rollback_guard_arm restore_tasks_cut_topology cleanup_redeploy_snapshot

# 4. Sync systemd units into /etc and pick up unit-file changes.
section "systemd units"
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
# HARDEN-55: re-assert the least-privilege posture (dedicated service account, root-owned
# read-only code tree, service-owned data dir incl. the CI-12 source clone). Idempotent.
sudo bash deploy/apply-least-privilege.sh
sudo systemctl daemon-reload

# 5. Restart the web tier (strict) + any active auxiliary units so new code/units take effect.
#    Auth (:8121), Tasks (:8122), and Coord (:8123) must be healthy before Caddy.
section "restart services"
sudo systemctl enable switchboard-auth switchboard-tasks switchboard-coord >/dev/null 2>&1
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
    # BUG-70: the final gate must exercise the authenticated public edge, not only
    # parse intended Caddy ownership. Read the existing MCP bearer without printing it.
    if [ -z "${PM_RUNTIME_PROOF_TOKEN:-}" ]; then
        PM_RUNTIME_PROOF_TOKEN="$(sudo awk -F= '$1 == "PM_MCP_TOKEN" {
            sub(/^[^=]*=/, ""); gsub(/^\"|\"$/, ""); print; exit
        }' "$ROOT/.env" || true)"
    fi
    if [ -z "$PM_RUNTIME_PROOF_TOKEN" ]; then
        fail_runtime_proof "PM_MCP_TOKEN unavailable for authenticated edge proof"
    fi
    if ! PM_RUNTIME_PROOF_TOKEN="$PM_RUNTIME_PROOF_TOKEN" \
        "$PYTHON" "$ROOT/scripts/verify_runtime_deploy.py" \
            --root "$ROOT" \
            --canonical-sha "$CANONICAL_SHA" \
            --caddy-live "$CADDY_LIVE" \
            --service switchboard-auth:8121 \
            --service switchboard-tasks:8122 \
            --service switchboard-coord:8123 \
            --edge-owns '/api/auth*:8121' \
            --edge-owns '/api/tasks*:8122' \
            --edge-owns '/api/board:8123' \
            --edge-owns '/api/signals:8123' \
            --edge-owns '/ixp/v1/delta:8123' \
            --edge-owns '/api/coordination:8123' \
            --edge-owns '/api/coordinator_decisions:8123' \
            --edge-base-url "${PM_BASE:-https://plan.taikunai.com}" \
            --probe-task-id "${RUNTIME_PROOF_TASK_ID:-}"
    then
        fail_runtime_proof "authenticated runtime proof failed"
    fi
fi

rollback_guard_disarm
echo "redeploy complete."
