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
# BUG-110: xterm is vendored under /vendor/xterm/ (ApexCharts pattern) so hermetic
# Playwright never depends on jsDelivr under parallel CI load.
ok("/vendor/xterm/xterm.css" in INDEX, "xterm.js CSS is linked from the vendored static path (eager, small)")
ok("XTERM_JS_SRC" in RUNNER_SESSION and "/vendor/xterm/xterm.js" in RUNNER_SESSION,
   "xterm.js JS bundle URL is defined for lazy loading from /vendor/")
ok("XTERM_FIT_SRC" in RUNNER_SESSION and "/vendor/xterm/addon-fit.js" in RUNNER_SESSION,
   "the fit addon URL is defined for lazy loading from /vendor/")
ok("_ensureXterm" in RUNNER_SESSION and "_ensureScript(this.XTERM_JS_SRC)" in RUNNER_SESSION,
   "xterm.js loads through the existing _ensureScript lazy-load helper (Mermaid/ApexCharts pattern)")
ok("cdn.jsdelivr.net" not in RUNNER_SESSION,
   "runner-session.js has no jsDelivr dependency")
ok("cdn.jsdelivr.net" not in INDEX,
   "index.html product chrome has no jsDelivr dependency (Tabler/xterm/Bootstrap vendored)")
ok("new window.Terminal(" in RUNNER_SESSION, "a real Terminal instance is constructed")
ok("new window.FitAddon.FitAddon()" in RUNNER_SESSION, "the fit addon is instantiated")

# ---- one entry point, real relay connection, not the old badge-only stub ----
ok("openRunnerSessionPanel" in RUNNER_SESSION, "openRunnerSessionPanel is the terminal's one entry point")
ok("openRunnerWatch" not in COMPOSED, "the old badge-only openRunnerWatch is fully retired")
ok("new WebSocket(" in RUNNER_SESSION, "a real browser WebSocket connects to the relay")
# SIMPLIFY-10: the browser no longer mints against a runner id it chose. One
# task-scoped command opens the session; the server resolves the execution,
# attaches the host tunnel, and mints the ticket.
ok("/execution/open" in RUNNER_SESSION,
   "the terminal opens through the task-scoped open_session command")
ok("/pty/ticket" not in RUNNER_SESSION,
   "the browser no longer mints its own relay ticket for a self-chosen runner")
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
ok("binaryType = 'arraybuffer'" in RUNNER_SESSION and "_runnerPtyDecodeFrame" in RUNNER_SESSION,
   "browser uses binary SB1 frames (SIMPLIFY-9)")
ok("_runnerPtyEncodeFrame('in'" in RUNNER_SESSION or "encodeFrame('in'" in RUNNER_SESSION
   or "type === 'out'" in RUNNER_SESSION,
   "browser speaks out/in (not JSON data_b64 output/input)")

# ---- reconnect / replay, not a dead connection on drop ----------------------
ok("scheduleReconnect" in RUNNER_SESSION or "reconnectAttempts" in RUNNER_SESSION,
   "a dropped relay connection retries with backoff")
ok("'snapshot'" in RUNNER_SESSION or '"snapshot"' in RUNNER_SESSION
   or "type === 'snapshot'" in RUNNER_SESSION,
   "snapshot frames are handled the same as out frames")
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
for needle in ("data-session-state", "Ready for an agent", "Codex is working",
               "Blocked, agent still live", "Talk to agent"):
    ok(needle in APP, f"state-driven task session contract is present: {needle}")
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
ok("_runnerPtyIntentTask" in RUNNER_SESSION and "include_stale=true" in RUNNER_SESSION,
   "closing a watched runner remembers repeat-click intent and reopens its truthful stale gate")
ok("opts.includeStale" in RUNNER_SESSION and "!sessions.length" in RUNNER_SESSION,
   "a fresh page can discover stale runner history while never-run tasks still fall back to authoring")
# BUG-91: one task accumulates one runner row per dispatch attempt (SEG-2 held 8
# rows across 3 hosts and 31 hours). A client-side memo of WHICH runner therefore
# outranked the truth on every reopen. The memo is now a task id only; the server
# — which already orders sessions newest-first and returns the first watchable
# one — is the sole authority on runner identity.
ok("rememberedSid" not in RUNNER_SESSION and "rememberedSession" not in RUNNER_SESSION,
   "no client-side memo of a runner identity survives anywhere in the open path")
ok("this._runnerPtyIntentTask = id;" in RUNNER_SESSION
   and "runnerSessionId: rememberedSid" not in RUNNER_SESSION,
   "the remembered hint is a bare task id, never a {taskId, runnerSessionId} pair")
ok("const currentSession = sessions[0] || null;" in RUNNER_SESSION,
   "the refused-gate path shows the server's newest session, not a remembered one")
# BUG-91: when nothing started, the dispatcher's reason ("capacity exhausted for
# co-general: cap=4") is the useful message — not a description of the debris.
ok("watch?.dispatch" in RUNNER_SESSION,
   "the refusal gate reads the server's dispatch outcome")
ok("dispatch?.state === 'needs_attention'" in RUNNER_SESSION,
   "a wake queued far too long renders as needs_attention rather than a hard error")
ok("dispatch.dispatch_attempt" in RUNNER_SESSION,
   "a repeatedly-retried dispatch shows its attempt count instead of looking like a one-off")
# COORD-44 / UI-58: the refusal gate carries the ONE repair action, and every
# surface goes through the same start_task() operation — the browser never
# assembles a wake or picks a runner.
ok("runner-pty-start-retry" in RUNNER_SESSION and "startTaskSession" in RUNNER_SESSION,
   "the refusal gate offers Start/Retry wired to the unified start operation")
ok("/start`" in RUNNER_SESSION,
   "the gate's Start/Retry calls the unified /start endpoint")
ok("data.execution_id && data.relay_url" in RUNNER_SESSION
   and "Connected to the reserved session" in RUNNER_SESSION,
   "Start opens the reserved relay immediately instead of waiting for runner registration")
ok("rp.relayUrl" in RUNNER_SESSION and "rp.relayExpiresAt" in RUNNER_SESSION,
   "browser reconnect reuses the pending capability until its explicit expiry")
ok("}/start`" in APP and "action === 'attach'" in APP,
   "the task modal's codex Start uses the same /start endpoint and handles attach")
ok("runtime: rt" in APP and "}/dispatch`" in APP,
   "the claude-code queued dispatch path is preserved unchanged")
ok("_runnerPtyCloseTimer" in RUNNER_SESSION,
   "a pending close animation cannot hide a runner panel that was immediately reopened")

# ---- Proof Console retargeted to the panel, not an inline duplicate --------
ok("openRunnerSessionPanel" in PROOF, "the Proof Console's Watch/Chat button opens the shared panel")
ok("openRunnerWatch" not in PROOF, "the Proof Console no longer calls the retired badge-only opener")

# ---- one plain-language chat composer stays bound to the same exact PTY ----
for needle in ("Message the live agent", "delivery is acknowledged against this exact run",
               "request_runner_inject"):
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
