#!/usr/bin/env bash
# HARDEN-32 — diagnose and recover plan.taikunai.com when curls hang after TLS.
# Run on the Plan VM as ubuntu (or with sudo where noted).
set -euo pipefail

APP_UNIT="${PLAN_APP_UNIT:-projectplanner}"
CADDY_UNIT="${PLAN_CADDY_UNIT:-caddy}"
LOCAL_HEALTH="http://127.0.0.1:8110/health"
PUBLIC_HEALTH="${PLAN_PUBLIC_HEALTH:-https://plan.taikunai.com/health}"

section() { printf '\n== %s ==\n' "$1"; }

section "service state"
systemctl is-active "$APP_UNIT" "$CADDY_UNIT" projectplanner-mcp projectplanner-gateway 2>/dev/null || true
systemctl --no-pager --full status "$APP_UNIT" | sed -n '1,12p' || true

section "memory / disk"
free -h || true
df -h / /var/lib/projectplanner /opt/projectplanner 2>/dev/null || df -h /

section "local health (5s cap)"
if curl -sS -m 5 -o /tmp/plan-health.json -w 'local_health code=%{http_code} total=%{time_total}s\n' "$LOCAL_HEALTH"; then
  head -c 200 /tmp/plan-health.json; echo
else
  echo "local_health FAILED — uvicorn likely hung or not listening on 8110"
fi

section "recent app logs"
journalctl -u "$APP_UNIT" -n 40 --no-pager || true

section "recover"
read -r -p "Restart $APP_UNIT and $CADDY_UNIT now? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  sudo systemctl restart "$APP_UNIT" projectplanner-mcp
  sudo cp /opt/projectplanner/deploy/Caddyfile /etc/caddy/Caddyfile
  sudo caddy validate --config /etc/caddy/Caddyfile
  sudo systemctl restart "$CADDY_UNIT"
  sleep 2
  curl -sS -m 5 "$LOCAL_HEALTH" && echo
fi

section "public health (10s cap)"
curl -sS -m 10 -o /dev/null -w 'public_health code=%{http_code} total=%{time_total}s\n' "$PUBLIC_HEALTH" || true
