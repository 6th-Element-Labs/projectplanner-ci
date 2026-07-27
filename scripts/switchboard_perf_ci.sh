#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"

section() {
  printf '\n== %s ==\n' "$1"
}

section "Concurrent agent-path timing monitor"
CONCURRENT_LOAD_REPORT="${CONCURRENT_LOAD_REPORT:-.artifacts/concurrent-load-report.json}" \
  "$PYTHON" scripts/concurrent_load_gate.py

section "Cross-process SQLite timing monitor"
CROSS_PROCESS_LOAD_REPORT="${CROSS_PROCESS_LOAD_REPORT:-.artifacts/cross-process-load-report.json}" \
  "$PYTHON" scripts/cross_process_load_gate.py

section "Committed timing-ratchet contract"
"$PYTHON" test_concurrent_load_ratchet.py
"$PYTHON" test_cross_process_load_ratchet.py

section "Scheduled performance monitor complete"
