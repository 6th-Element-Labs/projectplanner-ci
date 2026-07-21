#!/usr/bin/env python3
"""UI-26: browser provider choice through Switchboard Connect.

Same string-assertion convention as test_ui24_terminal_wiring.py — this repo
has no JS execution test harness for source checks; genuine render/click
behavior is proven separately by tests/browser/test_ui26_codex_dispatch.py.

DISPATCH-12 makes provider choice data on one Start operation. These source
checks stay paired with the real browser proof for static/app.js.
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
# DISPATCH-12: provider is data; Start is the one operation.
ok("JSON.stringify({ project: proj, runtime: rt })" in APP,
   "Codex and Claude send the same Start envelope")
dispatch_block = APP[APP.index("async dispatchTask"):APP.index("async _openDirectRunnerWhenReady")]
ok("}/start`" in dispatch_block and "}/dispatch`" not in dispatch_block,
   "the browser no longer keeps a provider-specific dispatch path")
ok("if (data.action === 'attach' || data.action === 'starting')" in dispatch_block,
   "attach and in-flight dedupe are provider-neutral")

# ---- confirm/flash copy is honest and runtime-specific, not copy-pasted ---
ok("Switchboard Connect assigns the task" in APP and "available provider capacity" in APP,
   "the confirmation describes provider-neutral Connect placement")
ok("MCP connection and workspace configuration already installed" in APP,
   "the browser does not claim dispatch creates communication configuration")
ok("waiting for available Codex capacity" in APP,
   "an honest queued-with-no-capacity message exists for Codex")
ok("Use Watch above to see and steer the same terminal" in APP,
   "the running-state copy points the operator at the UI-24 terminal, not a nonexistent Codex session URL")

# ---- the live dispatch-panel poller distinguishes runtimes, not just the button --
ok("const isCodex = d.runtime === 'codex';" in APP,
   "_loadDispatch branches its rendering on the dispatch's actual runtime")
ok("Codex via Connect" in APP and "Claude via Connect" in APP,
   "the live panel distinguishes providers without fixing their placement")
ok("(!isCodex && d.session_url)" in APP,
   "the panel never offers an 'Open Claude session' link for a codex dispatch (no such URL exists for it)")

print(f"\nUI-26 codex dispatch (source): {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
