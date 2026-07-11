#!/usr/bin/env bash
# PERF-4 acceptance checks for interactive vs batch systemd slice hierarchy.
# Run on the Plan VM after unit files are installed and apply-resource-guards.sh.
set -euo pipefail

failures=0

check() {
  local label=$1
  shift
  if "$@"; then
    printf '  PASS  %s\n' "$label"
  else
    printf '  FAIL  %s\n' "$label"
    failures=$((failures + 1))
  fi
}

prop_eq() {
  local unit=$1 property=$2 expected=$3
  local actual
  actual="$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)"
  [[ "$actual" == "$expected" ]]
}

prop_max_bytes() {
  local unit=$1 property=$2 min_bytes=$3
  local actual
  actual="$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)"
  [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= min_bytes ))
}

section() { printf '\n== %s ==\n' "$1"; }

section "interactive slice"
check "projectplanner-interactive.slice MemorySwapMax=0" \
  prop_eq projectplanner-interactive.slice MemorySwapMax 0
check "projectplanner-interactive.slice MemoryLow >= 250M" \
  prop_max_bytes projectplanner-interactive.slice MemoryLow 262144000
check "projectplanner-interactive.slice CPUWeight >= 900" bash -c '
  w=$(systemctl show projectplanner-interactive.slice -p CPUWeight --value)
  [[ "$w" =~ ^[0-9]+$ && "$w" -ge 900 ]]
'

section "batch slice"
check "projectplanner-batch.slice CPUQuota is capped" bash -c '
  q=$(systemctl show projectplanner-batch.slice -p CPUQuotaPerSecUSec --value 2>/dev/null || true)
  [[ -n "$q" && "$q" != "infinity" ]]
'
check "projectplanner-batch.slice IOWeight <= 10" bash -c '
  w=$(systemctl show projectplanner-batch.slice -p IOWeight --value)
  [[ "$w" =~ ^[0-9]+$ && "$w" -le 10 ]]
'

section "interactive services"
for unit in projectplanner.service projectplanner-mcp.service projectplanner-gateway.service projectplanner-agent-host.service; do
  check "${unit} Slice=projectplanner-interactive.slice" \
    prop_eq "$unit" Slice projectplanner-interactive.slice
done

section "batch services"
for unit in \
  projectplanner-narrate.service \
  projectplanner-monitors.service \
  projectplanner-inbox.service \
  projectplanner-reconcile.service \
  projectplanner-summarize.service \
  projectplanner-digest.service \
  projectplanner-ci-gate.service \
  projectplanner-backup.service; do
  check "${unit} Slice=projectplanner-batch.slice" \
    prop_eq "$unit" Slice projectplanner-batch.slice
done
check "projectplanner-ci-gate.service MemoryMax <= 320M" bash -c '
  max=$(systemctl show projectplanner-ci-gate.service -p MemoryMax --value)
  [[ "$max" =~ ^[0-9]+$ && "$max" -le 335544320 ]]
'

if (( failures > 0 )); then
  printf '\nverify_cgroup_slices: %d check(s) failed\n' "$failures"
  exit 1
fi

printf '\nverify_cgroup_slices: all checks passed\n'
