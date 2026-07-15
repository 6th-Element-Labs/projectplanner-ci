# Playwright headless audit — 2026-07-15

- **Environment:** `https://plan.taikunai.com`
- **Harness:** Playwright Chromium headless + `Authorization: Bearer $PM_MCP_TOKEN`
- **Entry URL:** `/index.html?project=switchboard…` (`/` is login-gated; app shell is on `/index.html`)
- **Artifacts:** `/tmp/sb-playwright-validate/` (`audit-report.json`, `phaseB-poc.png`, `phaseB-autopilot.png`, console dumps)
- **Purpose:** Validate Session terminal + chat POC / Autopilot linked tasks in Mission UI; crawl tabs; capture console, network, and perf signals.

Related deliverables / tasks scoped the same day:

- `deliverable-session-terminal-chat-poc` — CO-11, CO-12, CO-13, UI-17, DOGFOOD-14, COORD-34, SESSION-13
- `deliverable-coordinator-mediated-dispatch-t0-t1` (M4.6) — COORD-34, SESSION-13 (+ CO-11/CO-12 foundation)

---

## Validation result (tasks in UI)

With `/api/projects` temporarily fulfilled in the browser (see BUG-A1), Mission UI showed:

| Deliverable | Task IDs visible in Mission pane |
|---|---|
| Session terminal + full chat POC | CO-11, CO-12, CO-13, UI-17, DOGFOOD-14, COORD-34, SESSION-13 |
| Deliverable Autopilot | COORD-34, SESSION-13, CO-11, CO-12 |

`GET /api/deliverables/deliverable-session-terminal-chat-poc/mission_status?project=switchboard` also returned all seven POC task ids (HTTP 200).

---

## Fix status (branch `cursor/playwright-audit-fixes`)

| ID | Status | Notes |
|---|---|---|
| BUG-A1 | **Fixed** | ACCESS-25 — Bearer parity for `GET /api/projects` + regression test. |
| BUG-A2 | **Fixed** | `view=cards` omits description; client uses `?view=cards` + `cache: no-cache`. |
| BUG-A3 | **Fixed** | Batched mission link enrichment; slim deliverable_tally. |
| BUG-A4 | **Fixed** | HARDEN-76 — `/health/saturation` timeout → 200 `degraded`. |
| BUG-A5 | **Fixed** | Skip `/api/auth/session` without `taikun_session` cookie. |
| BUG-A6 | **Fixed** | `#main-nav` in `ctrlFor`; nested hub open on deep link. |
| BUG-A7 | **Fixed** | Title derived from URL/`PM_PROJECT` even if picker fails. |
| BUG-A8 | **Fixed** | Mermaid render 12s timeout. |
| BUG-A9 | **Fixed** | People/tally/context deferred past board first paint. |
| BUG-A10 | **Fixed** | Inter via `html.fonts-ready` after `document.fonts.ready`. |
| BUG-A11 | **Fixed** | In-flight promise de-dupe for mission_status + dependency_graph. |
| BUG-A12 | **Harness only** | Don't set global Authorization on CDN in Playwright. |
| BUG-A13 | **Mitigated** | Relieved by A2/A9; scripts already content-hashed. |

---

## Issue register (for bug filing)

Severity: **P0** blocks correct operator UX under normal token/automation; **P1** major perf/reliability; **P2** polish / harness noise.

### BUG-A1 — `/api/projects` rejects Bearer; Mission picker stuck without cookie session

| | |
|---|---|
| **Severity** | P0 |
| **Symptom** | `GET /api/projects?project=switchboard` → **401** `{detail:"not authenticated"}` with MCP/env Bearer. Project switcher options = `[]`. UI falls back oddly (title still “Project Maxwell Plan…”); operator cannot switch boards with token-only auth. |
| **Contrast** | Same Bearer succeeds on `/api/board`, `/api/deliverables`, `/api/deliverables/…/mission_status`. |
| **Root cause (code)** | `src/switchboard/api/routers/projects.py` `list_projects` uses **session cookie only** (`current_user(request.cookies…)`), not `resolve_principal` / Bearer. Comment in `auth/routes.py` notes deny-by-default filtering and incomplete Bearer teaching. |
| **Impact** | Headless/agent UI, scoped-token browsers, and any non-cookie client cannot populate the project switcher. Deliverable deep-links with `?project=switchboard` still load picker data via `/api/deliverables`, but project chrome remains broken. |
| **Suggested fix** | Teach `GET /api/projects` to accept Bearer via the same principal resolver as other reads; return accessible projects for env-MCP / scoped tokens; keep cookie path for humans. Add regression test: Bearer → 200 with project list. |
| **Owner hint** | ACCESS / AUTH / ARCH-MS |

### BUG-A2 — Slow and large `GET /api/board?project=switchboard`

| | |
|---|---|
| **Severity** | P1 |
| **Symptom** | Resource timing ≈ **20s**, transfer ≈ **200KB** compressed/raw payload large; blocks Plan/Board paint. |
| **Evidence** | Playwright `perf.slow`: `api/board?project=switchboard` duration ~20335ms, transfer ~202709. |
| **Notes** | Existing `test_board_load_perf.py` (ETag/304). Likely heavy full-board serialization; 538 tasks on switchboard. |
| **Suggested fix** | Slim default view; stronger caching (ETag/If-None-Match already partial); defer non-critical fields; pagination or `view=` params; profile store board builder. |
| **Owner hint** | PERF / UI / store board path |

### BUG-A3 — Slow Autopilot `mission_status`

| | |
|---|---|
| **Severity** | P1 |
| **Symptom** | `GET …/deliverable-coordinator-mediated-dispatch-t0-t1/mission_status` ≈ **11–14s** (multiple calls during one Mission open). POC deliverable same endpoint ≈ **3s**. |
| **Evidence** | Slow list + duplicate fetches during nav (3× Autopilot mission_status in one session). |
| **Suggested fix** | Profile hot path (linked_tasks snapshots, economics, dependency prep); cache short-TTL; avoid N+1; clients: de-dupe concurrent fetches / abort stale. |
| **Owner hint** | DELIVERABLES / PERF |

### BUG-A4 — `GET /health/saturation` 504 / abort

| | |
|---|---|
| **Severity** | P1 |
| **Symptom** | Console: failed resource **504** on `health/saturation`; also `net::ERR_ABORTED`. Settings “Box pressure” UX depends on this. |
| **Evidence** | Audit failedRequests + console errors; client wait ~5–6s before fail. |
| **Suggested fix** | Bound work, fail soft (200 + degraded payload), timeout inside handler, never hang proxy to 504. Check PSI / concurrency_limiter / SQLite locks. |
| **Owner hint** | HARDEN / PERF / saturation_signals |

### BUG-A5 — `/api/auth/session` 401 under Bearer-only browse

| | |
|---|---|
| **Severity** | P2 (expected for anonymous cookie, noisy for token UI) |
| **Symptom** | Boot and chrome call `/api/auth/session` → 401 with Bearer; console noise; no user chip. |
| **Suggested fix** | Either accept Bearer and return synthetic principal for env-MCP, or suppress client call when already authorized via token injection, or treat 401 as quiet “token mode”. |
| **Owner hint** | AUTH / UI |

### BUG-A6 — Nested Plan/Inbox tab hrefs confuse deep navigation

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | Top nav is hubs: Plan → `#tab-plan-hub`, Inbox → `#tab-inbox-hub`. Board is `#tab-board` **inside** Plan hub. Clicks on `#tab-board` / wrong top hrefs timeout if hub not open first. |
| **Evidence** | Playwright nav timeouts for `#tab-board` without opening `#tab-plan-hub` first. |
| **Suggested fix** | Deep-link router already partial in `index.html`; ensure `?tab=` / hash opens parent hub; document for agents; optional alias redirects. |
| **Owner hint** | UI |

### BUG-A7 — Boot title / branding stuck on Maxwell when project list fails

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | With `?project=switchboard` but empty project switcher, document title remained **“Project Maxwell Plan \| Taikun Atlas”**. |
| **Suggested fix** | Derive title from URL/`PM_PROJECT` even when `/api/projects` fails; never leave Maxwell branding as default for switchboard deep links. |
| **Owner hint** | UI |

### BUG-A8 — Dependency map stuck on “Rendering…” while tasks already listed

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | POC Mission screenshot: linked tasks visible, map still “Rendering dependency map…”. |
| **Suggested fix** | Timeout/skeleton; ensure Mermaid lazy-load errors surface; don’t block other Mission sections. |
| **Owner hint** | UI / mission.js |

### BUG-A9 — Parallel boot fan-out latency (people / tally / narration / context)

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | Multiple ~5–7s parallel fetches on load (`api/people`, `tally/v1/project`, `api/narration/health`, `api/projects/…/context`, `health/saturation`). |
| **Suggested fix** | Prioritize critical path; defer Tally/narration until tab open; coalesce; server-side speed. |
| **Owner hint** | UI / PERF |

### BUG-A10 — Large font download on critical path (`InterVariable.woff2` ~8.7s)

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | Self-hosted Inter variable font ~8.7s duration in resource timing. |
| **Suggested fix** | `font-display: swap`; subset; preload only needed weight; CDN/cache headers check. |
| **Owner hint** | UI |

### BUG-A11 — Duplicate `mission_status` fetches for same deliverable

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | Same Autopilot `mission_status` URL fetched 3× in one session (~14s each). |
| **Suggested fix** | Share in-flight promise; abort on picker change; cache by deliverable+etag. |
| **Owner hint** | UI (mission.js / app.js) |

### BUG-A12 — (Harness) Tabler icon fonts CORS when global `Authorization` header set

| | |
|---|---|
| **Severity** | P3 / harness |
| **Symptom** | Console: jsDelivr Tabler fonts blocked — `Authorization` not allowed by CORS. |
| **Cause** | Playwright `extraHTTPHeaders: { Authorization }` applies to **all** requests including CDN. |
| **Suggested fix** | For tests: don’t set global Authorization; inject only for `plan.taikunai.com`. Product: self-host fonts (already partly vendor) to avoid CDN CORS entirely. |
| **Owner hint** | QA harness / UI |

### BUG-A13 — `app.js` / `mission.js` multi-second script fetch

| | |
|---|---|
| **Severity** | P2 |
| **Symptom** | `app.js?v=45` ~4.3s; `mission.js` ~3.5s on this run (network + size). |
| **Suggested fix** | Bundle split already partial; verify compression/cache; measure after CDN/path. |
| **Owner hint** | UI |

---

## Console summary (this run)

| Kind | Count |
|---|---|
| `pageerror` (uncaught JS) | **0** |
| `console.error` | **12** |
| Failed network | **10** |

Dominant failures: `api/projects` 401 (×3), `api/auth/session` 401 (×2), Tabler font CORS (×3), `health/saturation` 504/abort (×2).

---

## Perf hot list (resource timing, one Switchboard Mission session)

| Resource | Duration (approx) |
|---|---|
| `/api/board?project=switchboard` | ~20s |
| Autopilot `mission_status` | ~11–14s (×3) |
| `InterVariable.woff2` | ~8.7s |
| Boot fan-out (people/tally/narration/context/saturation) | ~5–7s each |
| Navigation `loadEvent` | ~13s |
| POC `mission_status` | ~3s |

---

## Recommended triage order for the fixing agent

1. **BUG-A1** `/api/projects` Bearer — unlocks honest UI automation and switcher.
2. **BUG-A4** saturation 504 — fail soft.
3. **BUG-A2** board payload/time — biggest paint win.
4. **BUG-A3** + **BUG-A11** mission_status cost / de-dupe.
5. Then P2s: A5, A7, A8, A9, A10, A6.

---

## What this session already validated (do not re-scope)

- POC + Autopilot deliverables exist; linked tasks are on the board and render when Mission can resolve Switchboard.
- Create dedicated VM / PTY / chat / panel work is scoped separately under `deliverable-session-terminal-chat-poc` (not this bug list).

---

## Harness notes for reproducers

```bash
# From a machine with PM_MCP_TOKEN and Playwright:
cd /tmp/sb-playwright-validate   # or copy audit.mjs into repo tests/
PM_MCP_TOKEN=… node audit.mjs
```

Phase A = no `/api/projects` mock (exposes A1).  
Phase B = fulfill `GET /api/projects` with Switchboard+Maxwell so Mission task list can be asserted.

---

*Filed from Cursor agent Playwright audit, 2026-07-15. Update this doc when bugs get BUG- ids after intake.*
