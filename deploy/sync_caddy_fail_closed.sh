#!/usr/bin/env bash
# Fail-closed Caddy sync for process-cut services (ARCH-MS-101).
#
# Prove every required local health URL is 200, validate the repo Caddyfile,
# then copy it to the live path and reload. Any health/validation failure
# leaves the prior live Caddyfile untouched (dead-unit must never own the edge).
#
# Usage (from repo root or PLAN_ROOT):
#   bash deploy/sync_caddy_fail_closed.sh \
#     http://127.0.0.1:8110/health \
#     http://127.0.0.1:8121/health \
#     http://127.0.0.1:8122/health
#
# Env:
#   PLAN_ROOT       deploy checkout (default /opt/projectplanner)
#   CADDY_LIVE      live edge path (default /etc/caddy/Caddyfile)
#   PLAN_CADDY_UNIT caddy systemd unit (default caddy)
#   REPO_CADDY      repo Caddyfile (default $PLAN_ROOT/deploy/Caddyfile)
#   SKIP_CADDY=1    exit 0 without touching the edge
set -euo pipefail

ROOT="${PLAN_ROOT:-/opt/projectplanner}"
CADDY_UNIT="${PLAN_CADDY_UNIT:-caddy}"
CADDY_LIVE="${CADDY_LIVE:-/etc/caddy/Caddyfile}"
REPO_CADDY="${REPO_CADDY:-$ROOT/deploy/Caddyfile}"

if [ "${SKIP_CADDY:-0}" = "1" ]; then
    echo "-- SKIP_CADDY=1 — leaving live $CADDY_LIVE untouched"
    exit 0
fi

if [ "$#" -lt 1 ]; then
    echo "!! usage: $0 <health-url> [health-url...]" >&2
    exit 2
fi

if ! command -v caddy >/dev/null 2>&1; then
    echo "-- caddy not installed — leaving live $CADDY_LIVE untouched"
    exit 0
fi

section() { printf '\n== %s ==\n' "$1"; }

section "health (pre-Caddy, fail-closed)"
for url in "$@"; do
    if ! HEALTH_URL="$url" bash "$ROOT/deploy/wait-for-health.sh"; then
        echo "!! health failed for $url — leaving live $CADDY_LIVE untouched" >&2
        exit 1
    fi
done

section "Caddyfile"
if ! caddy validate --adapter caddyfile --config "$REPO_CADDY"; then
    echo "!! $REPO_CADDY failed validation — leaving live $CADDY_LIVE untouched" >&2
    exit 1
fi

# Only overwrite the live edge after every routed-service health probe passed.
sudo cp "$REPO_CADDY" "$CADDY_LIVE"
sudo systemctl reload "$CADDY_UNIT" || sudo systemctl restart "$CADDY_UNIT"
echo "Caddy sync complete (live edge updated)."
