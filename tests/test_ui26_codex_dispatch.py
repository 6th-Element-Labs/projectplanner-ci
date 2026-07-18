#!/usr/bin/env python3
"""UI-26: browser dispatch to the operator's own Codex Agent Host.

Same string-assertion convention as test_ui24_terminal_wiring.py — this repo
has no JS execution test harness for source checks; genuine render/click
behavior is proven separately by tests/browser/test_ui26_codex_dispatch.py.

The backend dispatch endpoint and dispatch.py already fully implement a codex
runtime (see dispatch.py:126-215, merged pre-existing) — this task only adds
the missing UI affordance, so these checks are scoped to static/app.js.
"""
from __future__ import annotations

from pathlib import Path

from path_setup import ROOT  # noqa: F401

APP = (Path(ROOT) / "static" / "app.js").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# ---- both dispatch actions exist, wired to distinct runtimes ---------------
ok('id="edit-dispatch"' in APP and 'id="edit-dispatch-codex"' in APP,
   "the Dev tab renders both a Claude Code button and a Codex button")
ok("this.dispatchTask(t.task_id, 'claude-code')" in APP,
   "the Claude Code button explicitly passes runtime='claude-code'")
ok("this.dispatchTask(t.task_id, 'codex')" in APP,
   "the new Codex button explicitly passes runtime='codex'")

# ---- dispatchTask actually sends the runtime, doesn't just accept it -------
ok("async dispatchTask(id, runtime)" in APP, "dispatchTask takes a runtime parameter")
ok("body: JSON.stringify({ project: proj, runtime: rt })" in APP,
   "the POST body carries the selected runtime to the existing dispatch endpoint")

# ---- confirm/flash copy is honest and runtime-specific, not copy-pasted ---
ok("your own registered Codex Agent Host" in APP,
   "the Codex confirm dialog describes the real target (operator's own host), not a vendor cloud")
ok("your personal ChatGPT/Codex login" in APP,
   "the Codex confirm dialog is explicit this runs under the operator's own credentials")
ok("no Codex-capable host is online for this lane yet" in APP,
   "an honest queued-with-no-host message exists for the codex path, matching the existing Claude one")
ok("Use Watch above to see it live" in APP,
   "the running-state copy points the operator at the UI-24 terminal, not a nonexistent Codex session URL")

# ---- the live dispatch-panel poller distinguishes runtimes, not just the button --
ok("const isCodex = d.runtime === 'codex';" in APP,
   "_loadDispatch branches its rendering on the dispatch's actual runtime")
ok("Codex host dispatch" in APP, "the live panel labels a codex dispatch distinctly from Claude cloud")
ok("(!isCodex && d.session_url)" in APP,
   "the panel never offers an 'Open Claude session' link for a codex dispatch (no such URL exists for it)")

print(f"\nUI-26 codex dispatch (source): {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
