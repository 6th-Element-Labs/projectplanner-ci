#!/usr/bin/env bash
# PERF-3 acceptance checks for zram swap + cgroup memory isolation.
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
  [[ "$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)" == "$expected" ]]
}

prop_min_bytes() {
  local unit=$1 property=$2 min_bytes=$3
  local actual
  actual="$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)"
  [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= min_bytes ))
}

section() { printf '\n== %s ==\n' "$1"; }

section "swap topology (zram, not disk)"
check "zram swap device is active" bash -c 'swapon --show | grep -q zram'
check "no disk-backed swap is active" bash -c '! swapon --show=TYPE 2>/dev/null | tail -n +2 | grep -Eq "^(partition|file)$"'

section "interactive tier (MemorySwapMax=0 + reservations)"
for unit in projectplanner.service projectplanner-mcp.service projectplanner-gateway.service; do
  check "${unit} MemorySwapMax=0" prop_eq "$unit" MemorySwapMax 0
  check "${unit} MemoryMin >= 64M" prop_min_bytes "$unit" MemoryMin 67108864
  check "${unit} MemoryLow >= MemoryMin" bash -c '
    u=$1
    min=$(systemctl show "$u" -p MemoryMin --value)
    low=$(systemctl show "$u" -p MemoryLow --value)
    [[ "$min" =~ ^[0-9]+$ && "$low" =~ ^[0-9]+$ && "$low" -ge "$min" ]]
  ' _ "$unit"
done

section "batch tier (MemoryMax hard caps)"
for unit in \
  projectplanner-narrate.service \
  projectplanner-monitors.service \
  projectplanner-inbox.service \
  projectplanner-coordinator-audit.service \
  projectplanner-summarize.service \
  projectplanner-digest.service; do
  check "${unit} MemoryMax <= 256M" bash -c '
    u=$1
    max=$(systemctl show "$u" -p MemoryMax --value)
    [[ "$max" =~ ^[0-9]+$ && "$max" -gt 0 && "$max" -le 268435456 ]]
  ' _ "$unit"
done
check "projectplanner-reconcile.service MemoryMax <= 512M" bash -c '
  max=$(systemctl show projectplanner-reconcile.service -p MemoryMax --value)
  [[ "$max" =~ ^[0-9]+$ && "$max" -gt 0 && "$max" -le 536870912 ]]
'
check "projectplanner-claim-gate.service MemoryMax <= 128M" bash -c '
  max=$(systemctl show projectplanner-claim-gate.service -p MemoryMax --value)
  [[ "$max" =~ ^[0-9]+$ && "$max" -gt 0 && "$max" -le 134217728 ]]
'

section "memory pressure snapshot"
free -h || true
if [[ -r /proc/vmstat ]]; then
  grep -E '^pswpin ' /proc/vmstat || true
  echo "  note: interactive MemorySwapMax=0 should keep hot-tier swap-in at zero under burst."
fi

if (( failures > 0 )); then
  printf '\nverify_memory_isolation: %d check(s) failed\n' "$failures"
  exit 1
fi

printf '\nverify_memory_isolation: all checks passed\n'
