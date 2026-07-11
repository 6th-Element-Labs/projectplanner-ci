#!/usr/bin/env bash
# PERF-4 / HARDEN-40: install interactive vs batch systemd slices so timer/oneshot
# jobs cannot starve the web/MCP request path on the small (2-vCPU / ~900 MB) box.
#
# PERF-3 zram swap (`deploy/setup-zram-swap.sh`) complements these slice caps.
#
# Slice hierarchy (declarative in deploy/*.slice + Slice= on each unit):
#   projectplanner-interactive.slice — web, MCP, gateway, agent host
#   projectplanner-batch.slice       — reconcile, narrate, ci-gate, timers
#
# Run once on a fresh box AFTER the units are installed and enabled (see
# PROVISION.md). Idempotent — re-running re-copies slices and clears legacy
# per-service set-property overrides so the repo units stay authoritative.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 644 "${DEPLOY_DIR}/projectplanner-interactive.slice" /etc/systemd/system/
install -m 644 "${DEPLOY_DIR}/projectplanner-batch.slice" /etc/systemd/system/
systemctl daemon-reload

INTERACTIVE_UNITS=(projectplanner projectplanner-mcp projectplanner-gateway projectplanner-agent-host)
BATCH_UNITS=(projectplanner-narrate projectplanner-monitors projectplanner-inbox
             projectplanner-reconcile projectplanner-summarize projectplanner-digest
             projectplanner-ci-gate projectplanner-backup)
LEGACY_PROPS=(CPUWeight MemoryLow MemoryHigh MemoryMax MemoryMin CPUQuota IOWeight MemorySwapMax)

for unit in "${INTERACTIVE_UNITS[@]}" "${BATCH_UNITS[@]}"; do
  for prop in "${LEGACY_PROPS[@]}"; do
    systemctl reset-property "${unit}.service" "${prop}" 2>/dev/null || true
  done
done

echo "PERF-4 slice guards installed. verify e.g.:"
echo "  bash scripts/verify_cgroup_slices.sh"
echo "  bash scripts/verify_memory_isolation.sh"
echo "  systemctl show projectplanner-interactive.slice -p CPUWeight -p MemoryLow -p MemorySwapMax"
echo "  systemctl show projectplanner-batch.slice -p CPUWeight -p CPUQuota -p IOWeight -p MemoryHigh"
echo "  systemctl show projectplanner.service -p Slice"
echo ""
echo "Restart interactive + batch units (or reboot) for Slice= assignments to take effect."
