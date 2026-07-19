#!/usr/bin/env python3
"""UI-24: bound PTY terminal frontend wiring — same string-assertion convention
as tests/test_ui17_proof_console.py / test_coord34_runner_bind.py (this repo has
no JS execution test harness; genuine render/input behavior is instead proven by
tests/browser/test_ui24_pty_terminal.py via Playwright)."""
from __future__ import annotations

from pathlib import Path

from path_setup import ROOT  # noqa: F401
from scripts.frontend_test_source import read_frontend_source

STATIC = Path(ROOT) / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
RUNNER_SESSION = (STATIC / "js" / "runner-session.js").read_text(encoding="utf-8")
MISSION = (STATIC / "js" / "mission.js").read_text(encoding="utf-8")
PROOF = (STATIC / "js" / "proof-console.js").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
COMPOSED = read_frontend_source(str(ROOT))

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# ---- xterm.js is actually wired, not just aspirational -----------------------
ok("@xterm/xterm" in INDEX, "xterm.js CSS is linked in index.html head (eager, small)")
ok("XTERM_JS_SRC" in RUNNER_SESSION and "@xterm/xterm" in RUNNER_SESSION,
   "xterm.js JS bundle URL is defined for lazy loading")
ok("XTERM_FIT_SRC" in RUNNER_SESSION and "@xterm/addon-fit" in RUNNER_SESSION,
   "the fit addon URL is defined for lazy loading")
ok("_ensureXterm" in RUNNER_SESSION and "_ensureScript(this.XTERM_JS_SRC)" in RUNNER_SESSION,
   "xterm.js loads through the existing _ensureScript lazy-load helper (Mermaid/ApexCharts pattern)")
ok("new window.Terminal(" in RUNNER_SESSION, "a real Terminal instance is constructed")
ok("new window.FitAddon.FitAddon()" in RUNNER_SESSION, "the fit addon is instantiated")

# ---- one entry point, real relay connection, not the old badge-only stub ----
ok("openRunnerSessionPanel" in RUNNER_SESSION, "openRunnerSessionPanel is the terminal's one entry point")
ok("openRunnerWatch" not in COMPOSED, "the old badge-only openRunnerWatch is fully retired")
ok("new WebSocket(" in RUNNER_SESSION, "a real browser WebSocket connects to the relay")
ok("/pty/ticket" in RUNNER_SESSION, "the browser mints its own relay ticket via the synchronous pty/ticket endpoint")
ok("request_runner_open" in RUNNER_SESSION,
   "opening watch also ensures the host tunnel via request_runner_open (idempotent)")
ok("term.write(bytes)" in RUNNER_SESSION or "term.write(" in RUNNER_SESSION,
   "inbound output frames are written into the terminal, not just logged")
ok("_runnerPtySendInput" in RUNNER_SESSION and "term.onData(" in RUNNER_SESSION,
   "raw terminal input (keys, paste, Ctrl-C) forwards through xterm's onData, not a separate input box")
ok("_runnerPtySendResize" in RUNNER_SESSION and "ResizeObserver" in RUNNER_SESSION,
   "container resize forwards a resize frame")
ok("_runnerPtyEncodeFrame('resize', { rows: rp.term.rows, cols: rp.term.cols })" in RUNNER_SESSION,
   "resize frames carry rows/cols matching the relay frame contract")

# ---- reconnect / replay, not a dead connection on drop ----------------------
ok("scheduleReconnect" in RUNNER_SESSION or "reconnectAttempts" in RUNNER_SESSION,
   "a dropped relay connection retries with backoff")
ok("'replay'" in RUNNER_SESSION or '"replay"' in RUNNER_SESSION,
   "replay frames are handled the same as output frames")
ok("rp.reconnectAttempts = 0" in RUNNER_SESSION, "a successful (re)connect resets the backoff counter")

# ---- one terminal, two doors: sidecar <-> docked reparenting, never duplicated
ok("tk-pty-sidecar" in INDEX and "tk-pty-docked" in INDEX,
   "the panel supports both the sidecar and docked presentation")
ok("_runnerPtyToggleDock" in RUNNER_SESSION, "there is one explicit handoff between sidecar and docked")
ok("dockInto.appendChild(els.panel)" in RUNNER_SESSION,
   "docking reparents the existing panel element rather than creating a second one")
ok("this._runnerPty.runnerSessionId === sid" in RUNNER_SESSION,
   "re-opening the same already-connected session moves containers instead of reconnecting")
ok("runner-pty-dev-mount" in APP, "the task-detail Dev tab provides a dock target for the same panel")
ok("taskPrimaryRunnerHtml" in APP and "task-primary-start" in APP,
   "the first task-details surface exposes the primary Start task action")
ok("task-primary-watch-here" in APP and "task-primary-watch-sidecar" in APP,
   "the first task-details surface exposes both in-modal and side-panel Watch actions")
ok("runner-pty-details-mount" in APP and "modalDetailsMount || modalDevMount" in RUNNER_SESSION,
   "the shared PTY docks into task Details first while retaining the Dev-tab fallback")
ok("dockInto: mount || undefined" in APP or "dockInto:" in APP,
   "the Dev tab's Watch/Chat button docks in place instead of opening a duplicate sidecar")

# ---- ambient trigger: click a task box on the Mission dependency graph ------
ok("openRunnerSessionPanel" in MISSION, "Mission graph node clicks can open the terminal panel")
ok("fallbackIfNotWatchable" in MISSION,
   "a task with no live runner falls back to the existing node-actions modal instead of a dead panel")
ok("openNodeModal" in MISSION, "the pre-existing deliverable-link node modal is preserved as the fallback")
ok("mission-dag-node" in APP and "await this.openRunnerSessionPanel" in APP,
   "the visible dependency-map pills use the same runner-first path as Mermaid graph nodes")
ok("_runnerPtyLast" in RUNNER_SESSION and "include_stale=true" in RUNNER_SESSION,
   "closing a watched runner remembers repeat-click intent and reopens its truthful stale gate")
ok("opts.includeStale" in RUNNER_SESSION and "!sessions.length" in RUNNER_SESSION,
   "a fresh page can discover stale runner history while never-run tasks still fall back to authoring")
ok("if (rememberedSid)" in RUNNER_SESSION
   and "this._runnerPtyLast = { taskId: id, runnerSessionId: rememberedSid }" in RUNNER_SESSION,
   "a discovered stale runner retains its identity across close/reopen even if later discovery is empty")
ok("_runnerPtyCloseTimer" in RUNNER_SESSION,
   "a pending close animation cannot hide a runner panel that was immediately reopened")

# ---- Proof Console retargeted to the panel, not an inline duplicate --------
ok("openRunnerSessionPanel" in PROOF, "the Proof Console's Watch/Chat button opens the shared panel")
ok("openRunnerWatch" not in PROOF, "the Proof Console no longer calls the retired badge-only opener")

# ---- chat composer + shortcuts stay a higher-level path into the same PTY --
for needle in ("data-runner-chat-kind=\"redirect\"", "data-runner-chat-kind=\"hold\"",
               "data-runner-chat-kind=\"approve\"", "request_runner_inject"):
    ok(needle in INDEX or needle in RUNNER_SESSION, f"chat/shortcut wiring present: {needle}")
ok("e.preventDefault()" in RUNNER_SESSION and "!e.isComposing" in RUNNER_SESSION,
   "Enter submits the composer exactly once without swallowing IME composition")
ok("_runnerPtyAwaitChatDelivery" in RUNNER_SESSION and "Delivered to ${rp.taskId}" in RUNNER_SESSION,
   "session chat distinguishes a queued request from confirmed host delivery")
ok("els.chatInput.value = text" in RUNNER_SESSION,
   "failed session chat restores the operator's text instead of discarding it")

# ---- openTask() re-renders an already-open modal without a hide.bs.modal --
# ---- event (e.g. revokeClaim()) — evacuate a docked panel first, or its ---
# ---- innerHTML rewrite silently destroys it with no cleanup ---------------
ok("_runnerPtyEvacuateIfDocked" in RUNNER_SESSION,
   "a reusable evacuate-if-docked primitive exists, shared by the modal-hide guard and app.js")
ok("this._runnerPtyEvacuateIfDocked" in APP,
   "openTask() calls the evacuate guard before rewriting task-modal-body's innerHTML")
ok("taskModalEl.dataset.taskId = t.task_id" in APP,
   "openTask() stamps which task the modal is showing, so dock-target matching has a signal to check")
ok("modal.dataset.taskId === String(rp.taskId" in RUNNER_SESSION,
   "docking into the task-detail modal checks its task id matches the panel's own session, "
   "not just that some modal happens to be open")

# ---- kill stays human-gated through the shared Tabler confirm, not window.confirm
ok("this._confirm({" in RUNNER_SESSION and "Kill runner" in RUNNER_SESSION,
   "runner kill is confirmed through the shared Tabler dialog with a specific prompt")
ok("window.confirm(`Request runner kill" not in COMPOSED,
   "the old bare window.confirm kill prompt is gone")

print(f"\nUI-24 terminal wiring: {passed} passed, {failed} failed")
import sys  # noqa: E402
sys.exit(1 if failed else 0)
