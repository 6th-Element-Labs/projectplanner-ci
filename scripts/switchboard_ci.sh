#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
STRICT="${SWITCHBOARD_CI_STRICT:-0}"
REQUIRE_NODE="${SWITCHBOARD_CI_REQUIRE_NODE:-0}"

# Managed CI can run inside a live Agent Host process.  Keep its routing, wake,
# account, and credential context out of repository tests while preserving the
# active interpreter and ordinary CI controls such as PATH and STRICT.
# shellcheck source=ci_runtime_env.sh
. "$ROOT/scripts/ci_runtime_env.sh"

# Absolute path to this script so parallel test workers can re-invoke it (see __run_one).
SELF="$ROOT/scripts/switchboard_ci.sh"

# Parallelism for the Python suite. Every test file is hermetic — it points the store at its
# own tempfile.mkdtemp DB (PM_*_DB_PATH) and binds no fixed port — so files run concurrently
# with no shared-state contention. Override with SWITCHBOARD_CI_JOBS; default = CPU count.
_cpu_count() {
  if command -v nproc >/dev/null 2>&1; then nproc
  elif command -v getconf >/dev/null 2>&1; then getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4
  elif command -v sysctl >/dev/null 2>&1; then sysctl -n hw.ncpu 2>/dev/null || echo 4
  else echo 4
  fi
}
JOBS="${SWITCHBOARD_CI_JOBS:-$(_cpu_count)}"

section() {
  printf '\n== %s ==\n' "$1"
}

# Worker: run one test file in its own process, buffering output so parallel logs stay
# readable. A pass prints one PASS line; a failure is recorded as a .fail file (and echoed)
# so the parent can list every failure at the end. Always exits 0 so xargs keeps scheduling
# the remaining suite instead of aborting on the first red test.
_run_one_test() {
  local test_file="$1"
  local safe out rc
  safe="$(printf '%s' "$test_file" | tr '/.' '__')"
  if out="$("$PYTHON" "$test_file" 2>&1)"; then
    printf 'PASS  %s\n' "$test_file"
  else
    rc=$?
    { printf '== FAIL %s (exit %s) ==\n' "$test_file" "$rc"
      printf '%s\n' "$out"
    } > "${SWITCHBOARD_CI_RESULTS:?SWITCHBOARD_CI_RESULTS must be set}/$safe.fail"
    printf 'FAIL  %s (exit %s)\n' "$test_file" "$rc"
  fi
  return 0
}

# Every executable Python test is discovered automatically. A test may be skipped only by
# adding its repo-relative path here with a reason that can survive code review.
TEST_DENYLIST=(
  ""  # Empty sentinel keeps macOS Bash 3 + `set -u` happy when nothing is denied.
  # "test_example.py"  # Example: requires a provider fixture unavailable in hermetic CI.
)

run_discovered_tests() {
  local results_dir list total failed xrc=0 denied

  results_dir="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-ci-results.XXXXXX")"
  list="$(mktemp "${TMPDIR:-/tmp}/switchboard-ci-tests.XXXXXX")"

  # Discover every test file (repo-relative, stable order).
  find . \
    -path './.git' -prune -o \
    -path './.venv' -prune -o \
    -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print \
    | sed 's#^\./##' | LC_ALL=C sort > "$list"

  # Drop denylisted tests (announced — a skip is never silent).
  for denied in "${TEST_DENYLIST[@]}"; do
    [ -z "$denied" ] && continue
    if grep -qxF "$denied" "$list"; then
      printf 'SKIP  %s (documented in TEST_DENYLIST)\n' "$denied"
      grep -vxF "$denied" "$list" > "$list.keep" && mv "$list.keep" "$list"
    fi
  done

  total="$(wc -l < "$list" | tr -d ' ')"
  if [ "$total" -eq 0 ]; then
    echo "No Python tests discovered." >&2
    rm -rf "$results_dir" "$list"
    return 1
  fi

  section "Python tests — ${total} files, ${JOBS}-way parallel"
  # One worker process per file, JOBS at a time. Workers self-report and always exit 0
  # (recording failures as files), so the whole suite runs even when some tests are red.
  SWITCHBOARD_CI_RESULTS="$results_dir" \
    xargs -P "$JOBS" -I {} bash "$SELF" __run_one {} < "$list" || xrc=$?

  failed="$(find "$results_dir" -name '*.fail' | wc -l | tr -d ' ')"
  if [ "$failed" -ne 0 ] || [ "$xrc" -ne 0 ]; then
    section "FAILED: ${failed} of ${total} Python test file(s)"
    cat "$results_dir"/*.fail 2>/dev/null || true
    if [ "$xrc" -ne 0 ] && [ "$failed" -eq 0 ]; then
      printf 'tests: worker scheduler exited %s with no per-test failure recorded (crash/OOM?).\n' "$xrc" >&2
    else
      printf 'tests: %d of %d Python test file(s) FAILED (see above).\n' "$failed" "$total" >&2
    fi
    rm -rf "$results_dir" "$list"
    return 1
  fi

  rm -rf "$results_dir" "$list"
  printf '\nAll %d Python test files passed (%s-way parallel).\n' "$total" "$JOBS"
}

# Parallel-worker fast path: `switchboard_ci.sh __run_one <test_file>` runs a single test and
# exits, without re-running the whole gate. Invoked by run_discovered_tests via xargs above.
if [ "${1:-}" = "__run_one" ]; then
  _run_one_test "${2:?usage: __run_one <test_file>}"
  exit $?
fi

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

required = ["fastapi", "httpx", "mcp", "openpyxl", "playwright", "uvicorn"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    print("Missing required CI dependency module(s): " + ", ".join(missing))
    sys.exit(1)
print("Required dependency modules importable: " + ", ".join(required))
PY

  section "Required Chromium service-cut browser"
  "$PYTHON" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    browser.close()
print("Playwright Chromium launch: PASS")
PY
fi

section "Python compile"
"$PYTHON" -m compileall -q . -x '(^|/)(\.git|\.venv|__pycache__)(/|$)|(^|/)\._'

section "Concurrent agent-path SLO gate"
CONCURRENT_LOAD_REPORT="${CONCURRENT_LOAD_REPORT:-${TMPDIR:-/tmp}/switchboard-concurrent-load-report.json}" \
  "$PYTHON" scripts/concurrent_load_gate.py

section "Cross-process SQLite contention SLO gate (ARCH-19)"
CROSS_PROCESS_LOAD_REPORT="${CROSS_PROCESS_LOAD_REPORT:-${TMPDIR:-/tmp}/switchboard-cross-process-load-report.json}" \
  "$PYTHON" scripts/cross_process_load_gate.py

section "CI hermeticity gate (tests must not read live host state)"
# A flaky test blocks the whole merge-queue train, not just one PR. Fail before the suite runs
# if any test_*.py reaches for live /proc, host load, psutil, or real network (BUG-67 class).
"$PYTHON" scripts/ci_hermeticity_lint.py .

run_discovered_tests

if [ "$STRICT" = "1" ]; then
  section "Dedicated Switchboard UI / Playwright gate"
  "$PYTHON" scripts/run_ui_playwright.py \
    --task-id "${SWITCHBOARD_TASK_ID:-CI-UI}" \
    --work-session-id "${SWITCHBOARD_WORK_SESSION_ID:-}" \
    --branch "${SWITCHBOARD_BRANCH:-}" \
    --head-sha "${SWITCHBOARD_HEAD_SHA:-${GITHUB_SHA:-}}" \
    --output "${UI_PLAYWRIGHT_REPORT:-.artifacts/ui-playwright-receipt.json}"
fi

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
