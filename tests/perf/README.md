# Load-performance suite (Playwright CLI only)

Measures cold/warm/throttled page-load performance, audits asset & icon-font
bloat, and enforces budgets as a CI gate. No external services — pure
Playwright (Chromium via the repo `.venv`) + PerformanceObserver + CDP.

## Run it

```bash
# full run against prod (audit + browser probe, medians of 3 runs)
.venv/bin/python tests/perf/run_perf_suite.py --base-url https://plan.taikunai.com

# authed board (not just the login shell): pass a Playwright storageState file
.venv/bin/python tests/perf/run_perf_suite.py \
    --base-url http://localhost:8110 --storage-state .auth/board.json

# fast, no browser (header/size/icon audit only) — good for quick CI
.venv/bin/python tests/perf/run_perf_suite.py --audit-only
```

Exit code is non-zero on any budget breach, so it drops straight into a merge
gate. Outputs `.artifacts/perf-audit-receipt.json` (machine) and
`.artifacts/perf-report.md` (ranked human report).

## Parts

| file | what it does |
|---|---|
| `run_perf_suite.py` | orchestrator + budget scoring + report + CI exit code |
| `load_probe.py` | Playwright cold/warm/throttled timing, medians, waterfall, runtime icon collection |
| `asset_audit.py` | HTTP audit: compression, cache headers, oversized/render-blocking assets |
| `icon_usage.py` | the used-icon truth set (see below) |
| `subset_icons.py` | generates the subsetted icon font + trimmed CSS |
| `budgets.json` | thresholds; **tighten as fixes land, never loosen silently** |

## Icon subsetting is safe by construction

The shipped Tabler font defines ~5,147 glyphs; the app uses ~185. Naively
subsetting to statically-matched `ti-` classes would drop icons built
dynamically (`ti-${icon}`, `icons[status]`, provider brand icons) and render
blank squares.

`icon_usage.py` instead builds a **superset**: every `ti-*` literal *plus*
every quoted string anywhere in the JS/HTML that is a real icon name
(validated against the class→codepoint map parsed from the shipped CSS).
Over-including a few glyphs costs bytes; under-including breaks the UI, so it
biases to include. `load_probe.py` additionally records every `.ti-*` class
rendered in the live DOM, and the suite fails if any observed icon is absent
from the subset — a runtime backstop before the swap ships.

**Regenerate the subset whenever icons change** (`subset_icons.py`); a stale
subset is the only way this breaks.
