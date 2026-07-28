# Autopilot dock — rename, runner condition ladder, honest liveness

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the Fleet surface to Autopilot and rebuild its runner card around a condition ladder that reports whether a runner is actually making progress, instead of reporting that its lease is alive.

**Architecture:** A new pure function `runnerConditions` lives beside the existing `prConditions` in `static/js/fleet-dock.js` and returns every condition that holds, worst first. `_dockRunnerHtml` renders `[0]` as the authoritative chip, tints the card's left edge with its tone, and picks the primary action from its key. The "waiting on you" condition comes from joining the operator attention queue to runners on `runner_session_id`. No backend change: every field already ships.

**Tech Stack:** Vanilla ES5-flavoured JS (no build step), Tabler 1.4.0 classes, Python 3.12 script-style tests executed as `python <file>`, `node -e` for behavioural JS tests.

## Scope: this is a card reskin, and the task list says so

Operator challenge 2026-07-28: *"we're just reskinning the toast card, you sure
this isn't overscoped?"* It was. Cut to **two** board tasks.

| Board task | Plan tasks | Delivers |
|---|---|---|
| UI-68 — Autopilot rename and the honest runner card | 1, 2, 4, 5 | the reskin: the card tells the truth |
| UI-71 — Activate waiting-on-you | 3 | lights up ladder rank 3 and the Answer action |

Each is one branch, one PR, one merge, and each ends with a working dock. Commit
per plan task as the steps say; the PR is the shipping unit. Plan task 9's
verification steps are **not** a separate board task — run them inside whichever
task you are finishing, including the mutation check.

### Cut, and why

| Cut | Plan task | Reason |
|---|---|---|
| `loadCoverage` widened to runner task ids | 8 (second half) | pure addition, never approved; the pill already works on PR rows |
| Bucket grouping and collapse-healthy | 6 | restructures the list, not the card; separable and not asked for |
| Tone dots on the tab strip | 7 (second half) | touches the PR and deploy tabs, which are out of scope |
| Pill reason rewrite | 7 (first half) | list-level, not card-level; the "All clear" rename in task 1 stays |
| Separate verification board task | 9 | process overhead; its steps belong inside the two real tasks |

Plan task 8's **label change only** (Driving / Armed / Paused / Stale / Arm) stays
in UI-68: it is not an enhancement, it is required by the rename — on a surface
called Autopilot a pill reading "Autopilot armed" repeats the noun.

### Why UI-71 is separable

Six of the seven ladder ranks come from the runner payload alone. Only
`waiting_on_you` needs the attention join. UI-68 therefore writes and tests the
complete seven-rank ladder and the Answer action, with `_dockAttention` always
`{}` — rank 3 simply never fires and Answer never renders. A stuck runner still
reads "Silent 14m" instead of a green "running", which is the core fix. UI-71 is
then a small data-plumbing task that activates code already shipped and tested.

### ADR compliance

- **ADR-0018 (Connect/Communicate boundary).** Runner sessions are Connect-plane;
  attention requests are Communicate-plane. The join MUST stay in the browser,
  which ADR-0018 permits explicitly ("an edge transport may expose commands from
  both contexts"). Do NOT move it server-side into `/ixp/v1/runner_sessions`, and
  do NOT have the host stamp an `awaiting_input` flag on the runner record —
  either would make Connect depend on Communicate to supervise work, which the
  ADR forbids. Sharing a transport is not permission to share domain logic.
- **ADR-0006 / ADR-0021 subtraction accounting.** Mechanisms deleted: 3 (the
  `attn` prose-join array, the `tone` ternary, the `dead` boolean in
  `_dockRunnerHtml`). Mechanisms added: 0 — `runnerConditions` is presentation
  logic that replaces them, and the attention read is a read, not an authority.
  Net authority change: 0.
- **ADR-0020 (merge gates observe, not enforce).** Nothing here adds a merge
  gate; the dock reports condition and never blocks a merge.

## Global Constraints

- **Tests must run as `python <file>`.** CI executes every discovered test file with `"$PYTHON" "$test_file"` (`scripts/switchboard_ci.sh:44`). A pytest-fixture test file asserts nothing and still prints PASS. Every test file in this plan ends with a `__main__` block that calls each test function. No `monkeypatch`, no fixtures.
- **`node --check` gates every JS file** in `static/js/*.js` and `static/app.js` (`scripts/switchboard_ci.sh:190`). Syntax errors fail CI.
- **Unknown is not zero.** A runner whose host does not report output age must render as `Running` with uptime — never `Silent`, never `0s`.
- **Internal identifiers do not change.** `fleet-dock` element id, `toptab-fleet`, `#tab-fleet`, `_renderFleetDock`, `_loadFleetDock`, `SwitchboardFleetDock`, `static/js/fleet-dock.js`, and all `/ixp/v1` routes keep their names. Only user-facing strings change.
- **Tabler classes only** for tone: `bg-red-lt`, `bg-orange-lt`, `bg-yellow-lt`, `bg-green-lt`, `bg-secondary-lt`, and `var(--tblr-{tone})` for the left edge. No bespoke colour values.
- **Escape all interpolated data** with `app.esc(...)` in any HTML string.

---

### Task 1: Rename Fleet to Autopilot

**Files:**
- Modify: `static/index.html:252`, `static/index.html:599`
- Modify: `static/app.js:1201`, `static/app.js:1256`
- Test: `tests/test_autopilot_dock_rename.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Independently shippable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_rename.py`:

```python
"""Autopilot dock: the Fleet surface is called Autopilot. Internal ids are unchanged."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
APP = ROOT / "static" / "app.js"


def test_left_nav_and_page_title_say_autopilot():
    html = INDEX.read_text(encoding="utf-8")
    assert '<span class="nav-link-title">Autopilot</span>' in html
    assert '<span class="nav-link-title">Fleet</span>' not in html
    assert 'me-2"></i>Autopilot</h2>' in html


def test_dock_header_and_pill_say_autopilot():
    js = APP.read_text(encoding="utf-8")
    assert '<span class="fw-medium">Autopilot</span>' in js
    assert '<span class="fw-medium">Fleet</span>' not in js
    assert "'All clear'" in js
    assert "'Fleet clear'" not in js


def test_internal_identifiers_are_untouched():
    html = INDEX.read_text(encoding="utf-8")
    js = APP.read_text(encoding="utf-8")
    for ident in ('id="toptab-fleet"', 'href="#tab-fleet"', 'id="fleet-dock"'):
        assert ident in html, ident
    for ident in ("_renderFleetDock", "_loadFleetDock", "SwitchboardFleetDock"):
        assert ident in js, ident


if __name__ == "__main__":
    test_left_nav_and_page_title_say_autopilot()
    test_dock_header_and_pill_say_autopilot()
    test_internal_identifiers_are_untouched()
    print("PASS test_autopilot_dock_rename")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_rename.py`
Expected: `AssertionError` on the first assert — the nav title still says Fleet.

- [ ] **Step 3: Apply the four string changes**

In `static/index.html:252`, change only the title span:

```html
<li class="nav-item"><a class="nav-link" id="toptab-fleet" data-bs-toggle="tab" href="#tab-fleet" role="tab"><span class="nav-link-icon"><i class="ti ti-server-bolt"></i></span><span class="nav-link-title">Autopilot</span></a></li>
```

In `static/index.html:599`:

```html
<h2 class="page-title m-0"><i class="ti ti-server-bolt me-2"></i>Autopilot</h2>
```

In `static/app.js:1201`, replace `'Fleet clear'` with `'All clear'`:

```js
<span class="fw-medium">${nAttn ? this.esc(String(nAttn)) + ' blocked' : 'All clear'}</span>
```

In `static/app.js:1256`:

```js
<span class="fw-medium">Autopilot</span>
```

- [ ] **Step 4: Run the test and the syntax gate**

Run: `python tests/test_autopilot_dock_rename.py && node --check static/app.js`
Expected: `PASS test_autopilot_dock_rename`, no output from `node --check`.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.js tests/test_autopilot_dock_rename.py
git commit -m "feat(<TASK-ID>): rename the Fleet surface to Autopilot"
```

---

### Task 2: `runnerConditions` — the ladder

**Files:**
- Modify: `static/js/fleet-dock.js` (add three methods to the `FleetDock` object, after `prConditions`)
- Test: `tests/test_autopilot_dock_runner_condition_ladder.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FleetDock.shortAge(seconds) -> string` — `''` when input is null/negative/non-finite, else `'4s' | '14m' | '2h' | '3d'`.
  - `FleetDock.runnerOutputAge(session, nowSeconds) -> number | null` — seconds since last output, or `null` when unknown.
  - `FleetDock.runnerConditions(app, session, attentionBySession, nowSeconds) -> Array<{key, label, tone, icon, title?}>` — worst first, never empty. `attentionBySession` is an object keyed by `runner_session_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_runner_condition_ladder.py`:

```python
"""Autopilot dock: the runner condition ladder — ranks, and the honest-unknown rule.

Behavioural JS test. The module is an IIFE that assigns to `window`, so we
stub `globalThis.window` before requiring it and read the result as JSON.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "fleet-dock.js"
NOW = 1_000_000.0


def conditions(session, attention=None, now=NOW):
    script = (
        "globalThis.window = {};\n"
        f"require({json.dumps(str(MODULE))});\n"
        "const app = { _fleetAge: () => '6m', esc: (s) => String(s == null ? '' : s) };\n"
        f"const out = window.SwitchboardFleetDock.runnerConditions("
        f"app, {json.dumps(session)}, {json.dumps(attention or {})}, {now});\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"node failed: {res.stderr.strip()}")
    return json.loads(res.stdout)


def _session(**over):
    base = {
        "runner_session_id": "rs-1", "task_id": "ADAPTER-32", "status": "running",
        "stale": False, "environment": {}, "last_snapshot": {},
    }
    base.update(over)
    return base


def test_exited_outranks_everything_below():
    got = conditions(_session(status="exited"))
    assert got[0]["key"] == "exited", got
    assert got[0]["tone"] == "red"


def test_lost_host_wins_over_a_pending_question():
    session = _session(stale=True)
    attention = {"rs-1": {"request_id": "r1", "version": 3}}
    got = conditions(session, attention)
    assert got[0]["key"] == "lost_host", got


def test_waiting_on_you_outranks_silent():
    session = _session(environment={"progress_fault": {"output_age_s": 840}})
    attention = {"rs-1": {"request_id": "r1", "version": 3}}
    got = conditions(session, attention)
    assert got[0]["key"] == "waiting_on_you", got
    assert [c["key"] for c in got].count("silent") == 1


def test_silent_reports_the_fault_output_age():
    got = conditions(_session(environment={"progress_fault": {"output_age_s": 840}}))
    assert got[0]["key"] == "silent", got
    assert got[0]["label"] == "Silent 14m", got[0]


def test_idle_beats_working_when_no_task_is_bound():
    session = _session(task_id="", environment={"last_output_at": NOW - 4})
    got = conditions(session)
    assert got[0]["key"] == "idle", got


def test_working_uses_output_age_not_uptime():
    session = _session(environment={"last_output_at": NOW - 4, "uptime_seconds": 2820})
    got = conditions(session)
    assert got[0]["key"] == "working", got
    assert got[0]["label"] == "Working 4s", got[0]


def test_unknown_output_age_is_never_silent_and_never_zero():
    got = conditions(_session(environment={"uptime_seconds": 2820}))
    assert got[0]["key"] == "running_unknown", got
    assert "Silent" not in got[0]["label"], got[0]
    assert "0s" not in got[0]["label"], got[0]


def test_dirty_workspace_is_secondary_never_primary():
    session = _session(
        environment={"last_output_at": NOW - 4},
        last_snapshot={"status_porcelain": " M a.py\n M b.py\n?? c.py"},
    )
    got = conditions(session)
    assert got[0]["key"] == "working", got
    dirty = [c for c in got if c["key"] == "dirty"]
    assert dirty and dirty[0]["label"] == "3 uncommitted", got


def test_conditions_are_never_empty():
    assert conditions(_session(status="unknown-state"))


if __name__ == "__main__":
    test_exited_outranks_everything_below()
    test_lost_host_wins_over_a_pending_question()
    test_waiting_on_you_outranks_silent()
    test_silent_reports_the_fault_output_age()
    test_idle_beats_working_when_no_task_is_bound()
    test_working_uses_output_age_not_uptime()
    test_unknown_output_age_is_never_silent_and_never_zero()
    test_dirty_workspace_is_secondary_never_primary()
    test_conditions_are_never_empty()
    print("PASS test_autopilot_dock_runner_condition_ladder")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_runner_condition_ladder.py`
Expected: `AssertionError: node failed: TypeError: window.SwitchboardFleetDock.runnerConditions is not a function`

- [ ] **Step 3: Implement the three methods**

In `static/js/fleet-dock.js`, insert immediately after the `prConditions` method's closing `},`:

```js
        // Duration in seconds -> the dock's compact age string. Empty for an
        // unknown duration: callers must not print a unit they cannot prove.
        shortAge(sec) {
            const n = Number(sec);
            if (sec === null || sec === undefined || !isFinite(n) || n < 0) return '';
            if (n < 60) return `${Math.round(n)}s`;
            if (n < 3600) return `${Math.round(n / 60)}m`;
            if (n < 86400) return `${Math.round(n / 3600)}h`;
            return `${Math.round(n / 86400)}d`;
        },
        // Seconds since this runner last produced output, or null when the host
        // does not report it. WATCH-19's fault already computes the age, so it
        // wins; last_output_at is the fallback. null is a real answer — never
        // coerce it to 0, or a silent host reads as a working one.
        runnerOutputAge(s, nowSeconds) {
            const env = (s && s.environment) || {};
            const fault = env.progress_fault;
            if (fault && typeof fault === 'object' && fault.output_age_s !== null
                    && fault.output_age_s !== undefined) {
                const n = Number(fault.output_age_s);
                if (isFinite(n) && n >= 0) return n;
            }
            if (env.last_output_at !== null && env.last_output_at !== undefined) {
                const at = Number(env.last_output_at);
                if (isFinite(at) && at > 0) return Math.max(0, Number(nowSeconds) - at);
            }
            return null;
        },
        // Every condition that holds, worst first — the same contract as
        // prConditions. The card shows [0] as the authoritative chip and tints
        // its left edge with that tone; `dirty` is only ever a secondary chip.
        runnerConditions(app, s, attentionBySession, nowSeconds) {
            const out = [];
            const env = (s && s.environment) || {};
            const snap = (s && s.last_snapshot) || {};
            const ask = (attentionBySession || {})[s && s.runner_session_id];
            const fault = (env.progress_fault && typeof env.progress_fault === 'object')
                ? env.progress_fault : null;
            const age = FleetDock.runnerOutputAge(s, nowSeconds);
            const stale = !!(s && s.stale);
            const running = (s && s.status) === 'running';

            if (!stale && !running) {
                out.push({ key: 'exited', label: 'Exited', tone: 'red',
                           icon: 'alert-triangle', title: env.failure_reason || '' });
            }
            if (stale) {
                const seen = app._fleetAge((s && s.updated_at) || snap.captured_at);
                out.push({ key: 'lost_host', label: `Lost host ${seen}`.trim(),
                           tone: 'red', icon: 'plug-connected-x' });
            } else if (ask) {
                out.push({ key: 'waiting_on_you', label: 'Waiting on you',
                           tone: 'orange', icon: 'user-exclamation' });
            }
            if (fault) {
                const quiet = FleetDock.shortAge(age);
                out.push({ key: 'silent', label: quiet ? `Silent ${quiet}` : 'Silent',
                           tone: 'yellow', icon: 'zzz' });
            }
            if (running && !(s && s.task_id)) {
                out.push({ key: 'idle', label: 'Idle', tone: 'secondary', icon: 'minus' });
            } else if (running && age !== null) {
                out.push({ key: 'working', label: `Working ${FleetDock.shortAge(age)}`,
                           tone: 'green', icon: 'player-play' });
            } else if (running) {
                // Honest unknown: the host never stamped an output age, so the
                // card reports uptime and says nothing about progress.
                out.push({ key: 'running_unknown', label: 'Running',
                           tone: 'secondary', icon: 'help-circle' });
            }
            const dirty = String(snap.status_porcelain || '').split('\n').filter(Boolean).length;
            if (dirty) {
                out.push({ key: 'dirty', label: `${dirty} uncommitted`,
                           tone: 'secondary', icon: 'file-diff' });
            }
            if (!out.length) {
                out.push({ key: 'unknown', label: 'Unknown', tone: 'secondary', icon: 'help-circle' });
            }
            return out;
        },
```

- [ ] **Step 4: Run the test and the syntax gate**

Run: `python tests/test_autopilot_dock_runner_condition_ladder.py && node --check static/js/fleet-dock.js`
Expected: `PASS test_autopilot_dock_runner_condition_ladder`, no output from `node --check`.

- [ ] **Step 5: Commit**

```bash
git add static/js/fleet-dock.js tests/test_autopilot_dock_runner_condition_ladder.py
git commit -m "feat(<TASK-ID>): runner condition ladder with honest output age"
```

---

### Task 3: Join the attention queue to runners

**Files:**
- Modify: `static/js/fleet-dock.js` (add `loadRunnerAttention`)
- Modify: `static/app.js` `_loadFleetDock` (~line 820) and `_fleetSignature` (~line 778)
- Test: `tests/test_autopilot_dock_runner_attention_join.py`

**Interfaces:**
- Consumes: `FleetDock.runnerConditions` from Task 2.
- Produces:
  - `FleetDock.loadRunnerAttention(app) -> Promise<Object>` — resolves to a map of `runner_session_id -> {request_id, version, prompt, choices, recommended_default}`. Resolves to `{}` on any failure; the dock must render without it.
  - `app._dockAttention` — the same map, stored on the app object for the render pass, exactly as `app._dockAutopilot` already works.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_runner_attention_join.py`:

```python
"""Autopilot dock: pending attention requests join runners on runner_session_id."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "fleet-dock.js"
APP = ROOT / "static" / "app.js"


def test_module_reads_the_operator_queue_and_keys_by_session():
    js = MODULE.read_text(encoding="utf-8")
    assert "loadRunnerAttention" in js
    assert "api/attention/requests" in js
    assert "runner_session_id" in js
    assert "recommended_default" in js


def test_join_is_advisory_and_never_blocks_the_render():
    js = MODULE.read_text(encoding="utf-8")
    start = js.index("loadRunnerAttention")
    body = js[start:start + 1400]
    assert "catch" in body, "a failed attention read must not blank the dock"
    assert "return {}" in body


def test_dock_loop_stores_and_signs_the_attention_map():
    js = APP.read_text(encoding="utf-8")
    assert "_dockAttention" in js
    assert "loadRunnerAttention" in js
    signature = js[js.index("_fleetSignature("):js.index("async _loadFleetDock")]
    assert "attention" in signature, "an answered question must re-render the dock"


if __name__ == "__main__":
    test_module_reads_the_operator_queue_and_keys_by_session()
    test_join_is_advisory_and_never_blocks_the_render()
    test_dock_loop_stores_and_signs_the_attention_map()
    print("PASS test_autopilot_dock_runner_attention_join")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_runner_attention_join.py`
Expected: `AssertionError` — `loadRunnerAttention` is not in the module.

- [ ] **Step 3: Add the loader to `static/js/fleet-dock.js`**

Insert after `loadCoverage`:

```js
        // Autopilot dock: the operator attention queue, indexed by the runner session it
        // is bound to. An attention request carries runner_session_id as a
        // first-class asserted field, so "this runner is asking you something"
        // is a key match, not an inference. Advisory by contract: a failed read
        // renders the dock without Waiting-on-you, never blank.
        async loadRunnerAttention(app) {
            try {
                const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
                const res = await app._fetchTimeout(
                    `api/attention/requests?${p}&limit=200`, { cache: 'no-store' });
                if (!res.ok) return {};
                const items = ((await res.json()) || {}).items || [];
                const bound = {};
                items.forEach((it) => {
                    const sid = String((it && it.runner_session_id) || '');
                    const status = String((it && it.status) || 'pending');
                    if (!sid || status === 'resolved' || status === 'expired') return;
                    bound[sid] = {
                        request_id: it.request_id, version: it.version,
                        prompt: it.prompt || '',
                        choices: it.choices || null,
                        recommended_default: it.recommended_default,
                    };
                });
                return bound;
            } catch (e) { return {}; }
        },
```

- [ ] **Step 4: Fetch it in the dock loop**

In `static/app.js` `_loadFleetDock`, add the call alongside the existing three (keep it in the same `Promise.all`, so one slow read cannot serialise the others):

```js
            const [runnerList, pRes, dRes, attention] = await Promise.all([
                this._fetchFleetRunners(force),
                fetch(`/ixp/v1/open_prs?${p}`, { cache: 'no-store' }),
                fetch(`/ixp/v1/deployments?${p}`, { cache: 'no-store' }),
                window.SwitchboardFleetDock.loadRunnerAttention(this),
            ]);
            this._dockAttention = attention || {};
```

- [ ] **Step 5: Include it in the signature**

In `_fleetSignature`, add an `attention` parameter and fold it in so answering a question re-renders. Change the signature line and the returned array:

```js
    _fleetSignature(runners, prs, deployments, prUnavailable, autopilotCoverage, attention) {
```

and inside the final `return JSON.stringify([...])`, append one element:

```js
            Object.keys(attention || {}).sort().map(
                (k) => [k, (attention[k] || {}).request_id, (attention[k] || {}).version]),
```

Then update the call site in `_loadFleetDock` to pass `this._dockAttention` as the sixth argument.

- [ ] **Step 6: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_runner_attention_join.py && node --check static/js/fleet-dock.js && node --check static/app.js`
Expected: `PASS test_autopilot_dock_runner_attention_join`, no output from either check.

- [ ] **Step 7: Commit**

```bash
git add static/js/fleet-dock.js static/app.js tests/test_autopilot_dock_runner_attention_join.py
git commit -m "feat(<TASK-ID>): join the attention queue to runners by session id"
```

---

### Task 4: Rebuild the runner card

**Files:**
- Modify: `static/app.js` `_dockRunnerHtml` (replace the whole method, currently lines 1062-1092)
- Test: `tests/test_autopilot_dock_runner_card_render.py`

**Interfaces:**
- Consumes: `FleetDock.runnerConditions`, `FleetDock.shortAge`, `FleetDock.runnerOutputAge`, `app._dockAttention`.
- Produces: `_dockRunnerHtml(s) -> string` — unchanged signature, new markup.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_runner_card_render.py`:

```python
"""Autopilot dock: the runner card leads with the task, not the session id."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "app.js"


def _card_source():
    js = APP.read_text(encoding="utf-8")
    start = js.index("_dockRunnerHtml(s) {")
    return js[start:js.index("_prConditions(x)", start)]


def test_card_uses_the_ladder_and_tints_its_edge():
    card = _card_source()
    assert "runnerConditions" in card
    assert "border-left:2px solid" in card


def test_session_id_leaves_the_card_face_for_the_tooltip():
    card = _card_source()
    assert "title=" in card and "runner_session_id" in card
    assert 'class="font-monospace" style="font-size:12px;">${this.esc(s.runner_session_id)}' not in card


def test_card_shows_log_tail_and_output_age():
    card = _card_source()
    assert "log_tail" in card
    assert "quiet" in card


def test_kill_is_not_a_bare_top_level_button():
    card = _card_source()
    assert "btn-outline-danger" not in card, "kill belongs in the overflow menu"


def test_uptime_is_in_the_provenance_line_not_the_status_slot():
    card = _card_source()
    assert "uptime_seconds" in card
    head = card[:card.index("dk-task") if "dk-task" in card else 600]
    assert "uptime" not in head, "uptime must not sit in the top-right status slot"


if __name__ == "__main__":
    test_card_uses_the_ladder_and_tints_its_edge()
    test_session_id_leaves_the_card_face_for_the_tooltip()
    test_card_shows_log_tail_and_output_age()
    test_kill_is_not_a_bare_top_level_button()
    test_uptime_is_in_the_provenance_line_not_the_status_slot()
    print("PASS test_autopilot_dock_runner_card_render")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_runner_card_render.py`
Expected: `AssertionError` — the card does not call `runnerConditions`.

- [ ] **Step 3: Replace `_dockRunnerHtml`**

Replace the entire method in `static/app.js` with:

```js
    _dockRunnerHtml(s) {
        const env = s.environment || {};
        const snap = s.last_snapshot || {};
        const now = Date.now() / 1000;
        const FD = window.SwitchboardFleetDock;
        const conditions = FD.runnerConditions(this, s, this._dockAttention || {}, now);
        const primary = conditions[0];
        const secondary = conditions.find((c) => c.key !== primary.key && c.key === 'dirty');
        const accent = `var(--tblr-${primary.tone}, var(--tblr-secondary, #626976))`;
        const age = FD.runnerOutputAge(s, now);
        const uptime = env.uptime_seconds == null
            ? '' : ` · up ${FD.shortAge(env.uptime_seconds)}`;
        const branch = snap.branch ? ` · ${this.esc(snap.branch)}` : '';
        const tail = String(env.log_tail || '').split('\n').filter(Boolean).pop() || '';
        const quiet = FD.shortAge(age);
        const cpu = (env.progress_fault || {}).cpu_percent;
        const meta = [s.runtime || '?', s.host_id || '?'].map((v) => this.esc(v)).join(' · ');
        const autopilot = FD.autopilotHtml(this, { tasks: s.task_id ? [{ task_id: s.task_id }] : [] });
        return `<div class="p-2 border rounded mb-2" style="border-left:2px solid ${accent} !important;border-top-left-radius:0;border-bottom-left-radius:0;">
            <div class="d-flex align-items-center gap-2">
                ${this._dockBadge(primary.label, primary.tone, primary.icon, primary.title || '')}
                ${secondary ? `<span class="badge bg-transparent border text-secondary" style="flex:none;">+ ${this.esc(secondary.label)}</span>` : ''}
                ${autopilot ? `<span class="ms-auto"></span>${autopilot}` : ''}
            </div>
            <div class="mt-1 text-truncate" style="font-size:13px;" title="${this.esc(s.runner_session_id)}">
                <span class="font-monospace text-secondary" style="font-size:12px;">${this.esc(s.task_id || 'no task')}</span>
                ${this.esc(s.task_title || '')}
            </div>
            <div class="text-secondary font-monospace text-truncate" style="font-size:11px;">${meta}${branch}${this.esc(uptime)}</div>
            ${tail ? `<div class="mt-2 px-2 py-1" style="background:var(--tblr-bg-surface-secondary,#f6f7f9);border-radius:var(--tblr-border-radius);">
                <div class="font-monospace text-truncate" style="font-size:11px;">&rsaquo; ${this.esc(tail)}</div>
                ${quiet ? `<div class="text-secondary" style="font-size:11px;">quiet ${this.esc(quiet)}${cpu == null ? '' : ` · cpu ${this.esc(String(cpu))}%`}</div>` : ''}
            </div>` : ''}
            ${this._dockRunnerActions(s, primary)}
        </div>`;
    },
```

- [ ] **Step 4: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_runner_card_render.py && node --check static/app.js`
Expected: `PASS test_autopilot_dock_runner_card_render`, no output from `node --check`.

Note: `_dockRunnerActions` does not exist yet, so the dock will throw at runtime until Task 5. That is expected and contained — `_renderFleetDock` already catches render throws and resets `_fleetSig` (`static/app.js:845`). Do not skip Task 5.

- [ ] **Step 5: Commit**

```bash
git add static/app.js tests/test_autopilot_dock_runner_card_render.py
git commit -m "feat(<TASK-ID>): runner card leads with the task and shows live output"
```

---

### Task 5: Action hierarchy — Answer first, kill in the overflow

**Files:**
- Modify: `static/app.js` (add `_dockRunnerActions` and `_dockAnswer` next to `_dockRunnerHtml`)
- Test: `tests/test_autopilot_dock_runner_actions.py`

**Interfaces:**
- Consumes: `app._dockAttention`, the `primary` condition object from Task 2.
- Produces:
  - `_dockRunnerActions(s, primary) -> string` — the action row markup.
  - `_dockAnswer(sessionId, choice) -> Promise<void>` — posts the attention decision.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_runner_actions.py`:

```python
"""Autopilot dock: the primary action follows the condition; kill is demoted."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "app.js"


def _actions_source():
    js = APP.read_text(encoding="utf-8")
    start = js.index("_dockRunnerActions(s, primary) {")
    return js[start:start + 2600]


def test_waiting_on_you_offers_answer_as_the_primary():
    src = _actions_source()
    assert "waiting_on_you" in src
    assert "Answer" in src


def test_silent_offers_nudge_only_when_inject_is_available():
    src = _actions_source()
    assert "Nudge" in src
    assert "inject" in src


def test_exited_offers_restart_only_when_restart_is_available():
    src = _actions_source()
    assert "Restart" in src
    assert "'restart'" in src


def test_lost_host_has_no_primary_verb():
    src = _actions_source()
    marker = src.index("lost_host")
    window = src[marker:marker + 220]
    assert "Reclaim" not in window, "reclaim belongs to the lease-orphan work"


def test_kill_lives_behind_the_overflow_menu():
    src = _actions_source()
    assert "dropdown" in src
    assert "Kill" in src


def test_answer_posts_expected_version_to_the_decide_endpoint():
    js = APP.read_text(encoding="utf-8")
    start = js.index("async _dockAnswer(")
    body = js[start:start + 1200]
    assert "/api/attention/requests/" in body
    assert "/decide" in body
    assert "expected_version" in body


if __name__ == "__main__":
    test_waiting_on_you_offers_answer_as_the_primary()
    test_silent_offers_nudge_only_when_inject_is_available()
    test_exited_offers_restart_only_when_restart_is_available()
    test_lost_host_has_no_primary_verb()
    test_kill_lives_behind_the_overflow_menu()
    test_answer_posts_expected_version_to_the_decide_endpoint()
    print("PASS test_autopilot_dock_runner_actions")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_runner_actions.py`
Expected: `ValueError: substring not found` — `_dockRunnerActions` does not exist.

- [ ] **Step 3: Implement both methods**

Insert immediately after `_dockRunnerHtml` in `static/app.js`:

```js
    // The primary action follows the condition, so the most useful verb is the
    // one under the cursor: a runner stuck at a prompt wants Answer, not Kill.
    // Kill is the most destructive action on the card and owned the only
    // outline button before this change — it now lives behind the overflow.
    _dockRunnerActions(s, primary) {
        const actions = s.available_actions || [];
        const task = this.esc(s.task_id || '');
        const sid = this.esc(s.runner_session_id || '');
        const watch = s.task_id
            ? `<button class="btn btn-sm btn-outline-secondary" data-runner-watch-task="${task}"><i class="ti ti-terminal-2 me-1"></i>Watch</button>`
            : '';
        let lead = '';
        if (primary.key === 'waiting_on_you') {
            lead = `<button class="btn btn-sm btn-primary" data-runner-answer="${sid}"><i class="ti ti-message-2 me-1"></i>Answer</button>`;
        } else if (primary.key === 'silent' && actions.includes('inject')) {
            lead = `<button class="btn btn-sm btn-outline-secondary" data-runner-task="${task}" data-runner-action="inject"><i class="ti ti-wand me-1"></i>Nudge</button>`;
        } else if (primary.key === 'exited' && actions.includes('restart')) {
            lead = `<button class="btn btn-sm btn-outline-secondary" data-runner-task="${task}" data-runner-action="restart"><i class="ti ti-refresh me-1"></i>Restart</button>`;
        }
        // lost_host deliberately has no primary verb: reclaiming an orphaned
        // lease belongs to the ADR-8 lease-orphan work, not to this card.
        const item = (action, icon, label) => actions.includes(action)
            ? `<button class="dropdown-item" data-runner-task="${task}" data-runner-action="${action}"><i class="ti ti-${icon} me-2"></i>${label}</button>`
            : '';
        const menu = [item('inject', 'wand', 'Nudge'), item('restart', 'refresh', 'Restart'),
                      item('open', 'external-link', 'Open'), item('kill', 'square', 'Kill')]
            .filter(Boolean).join('');
        return `<div class="mt-2 d-flex gap-2 align-items-center">
            ${lead}${watch}
            <span class="ms-auto"></span>
            ${menu ? `<div class="dropdown">
                <button class="btn btn-sm btn-ghost-secondary p-1" data-bs-toggle="dropdown" aria-label="More actions"><i class="ti ti-dots"></i></button>
                <div class="dropdown-menu dropdown-menu-end">${menu}</div>
            </div>` : ''}
        </div>`;
    },
    // Answer the runner's pending question through the same path the inbox
    // uses. expected_version is what stops this from overwriting a decision an
    // agent or another operator already made between our poll and this click.
    async _dockAnswer(sessionId, choice) {
        const ask = (this._dockAttention || {})[sessionId];
        if (!ask) return;
        const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
        try {
            const res = await fetch(
                `/api/attention/requests/${encodeURIComponent(ask.request_id)}/decide?${p}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        expected_version: ask.version,
                        choice: choice === undefined ? ask.recommended_default : choice,
                        idempotency_key: `operator-decide:${ask.request_id}`,
                    }),
                });
            const body = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(body.message || body.detail || `HTTP ${res.status}`);
        } catch (error) {
            window.alert(`Could not answer: ${error.message || error}`);
        }
        await this._loadFleetDock(true);
    },
```

- [ ] **Step 4: Wire the click handler**

In `_renderFleetDock`, beside the existing `[data-runner-action]` binding (~line 1272), add:

```js
        host.querySelectorAll('[data-runner-answer]').forEach((b) =>
            b.addEventListener('click', () => this._dockAnswer(b.getAttribute('data-runner-answer'))));
```

- [ ] **Step 5: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_runner_actions.py && node --check static/app.js`
Expected: `PASS test_autopilot_dock_runner_actions`, no output from `node --check`.

- [ ] **Step 6: Commit**

```bash
git add static/app.js tests/test_autopilot_dock_runner_actions.py
git commit -m "feat(<TASK-ID>): condition-driven runner actions, kill behind the overflow"
```

---

### Task 6: Bucket grouping, worst-first sort, collapse healthy

**Files:**
- Modify: `static/js/fleet-dock.js` (add `runnerRank`)
- Modify: `static/app.js` `_renderFleetDock` runners branch (~line 1214)
- Test: `tests/test_autopilot_dock_runner_buckets.py`

**Interfaces:**
- Consumes: `FleetDock.runnerConditions`.
- Produces: `FleetDock.runnerRank(key) -> number` — 0 is worst. Unknown keys sort last.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_runner_buckets.py`:

```python
"""Autopilot dock: worst first, and healthy runners collapse to one row."""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "fleet-dock.js"
APP = ROOT / "static" / "app.js"


def ranks(keys):
    script = (
        "globalThis.window = {};\n"
        f"require({json.dumps(str(MODULE))});\n"
        f"console.log(JSON.stringify({json.dumps(keys)}.map("
        "(k) => window.SwitchboardFleetDock.runnerRank(k))));\n"
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"node failed: {res.stderr.strip()}")
    return json.loads(res.stdout)


def test_rank_order_matches_the_ladder():
    order = ranks(["exited", "lost_host", "waiting_on_you", "silent",
                   "idle", "working", "running_unknown"])
    assert order == sorted(order), order
    assert len(set(order)) == len(order), order


def test_unknown_keys_sort_last():
    assert ranks(["nonsense-key"])[0] > ranks(["running_unknown"])[0]


def test_render_groups_and_collapses():
    js = APP.read_text(encoding="utf-8")
    branch = js[js.index("if (tab === 'runners')"):js.index("} else if (tab === 'prs'")]
    assert "runnerRank" in branch
    assert "working normally" in branch


if __name__ == "__main__":
    test_rank_order_matches_the_ladder()
    test_unknown_keys_sort_last()
    test_render_groups_and_collapses()
    print("PASS test_autopilot_dock_runner_buckets")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_runner_buckets.py`
Expected: `AssertionError: node failed: TypeError: window.SwitchboardFleetDock.runnerRank is not a function`

- [ ] **Step 3: Add `runnerRank` to `static/js/fleet-dock.js`**

Insert after `runnerConditions`:

```js
        // Sort key for the runners list. 0 is worst, so the top of the list is
        // always the thing to look at. Unknown keys sort last rather than
        // throwing — a new condition must not reorder the list by accident.
        runnerRank(key) {
            const order = ['exited', 'lost_host', 'waiting_on_you', 'silent',
                           'idle', 'working', 'running_unknown'];
            const at = order.indexOf(key);
            return at === -1 ? order.length : at;
        },
```

- [ ] **Step 4: Group the runners branch in `_renderFleetDock`**

Replace the `if (tab === 'runners') { ... }` branch with:

```js
        if (tab === 'runners') {
            const now = Date.now() / 1000;              // hoisted out in Task 7
            const FD = window.SwitchboardFleetDock;     // hoisted out in Task 7
            const decorated = runners.map((s) => ({
                s, c: FD.runnerConditions(this, s, this._dockAttention || {}, now),
            }));
            decorated.forEach((d) => { d.rank = FD.runnerRank(d.c[0].key); });
            decorated.sort((a, b) => a.rank - b.rank);
            const healthyKeys = ['working', 'running_unknown', 'idle'];
            const loud = decorated.filter((d) => !healthyKeys.includes(d.c[0].key));
            const calm = decorated.filter((d) => healthyKeys.includes(d.c[0].key));
            const buckets = [
                ['Needs you', loud.filter((d) => d.c[0].key === 'waiting_on_you')],
                ['Broken', loud.filter((d) => d.c[0].key === 'exited' || d.c[0].key === 'lost_host')],
                ['Stalled', loud.filter((d) => d.c[0].key === 'silent')],
            ];
            const groups = buckets.filter(([, rows]) => rows.length).map(([name, rows]) =>
                `<div class="text-secondary text-uppercase" style="font-size:10px;letter-spacing:.08em;margin:2px 0 4px 2px;">${this.esc(name)} · ${rows.length}</div>`
                + rows.map((d) => this._dockRunnerHtml(d.s)).join('')).join('');
            const quiet = calm.length
                ? `<div class="d-flex align-items-center gap-2 p-2 rounded" style="background:var(--tblr-bg-surface-secondary,#f6f7f9);font-size:12px;">
                    <span style="width:7px;height:7px;border-radius:50%;background:var(--tblr-green);"></span>
                    <span class="text-secondary">${calm.length} working normally</span>
                   </div>` : '';
            body = (groups || quiet)
                ? `<div class="p-2">${groups}${quiet}</div>`
                : `<div class="p-3 text-secondary small">No live runners for this project.</div>`;
        } else if (tab === 'prs' && this._dockPrUnavailable) {
```

- [ ] **Step 5: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_runner_buckets.py && node --check static/app.js && node --check static/js/fleet-dock.js`
Expected: `PASS test_autopilot_dock_runner_buckets`, no output from either check.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/js/fleet-dock.js tests/test_autopilot_dock_runner_buckets.py
git commit -m "feat(<TASK-ID>): group runners worst-first and collapse the healthy ones"
```

---

### Task 7: Tab-strip tone dots and the collapsed pill

**Files:**
- Modify: `static/app.js` `_renderFleetDock` (the `tabBtn` helper ~line 1209 and the collapsed-pill branch ~line 1197)
- Test: `tests/test_autopilot_dock_dock_pill_and_dots.py`

**Interfaces:**
- Consumes: `FleetDock.runnerConditions`, `FleetDock.runnerRank`.
- Produces: nothing consumed downstream.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_dock_pill_and_dots.py`:

```python
"""Autopilot dock: a closed tab still reports, and the pill names the reason."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "app.js"


def _render_source():
    js = APP.read_text(encoding="utf-8")
    start = js.index("_renderFleetDock(runners, prs, deploymentPayload) {")
    return js[start:js.index("workSessionsPanelHtml", start)]


def test_each_tab_carries_a_tone_dot():
    src = _render_source()
    tab_btn = src[src.index("const tabBtn ="):src.index("let body;")]
    assert "border-radius:50%" in tab_btn, "tabs need a worst-condition dot"


def test_pill_distinguishes_a_question_from_a_crash():
    src = _render_source()
    pill = src[src.index("if (collapsed)"):src.index("if (!this._dockTab)")]
    assert "waiting on you" in pill
    assert "broken" in pill
    assert "All clear" in pill


def test_pill_counts_silent_runners_separately():
    src = _render_source()
    pill = src[src.index("if (collapsed)"):src.index("if (!this._dockTab)")]
    assert "silent" in pill


if __name__ == "__main__":
    test_each_tab_carries_a_tone_dot()
    test_pill_distinguishes_a_question_from_a_crash()
    test_pill_counts_silent_runners_separately()
    print("PASS test_autopilot_dock_dock_pill_and_dots")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_dock_pill_and_dots.py`
Expected: `AssertionError` — the tab button has no dot.

- [ ] **Step 3: Compute the counts once, above the collapsed branch**

Task 6 declared `const FD` and `const now` *inside* the `if (tab === 'runners')`
branch. This step hoists them to the function scope, so **delete those two inner
declarations from the runners branch** and let it use the outer ones. Leaving
both is legal JS (the inner `const` shadows the outer) but recomputes the same
values and reads as a bug to the next person.

In `_renderFleetDock`, immediately after `const blockedPrs = prs.filter((x) => x.blocked);`, insert:

```js
        const FD = window.SwitchboardFleetDock;
        const nowSec = Date.now() / 1000;
        const runnerKeys = runners.map(
            (s) => FD.runnerConditions(this, s, this._dockAttention || {}, nowSec)[0].key);
        const nAsking = runnerKeys.filter((k) => k === 'waiting_on_you').length;
        const nSilent = runnerKeys.filter((k) => k === 'silent').length;
        const nBroken = runnerKeys.filter((k) => k === 'exited' || k === 'lost_host').length;
        const runnerTone = nBroken ? 'red' : (nAsking ? 'orange' : (nSilent ? 'yellow' : 'green'));
        const prTone = blockedPrs.length ? 'red' : (prs.length ? 'azure' : 'green');
        const deployTone = undeployed ? 'yellow' : 'green';
```

- [ ] **Step 4: Rewrite the collapsed pill**

Replace the whole `if (collapsed) { ... }` block body with:

```js
        if (collapsed) {
            const lead = nAsking ? `${nAsking} waiting on you`
                : (nBroken ? `${nBroken} broken`
                    : (blockedPrs.length ? `${blockedPrs.length} blocked` : 'All clear'));
            const dot = `var(--tblr-${nBroken ? 'red' : (nAsking ? 'orange' : (blockedPrs.length ? 'red' : 'green'))})`;
            const rest = [
                nSilent ? `${nSilent} silent` : '',
                `${running} working`,
                `${prs.length} PR${prs.length === 1 ? '' : 's'}`,
                undeployed ? `${undeployed} un-deployed` : '',
            ].filter(Boolean).join(' · ');
            host.innerHTML = `<button id="fleet-dock-pill" class="btn btn-sm shadow-sm" style="${anchor}border-radius:999px;display:inline-flex;align-items:center;gap:8px;">
                <span style="width:8px;height:8px;border-radius:50%;background:${dot};"></span>
                <span class="fw-medium">${this.esc(lead)}</span>
                <span class="text-secondary small">· ${this.esc(rest)}</span>
                <i class="ti ti-chevron-up"></i></button>`;
            document.getElementById('fleet-dock-pill').addEventListener('click', () => { this._dockCollapsed = false; rerender(); });
            return;
        }
```

- [ ] **Step 5: Add the dot to `tabBtn`**

Replace the `tabBtn` helper with:

```js
        const tabBtn = (key, label, count, tone) =>
            `<button class="btn btn-sm ${tab === key ? '' : 'btn-ghost-secondary'}" data-dock-tab="${key}" style="border-radius:0;border:0;border-bottom:2px solid ${tab === key ? 'var(--tblr-primary)' : 'transparent'};display:inline-flex;align-items:center;gap:5px;"><span style="width:6px;height:6px;border-radius:50%;background:var(--tblr-${tone});"></span>${label}<span class="text-secondary ms-1">${count}</span></button>`;
```

and update the three call sites to pass the tone:

```js
                ${tabBtn('runners', 'Runners', runners.length, runnerTone)}
                ${tabBtn('prs', 'Pull requests', prs.length, prTone)}
                ${tabBtn('deployments', 'Deployments', undeployed, deployTone)}
```

- [ ] **Step 6: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_dock_pill_and_dots.py && node --check static/app.js`
Expected: `PASS test_autopilot_dock_dock_pill_and_dots`, no output from `node --check`.

- [ ] **Step 7: Commit**

```bash
git add static/app.js tests/test_autopilot_dock_dock_pill_and_dots.py
git commit -m "feat(<TASK-ID>): tone dots per tab and a pill that names the reason"
```

---

### Task 8: Autopilot pill — drop the noun, cover runner tasks

**Files:**
- Modify: `static/js/fleet-dock.js` `autopilotHtml` (~line 74) and `loadCoverage` (~line 57)
- Modify: `static/app.js` `_loadFleetDock` (the `loadCoverage` call site)
- Test: `tests/test_autopilot_dock_autopilot_pill_labels.py`

**Interfaces:**
- Consumes: `app._dockAutopilot`.
- Produces: `FleetDock.loadCoverage(app, prs, runners) -> Promise<Object>` — note the new third parameter.

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_dock_autopilot_pill_labels.py`:

```python
"""Autopilot dock: on a surface called Autopilot, the pill shows the state verb alone."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "fleet-dock.js"


def _autopilot_source():
    js = MODULE.read_text(encoding="utf-8")
    start = js.index("autopilotHtml(app, x) {")
    return js[start:js.index("async autopilotAction(", start)]


def test_labels_are_state_verbs_not_the_noun():
    src = _autopilot_source()
    for label in ("'Driving'", "'Armed'", "'Paused'", "'Stale'", "'Arm'"):
        assert label in src, label
    assert "'Autopilot'" not in src
    assert "'Autopilot armed'" not in src


def test_tooltips_keep_the_full_sentence():
    src = _autopilot_source()
    assert "deliverable_id" in src
    assert "click to pause" in src


def test_coverage_read_includes_runner_tasks():
    js = MODULE.read_text(encoding="utf-8")
    start = js.index("async loadCoverage(app")
    body = js[start:start + 900]
    assert "runners" in body, "runner task ids must join the batched coverage read"


if __name__ == "__main__":
    test_labels_are_state_verbs_not_the_noun()
    test_tooltips_keep_the_full_sentence()
    test_coverage_read_includes_runner_tasks()
    print("PASS test_autopilot_dock_autopilot_pill_labels")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_autopilot_dock_autopilot_pill_labels.py`
Expected: `AssertionError: 'Driving'` — the labels still carry the noun.

- [ ] **Step 3: Change the five labels**

In `autopilotHtml`, replace the `states` map's label strings only — tones, icons, tooltips and actions are unchanged:

```js
            const states = {
                live: ['Driving', 'green', 'route',
                       cov.coverage === 'deliverable'
                           ? `Driven by ${cov.deliverable_id}'s autopilot — click to pause`
                           : 'Task-scoped autopilot running — click to pause', 'pause'],
                armed: ['Armed', 'azure', 'clock',
                        'Scope started; waiting for a coordinator host to pick it up — click to pause', 'pause'],
                paused: ['Paused', 'yellow', 'player-pause',
                         'Click to resume', 'resume'],
                stale: ['Stale', 'orange', 'alert-triangle',
                        'Scope holder is dead (deploy restart?) — click to re-arm', 'start'],
                none: ['Arm', 'secondary', 'player-play',
                       `Start a task-scoped autopilot for ${taskId}`, 'start'],
            };
```

- [ ] **Step 4: Widen the coverage read to runner tasks**

Replace the first two lines of `loadCoverage`'s body:

```js
        async loadCoverage(app, prs, runners) {
            const taskIds = [...new Set([
                ...(prs || []).flatMap((x) => (x.tasks || []).map((t) => t.task_id)),
                ...(runners || []).map((s) => s.task_id),
            ].filter(Boolean))];
```

and update the call site in `static/app.js` `_loadFleetDock` to pass runners:

```js
            this._dockAutopilot = await window.SwitchboardFleetDock.loadCoverage(this, prs, runners);
```

- [ ] **Step 5: Run the tests and the syntax gate**

Run: `python tests/test_autopilot_dock_autopilot_pill_labels.py && node --check static/js/fleet-dock.js && node --check static/app.js`
Expected: `PASS test_autopilot_dock_autopilot_pill_labels`, no output from either check.

- [ ] **Step 6: Commit**

```bash
git add static/js/fleet-dock.js static/app.js tests/test_autopilot_dock_autopilot_pill_labels.py
git commit -m "feat(<TASK-ID>): state-verb autopilot pill, now on runner cards too"
```

---

### Task 9: Full-suite verification

**Files:**
- No source changes. This task is the gate.

- [ ] **Step 1: Run every new test file directly**

```bash
for f in tests/test_autopilot_dock_*.py; do python "$f" || echo "FAILED $f"; done
```

Expected: one `PASS ...` line per file, no `FAILED` lines.

- [ ] **Step 2: Prove the tests can actually fail**

Temporarily change `'Driving'` back to `'Autopilot'` in `static/js/fleet-dock.js`, then run:

```bash
python tests/test_autopilot_dock_autopilot_pill_labels.py
```

Expected: `AssertionError`. Revert the change and re-run to confirm PASS. A test that cannot fail is not a test — do this before claiming the suite is green.

- [ ] **Step 3: Run the repository suite**

```bash
bash scripts/switchboard_ci.sh
```

Expected: no `FAIL` lines. If the suite reports pre-existing failures unrelated to this work, record which ones and report them rather than fixing them here.

- [ ] **Step 4: Check the dock in a real browser**

Start the dev server on :8110, open the board, and confirm: the left nav reads Autopilot, the dock header reads Autopilot, a live runner card leads with its task id and title, and the session id appears only as the tooltip on that line.

- [ ] **Step 5: Commit any fixes and open the PR**

```bash
git add -A
git commit -m "test(<TASK-ID>): verify the autopilot dock suite end to end"
```

---

## Follow-on plans (not in this plan)

- **Tab 2, pull requests** — task-first headline, merge queue as an ordered block, `blocked_reason` surfaced, and the three actions (Decide, Merge, Re-gate). Re-gate needs an audited dispatch route modelled on `/api/deployments/request`; Merge needs a server-side route over `merge_coordinator.py:452`.
- **Tab 3, deploy** — the drift banner over `health_view`, the three states, and the board-task join that `deployment_status` does not do today.
- **`tests/test_ui_deployment_fleet_tab.py`** — pytest fixtures with no `__main__`; asserts nothing under CI.
