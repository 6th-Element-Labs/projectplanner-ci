#!/usr/bin/env bash
# HARDEN-40 / PERF-3: pin cgroup resource guards so batch/timer jobs cannot starve
# the interactive tier on the small (2-vCPU / ~900 MB) box.
#
# PERF-3 adds MemoryMin + MemoryLow + MemorySwapMax=0 on web/MCP/gateway and
# MemoryMax hard caps on batch jobs so OOM is contained instead of swap-thrashing
# the request path. Pair with deploy/setup-zram-swap.sh.
set -euo pipefail

apply_interactive() {
  local unit=$1 cpu=$2 mem_min=$3 mem_low=$4
  systemctl set-property "$unit" \
    "CPUWeight=${cpu}" \
    "MemoryMin=${mem_min}" \
    "MemoryLow=${mem_low}" \
    "MemorySwapMax=0"
}

apply_batch() {
  local unit=$1 cpu=$2 mem_high=$3 mem_max=$4
  systemctl set-property "$unit" \
    "CPUWeight=${cpu}" \
    "MemoryHigh=${mem_high}" \
    "MemoryMax=${mem_max}"
}

apply_interactive projectplanner.service 900 200M 250M
apply_interactive projectplanner-mcp.service 400 120M 150M
apply_interactive projectplanner-gateway.service 300 80M 100M

for unit in narrate monitors inbox reconcile summarize digest; do
  apply_batch "projectplanner-${unit}.service" 20 180M 220M
done

apply_batch projectplanner-ci-gate.service 10 220M 320M

echo "resource guards applied. verify e.g.:"
echo "  bash scripts/verify_memory_isolation.sh"
echo "  systemctl show projectplanner.service -p MemoryMin -p MemoryLow -p MemorySwapMax"
echo "  systemctl show projectplanner-ci-gate.service -p MemoryMax"
