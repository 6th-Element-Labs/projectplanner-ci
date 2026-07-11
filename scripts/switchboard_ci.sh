#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
STRICT="${SWITCHBOARD_CI_STRICT:-0}"
REQUIRE_NODE="${SWITCHBOARD_CI_REQUIRE_NODE:-0}"

section() {
  printf '\n== %s ==\n' "$1"
}

run_test() {
  section "$1"
  "$PYTHON" "$1"
}

# Every executable Python test is discovered automatically. A test may be skipped only by
# adding its repo-relative path here with a reason that can survive code review.
TEST_DENYLIST=(
  ""  # Empty sentinel keeps macOS Bash 3 + `set -u` happy when nothing is denied.
  # "test_example.py"  # Example: requires a provider fixture unavailable in hermetic CI.
)

is_denied_test() {
  local candidate="$1"
  local denied
  for denied in "${TEST_DENYLIST[@]}"; do
    [ -z "$denied" ] && continue
    if [ "$candidate" = "$denied" ]; then
      return 0
    fi
  done
  return 1
}

run_discovered_tests() {
  local discovered=0
  local test_file

  while IFS= read -r test_file; do
    test_file="${test_file#./}"
    if is_denied_test "$test_file"; then
      printf 'SKIP  %s (documented in TEST_DENYLIST)\n' "$test_file"
      continue
    fi
    run_test "$test_file"
    discovered=$((discovered + 1))
  done < <(
    find . \
      -path './.git' -prune -o \
      -path './.venv' -prune -o \
      -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print \
      | LC_ALL=C sort
  )

  if [ "$discovered" -eq 0 ]; then
    echo "No Python tests discovered." >&2
    return 1
  fi
  printf '\nDiscovered and ran %d Python test files.\n' "$discovered"
}

section "Python runtime"
"$PYTHON" --version

if [ "$STRICT" = "1" ]; then
  section "Python version gate"
  "$PYTHON" - <<'PY'
import sys

if sys.version_info < (3, 10):
    print("Switchboard strict CI requires Python 3.10+ because runtime dependencies include mcp>=1.9.")
    sys.exit(1)
print("Python version is strict-CI compatible.")
PY

  section "Required Python dependencies"
  "$PYTHON" - <<'PY'
import importlib.util
import sys

required = ["fastapi", "httpx", "mcp", "openpyxl", "uvicorn"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    print("Missing required CI dependency module(s): " + ", ".join(missing))
    sys.exit(1)
print("Required dependency modules importable: " + ", ".join(required))
PY
fi

section "Python compile"
"$PYTHON" -m compileall -q . -x '(^|/)(\.git|\.venv|__pycache__)(/|$)|(^|/)\._'

section "Concurrent agent-path SLO gate"
CONCURRENT_LOAD_REPORT="${CONCURRENT_LOAD_REPORT:-${TMPDIR:-/tmp}/switchboard-concurrent-load-report.json}" \
  "$PYTHON" scripts/concurrent_load_gate.py

run_discovered_tests

section "Frontend JavaScript syntax"
if command -v node >/dev/null 2>&1; then
  node --check static/app.js
  for module in static/js/*.js; do
    node --check "$module"
  done
  node --check static/taikun-ui.js
  node --check static/taikun-theme.js
else
  if [ "$REQUIRE_NODE" = "1" ]; then
    echo "Node.js is required for this gate but was not found." >&2
    exit 1
  fi
  echo "SKIP  Node.js not found; JavaScript syntax check is optional outside strict CI."
fi

section "Switchboard CI gate complete"
