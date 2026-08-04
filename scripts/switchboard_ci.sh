#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
STRICT="${SWITCHBOARD_CI_STRICT:-0}"
REQUIRE_NODE="${SWITCHBOARD_CI_REQUIRE_NODE:-0}"
SCOPE="${SWITCHBOARD_CI_SCOPE:-full}"
BASE_SHA="${SWITCHBOARD_CI_BASE_SHA:-}"
FAIL_FAST="${SWITCHBOARD_CI_FAIL_FAST:-1}"
RESULT_REPORT="${CI_RESULT_REPORT:-.artifacts/ci-result.json}"

if [ "$SCOPE" != "fast" ] && [ "$SCOPE" != "full" ]; then
  echo "Unsupported SWITCHBOARD_CI_SCOPE=$SCOPE (expected fast or full)." >&2
  exit 2
fi

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
#
# Multiple managed workspaces can run this gate on one Agent Host at the same time. Their
# per-gate xargs pools therefore share a host-wide advisory slot pool, rather than each
# independently consuming the full CPU count. This keeps private Uvicorn/Chromium startup
# responsive under canonical parallel CI without weakening readiness assertions or skipping
# browser tests. Override the aggregate ceiling with SWITCHBOARD_CI_HOST_JOBS.
_cpu_count() {
  if command -v nproc >/dev/null 2>&1; then nproc
  elif command -v getconf >/dev/null 2>&1; then getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4
  elif command -v sysctl >/dev/null 2>&1; then sysctl -n hw.ncpu 2>/dev/null || echo 4
  else echo 4
  fi
}
JOBS="${SWITCHBOARD_CI_JOBS:-$(_cpu_count)}"
HOST_JOBS="${SWITCHBOARD_CI_HOST_JOBS:-$(_cpu_count)}"
BROWSER_WEIGHT="${SWITCHBOARD_CI_BROWSER_WEIGHT:-4}"

case "$HOST_JOBS" in
  ''|*[!0-9]*|0)
    echo "Unsupported SWITCHBOARD_CI_HOST_JOBS=$HOST_JOBS (expected a positive integer)." >&2
    exit 2
    ;;
esac
case "$BROWSER_WEIGHT" in
  ''|*[!0-9]*|0)
    echo "Unsupported SWITCHBOARD_CI_BROWSER_WEIGHT=$BROWSER_WEIGHT (expected a positive integer)." >&2
    exit 2
    ;;
esac
if [ "$BROWSER_WEIGHT" -gt "$HOST_JOBS" ]; then
  BROWSER_WEIGHT="$HOST_JOBS"
fi

section() {
  printf '\n== %s ==\n' "$1"
}

# Worker: run one test file in its own process, buffering output so parallel logs stay
# readable. A pass prints one PASS line; a failure is recorded as a .fail file (and echoed)
# so the parent can list every failure at the end. Always exits 0 so xargs keeps scheduling
# the remaining suite instead of aborting on the first red test.
_run_one_test() {
  local test_file="$1"
  local safe out rc weight=1
  safe="$(printf '%s' "$test_file" | tr '/.' '__')"
  case "$test_file" in
    tests/browser/*) weight="$BROWSER_WEIGHT" ;;
  esac
  if out="$("$PYTHON" scripts/ci_host_slot.py --slots "$HOST_JOBS" --weight "$weight" -- \
      "$PYTHON" "$test_file" 2>&1)"; then
    printf 'PASS  %s\n' "$test_file"
  else
    rc=$?
    { printf '== FAIL %s (exit %s) ==\n' "$test_file" "$rc"
      printf '%s\n' "$out"
    } > "${SWITCHBOARD_CI_RESULTS:?SWITCHBOARD_CI_RESULTS must be set}/$safe.fail"
    printf 'FAIL  %s (exit %s)\n' "$test_file" "$rc"
    if [ "${SWITCHBOARD_CI_FAIL_FAST:-1}" = "1" ]; then
      # GNU/BSD xargs both stop scheduling new work when a child exits 255.
      # Already-running workers finish, but a known-red gate no longer burns
      # minutes executing hundreds of additional files.
      return 255
    fi
  fi
  return 0
}

# Every executable Python test is discovered automatically. A test may be skipped only by
# adding its repo-relative path here with a reason that can survive code review.
TEST_DENYLIST=(
  ""  # Empty sentinel keeps macOS Bash 3 + `set -u` happy when nothing is denied.
  # Wall-clock ratchets are monitored by the scheduled performance workflow.
  # They are deliberately non-blocking for PR and merge-group verification.
  "test_concurrent_load_ratchet.py"
  "test_cross_process_load_ratchet.py"
  # "test_example.py"  # Example: requires a provider fixture unavailable in hermetic CI.
)

run_discovered_tests() {
  local results_dir list total failed xrc=0 denied

  results_dir="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-ci-results.XXXXXX")"
  list="$(mktemp "${TMPDIR:-/tmp}/switchboard-ci-tests.XXXXXX")"

  if [ "$SCOPE" = "fast" ] && [ -n "$BASE_SHA" ] && \
      git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    "$PYTHON" scripts/select_impacted_tests.py \
      --base "$BASE_SHA" --admission > "$list"
  else
    if [ "$SCOPE" = "fast" ]; then
      echo "fast gate could not verify its base SHA; failing safe to the full test list."
    fi
    # Discover every test file (repo-relative, stable order).
    find . \
      -path './.git' -prune -o \
      -path './.venv' -prune -o \
      -path './base-check' -prune -o \
      -path './.worktrees' -prune -o \
      -path './.claude' -prune -o \
      -path './.artifacts' -prune -o \
      -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print \
      | sed 's#^\./##' | LC_ALL=C sort > "$list"
  fi

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
    if [ "$SCOPE" = "fast" ]; then
      echo "No Python tests selected for this documentation-only admission."
      CI_FAILURE_SUMMARY="tests: no impacted Python tests"
      rm -rf "$results_dir" "$list"
      return 0
    fi
    echo "No Python tests discovered." >&2
    CI_FAILURE_SUMMARY="tests: discovery returned no files"
    rm -rf "$results_dir" "$list"
    return 1
  fi

  section "Python tests — ${total} files, ${JOBS}-way local / ${HOST_JOBS}-slot host (${SCOPE}; browser weight ${BROWSER_WEIGHT})"
  # One worker process per file, JOBS at a time. Workers self-report and always exit 0
  # (recording failures as files), so the whole suite runs even when some tests are red.
  SWITCHBOARD_CI_RESULTS="$results_dir" \
  SWITCHBOARD_CI_FAIL_FAST="$FAIL_FAST" \
    xargs -P "$JOBS" -I {} bash "$SELF" __run_one {} < "$list" || xrc=$?

  failed="$(find "$results_dir" -name '*.fail' | wc -l | tr -d ' ')"
  if [ "$failed" -ne 0 ] || [ "$xrc" -ne 0 ]; then
    section "FAILED: ${failed} of ${total} Python test file(s)"
    cat "$results_dir"/*.fail 2>/dev/null || true
    if [ "$xrc" -ne 0 ] && [ "$failed" -eq 0 ]; then
      printf 'tests: worker scheduler exited %s with no per-test failure recorded (crash/OOM?).\n' "$xrc" >&2
      CI_FAILURE_SUMMARY="tests: worker scheduler failed before recording a test"
    else
      printf 'tests: %d of %d Python test file(s) FAILED (see above).\n' "$failed" "$total" >&2
      CI_FAILURE_SUMMARY="tests: ${failed} of ${total} Python test files failed"
    fi
    rm -rf "$results_dir" "$list"
    return 1
  fi

  rm -rf "$results_dir" "$list"
  CI_FAILURE_SUMMARY="tests: ${total} Python test files passed"
  printf '\nAll %d Python test files passed (%s-way parallel).\n' "$total" "$JOBS"
}

# Parallel-worker fast path: `switchboard_ci.sh __run_one <test_file>` runs a single test and
# exits, without re-running the whole gate. Invoked by run_discovered_tests via xargs above.
if [ "${1:-}" = "__run_one" ]; then
  _run_one_test "${2:?usage: __run_one <test_file>}"
  exit $?
fi

CI_FAILURE_SUMMARY="gate failed before test completion"
write_ci_result() {
  local rc="$1" status="failure"
  [ "$rc" -eq 0 ] && status="success"
  CI_RESULT_STATUS="$status" \
  CI_RESULT_EXIT_CODE="$rc" \
  CI_RESULT_SCOPE="$SCOPE" \
  CI_RESULT_SUMMARY="$CI_FAILURE_SUMMARY" \
  CI_RESULT_REPORT_PATH="$RESULT_REPORT" \
    "$PYTHON" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CI_RESULT_REPORT_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
    "schema": "switchboard.ci_result.v1",
    "status": os.environ["CI_RESULT_STATUS"],
    "exit_code": int(os.environ["CI_RESULT_EXIT_CODE"]),
    "scope": os.environ["CI_RESULT_SCOPE"],
    "summary": os.environ["CI_RESULT_SUMMARY"],
}, sort_keys=True) + "\n", encoding="utf-8")
PY
}
on_exit() {
  local rc=$?
  trap - EXIT
  write_ci_result "$rc"
  exit "$rc"
}
trap on_exit EXIT

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

  if [ "$SCOPE" = "full" ]; then
    section "Required Chromium service-cut browser"
    "$PYTHON" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    browser.close()
print("Playwright Chromium launch: PASS")
PY
  fi
fi

section "Python compile"
"$PYTHON" -m compileall -q . -x '(^|/)(\.git|\.venv|__pycache__)(/|$)|(^|/)\._'

section "CI hermeticity gate (tests must not read live host state)"
# A flaky test blocks the whole merge-queue train, not just one PR. Fail before the suite runs
# if any test_*.py reaches for live /proc, host load, psutil, or real network (BUG-67 class).
"$PYTHON" scripts/ci_hermeticity_lint.py .

run_discovered_tests

if [ "$STRICT" = "1" ] && [ "$SCOPE" = "full" ]; then
  section "Mandatory Playwright evidence"
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

CI_FAILURE_SUMMARY="gate: ${SCOPE} verification passed"
section "Switchboard CI gate complete"
