#!/usr/bin/env bash
# CI-7 operator teardown: retire on-box VM CI apparatus after pull-model verify holds.
#
# Run on the Plan VM as a user with sudo (ubuntu). Idempotent — safe to re-run.
# Keeps .bak unit copies in deploy/retired/ for one week rollback.
#
# Usage:
#   cd /opt/projectplanner && sudo bash deploy/ci7-teardown-box-ci.sh
#
# Env:
#   PLAN_ROOT          deploy checkout (default /opt/projectplanner)
#   SKIP_RULESET=1     skip deleting GitHub ruleset 18821466
#   SKIP_DATA_PURGE=1  keep /var/lib/projectplanner/ci-gate (bare mirror + cache)
set -euo pipefail

ROOT="${PLAN_ROOT:-/opt/projectplanner}"
REPO="${GITHUB_REPOSITORY:-6th-Element-Labs/projectplanner}"
CI_GATE_DIR="/var/lib/projectplanner/ci-gate"
RETIRED_UNITS=(
  projectplanner-ci-gate.timer
  projectplanner-ci-gate.service
  projectplanner-ci-gate-request.path
  projectplanner-ci-gate-request.service
)

section() { printf '\n== %s ==\n' "$1"; }

section "stop and disable retired box CI units"
for u in "${RETIRED_UNITS[@]}"; do
  sudo systemctl stop "$u" 2>/dev/null || true
  sudo systemctl disable "$u" 2>/dev/null || true
  if [ -f "/etc/systemd/system/$u" ]; then
    sudo cp "/etc/systemd/system/$u" "/etc/systemd/system/${u}.bak-ci7"
    sudo rm -f "/etc/systemd/system/$u"
  fi
done
sudo systemctl daemon-reload

section "enable claim-gate timer (SESSION-12 only on box)"
sudo cp "$ROOT/deploy/projectplanner-claim-gate.service" /etc/systemd/system/
sudo cp "$ROOT/deploy/projectplanner-claim-gate.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-claim-gate.timer

if [ "${SKIP_DATA_PURGE:-0}" != "1" ]; then
  section "remove box CI state (bare mirror, worktrees, request markers)"
  if [ -d "$CI_GATE_DIR" ]; then
    sudo rm -rf "$CI_GATE_DIR"
    echo "removed $CI_GATE_DIR"
  else
    echo "no $CI_GATE_DIR — already clean"
  fi
else
  echo "-- SKIP_DATA_PURGE=1: leaving $CI_GATE_DIR in place"
fi

if [ "${SKIP_RULESET:-0}" != "1" ] && command -v gh >/dev/null 2>&1; then
  section "delete disabled merge-queue ruleset 18821466"
  if gh api "repos/${REPO}/rulesets/18821466" >/dev/null 2>&1; then
    gh api "repos/${REPO}/rulesets/18821466" -X DELETE
    echo "deleted ruleset 18821466 on ${REPO}"
  else
    echo "ruleset 18821466 not found (already deleted)"
  fi
else
  echo "-- skipping ruleset delete (SKIP_RULESET=1 or gh missing)"
fi

section "verify"
systemctl list-timers projectplanner-claim-gate.timer --no-pager || true
for u in "${RETIRED_UNITS[@]}"; do
  if systemctl is-enabled "$u" 2>/dev/null; then
    echo "!! $u is still enabled" >&2
    exit 1
  fi
done
echo "CI-7 box CI teardown complete."
