#!/usr/bin/env bash
# BUG-114: bring the running system to canonical master without a human in the loop.
#
# A merge to master is CI-green by construction (the merge queue gates every
# commit), so "origin/master advanced past HEAD" is a sufficient, safe trigger to
# redeploy. This wrapper is the TRIGGER; deploy/redeploy.sh remains the deploy
# mechanism (pull -> deps -> units -> restart -> Caddy fail-closed -> runtime
# proof + rollback). Run it from a short-interval systemd timer:
#
#     projectplanner-autodeploy.timer -> projectplanner-autodeploy.service
#
# Guarantees:
#   * flock so two ticks (or a tick overlapping a long redeploy) never run
#     redeploy concurrently — the second exits 0 immediately.
#   * Refreshes the /health/version staleness signal on EVERY tick, deploy or
#     not, so "prod is N behind master" is always fresh even between deploys.
#   * Deploys ONLY when behind; a redeploy failure is recorded in the signal and
#     surfaces a non-zero exit (redeploy.sh has already rolled back the edge).
#
# Overrides (env) — defaults target the prod VM; tests inject fakes:
#   PLAN_ROOT                 deploy checkout (default /opt/projectplanner)
#   PM_DEPLOY_STATE_FILE      staleness state file (default via deploy_staleness)
#   AUTODEPLOY_PYTHON         python for deploy_staleness.py (default venv/py3)
#   AUTODEPLOY_REDEPLOY_CMD   deploy command (default: bash $ROOT/deploy/redeploy.sh)
#   AUTODEPLOY_LOCK_FILE      flock path (default: <state dir>/auto-deploy.lock)
#   AUTODEPLOY_CANONICAL_REF  ref to track (default origin/master)
#   AUTODEPLOY_SKIP_FETCH=1   skip git fetch (tests / already-fetched)
set -euo pipefail

ROOT="${PLAN_ROOT:-/opt/projectplanner}"
CANONICAL_REF="${AUTODEPLOY_CANONICAL_REF:-origin/master}"
PYTHON="${AUTODEPLOY_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then PYTHON=python3; fi
REDEPLOY_CMD="${AUTODEPLOY_REDEPLOY_CMD:-bash $ROOT/deploy/redeploy.sh}"

STATE_FILE="${PM_DEPLOY_STATE_FILE:-}"
if [ -z "$STATE_FILE" ]; then
    # Resolve the module default (honours PM_DB_PATH) without duplicating logic.
    STATE_FILE="$("$PYTHON" - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import deploy_staleness
print(deploy_staleness.default_state_path())
PY
)"
fi
export PM_DEPLOY_STATE_FILE="$STATE_FILE"
STATE_DIR="$(dirname "$STATE_FILE")"
LOCK_FILE="${AUTODEPLOY_LOCK_FILE:-$STATE_DIR/auto-deploy.lock}"

log() { printf '[auto-deploy] %s\n' "$1"; }

refresh_signal() {
    # Reads local refs only; the privileged fetch runs in run_deploy_cycle first.
    "$PYTHON" "$ROOT/deploy_staleness.py" refresh \
        --root "$ROOT" --state "$STATE_FILE" --canonical-ref "$CANONICAL_REF"
}

run_deploy_cycle() {
    mkdir -p "$STATE_DIR"

    # 1. Fetch canonical master WITH privilege. HARDEN-55 makes the prod code tree
    #    root-owned, so an unprivileged `git fetch` cannot write .git and would
    #    silently leave origin/master stale — the trigger would then never see a
    #    new commit. redeploy.sh solves the same problem with `sudo git pull`;
    #    mirror it here. AUTODEPLOY_SUDO is emptied by tests (user-owned repo).
    local branch="${CANONICAL_REF#*/}"
    if [ "${AUTODEPLOY_SKIP_FETCH:-0}" != "1" ]; then
        ${AUTODEPLOY_SUDO-sudo} git -C "$ROOT" fetch --quiet origin "$branch" \
            || log "git fetch failed; comparing against last-known origin/master"
    fi

    # 2. Refresh the signal from local refs (reads only; the fetch already ran).
    local payload
    payload="$(refresh_signal)"
    local behind canonical
    behind="$("$PYTHON" -c 'import json,sys;print(json.loads(sys.argv[1])["commits_behind"])' "$payload")"
    canonical="$("$PYTHON" -c 'import json,sys;print(json.loads(sys.argv[1])["canonical_sha"])' "$payload")"

    if [ "$behind" = "0" ]; then
        log "up to date ($canonical); no deploy needed"
        return 0
    fi

    # 3. Behind -> deploy. redeploy.sh owns pull/restart/proof/rollback.
    log "prod is $behind commit(s) behind $CANONICAL_REF ($canonical); deploying"
    local rc=0
    # shellcheck disable=SC2086
    $REDEPLOY_CMD || rc=$?

    # 4. Record the outcome in the signal (recomputes running SHA; no re-fetch).
    if [ "$rc" -eq 0 ]; then
        "$PYTHON" "$ROOT/deploy_staleness.py" record-deploy \
            --root "$ROOT" --state "$STATE_FILE" --deployed-sha "$canonical" \
            --canonical-ref "$CANONICAL_REF" --ok >/dev/null
        log "deploy complete -> $canonical"
    else
        "$PYTHON" "$ROOT/deploy_staleness.py" record-deploy \
            --root "$ROOT" --state "$STATE_FILE" --deployed-sha "$canonical" \
            --canonical-ref "$CANONICAL_REF" --failed \
            --error "redeploy exited $rc" >/dev/null
        log "deploy FAILED (rc=$rc); redeploy.sh rolled back; signal records failure"
        return "$rc"
    fi
}

# Lock the whole cycle so a concurrent tick (or one overlapping a long redeploy)
# never starts a second redeploy. An atomic `mkdir` is the portable primitive
# (no flock dependency); the EXIT trap releases it on any normal exit, including
# a redeploy failure. A lock left by a hard-killed process is reclaimed once it
# ages past AUTODEPLOY_LOCK_STALE_SECONDS (default 30m) — long enough that a
# genuinely in-flight deploy is never interrupted, so a wedged deploy safely
# pauses further deploys rather than piling on.
mkdir -p "$STATE_DIR"
LOCK_DIR="$LOCK_FILE.d"
STALE_SECONDS="${AUTODEPLOY_LOCK_STALE_SECONDS:-1800}"

lock_age_seconds() {
    local now mtime
    now="$(date +%s)"
    mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo "$now")"
    echo $(( now - mtime ))
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ "$(lock_age_seconds)" -ge "$STALE_SECONDS" ]; then
        log "reclaiming a stale auto-deploy lock (older than ${STALE_SECONDS}s)"
        rmdir "$LOCK_DIR" 2>/dev/null || true
        if ! mkdir "$LOCK_DIR" 2>/dev/null; then
            log "another auto-deploy run holds the lock; skipping this tick"
            exit 0
        fi
    else
        log "another auto-deploy run holds the lock; skipping this tick"
        exit 0
    fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

run_deploy_cycle
