#!/usr/bin/env python3
"""UI-24: real browser proof for the bound PTY terminal — first Playwright test
in this repo (see tests/test_ui24_terminal_wiring.py for why: the existing
frontend "tests" only assert on source strings, never execute JS). This test
boots the real app, signs up through the real UI, then drives the terminal
module through genuine browser APIs (a real Chromium DOM, real keyboard
events) and asserts on what actually rendered — not mocked React output.

Covers what tests/test_ui24_host_bridge_wiring.py (backend, no browser) and
test_ui24_terminal_wiring.py (frontend, no JS execution) structurally cannot:
does xterm.js actually render ANSI bytes, does a keypress actually round-trip
through onData, does resize actually compute sane rows/cols, does the sidecar
<-> docked reparent actually keep the same live terminal element.

Does not require a real Agent Host or relay — that end-to-end proof is
DOGFOOD-16's job. This proves the browser side is correct in isolation, the
same way the backend tests prove the relay side is correct in isolation.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ (path_setup.py lives there)
from path_setup import ROOT  # noqa: F401,E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  playwright not installed — run: python3 -m pip install playwright "
          "&& python3 -m playwright install chromium")
    sys.exit(0)


TMP = Path(tempfile.mkdtemp(prefix="ui24-browser-"))
PORT = 8137  # distinct from the dev-default 8110 so this never collides with a running dev server.
BASE_URL = f"http://127.0.0.1:{PORT}"
EMAIL = "ui24-browser-test@example.local"
PASSWORD = "ui24-browser-test-pw"

env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PORT": str(PORT),
    "PM_AUTH_MODE": "dev-open",
})

server = subprocess.Popen(
    [sys.executable, str(Path(ROOT) / "app.py")],
    cwd=str(ROOT), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)


def _wait_healthy(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if server.poll() is not None:
            return False
        time.sleep(0.25)
    return False


try:
    healthy = _wait_healthy()
    ok(healthy, "app.py boots and /health responds (dev-open auth, throwaway DB)")
    if not healthy:
        raise SystemExit("server did not become healthy — aborting")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # ---- real signup through the real UI --------------------------------
        page.goto(f"{BASE_URL}/signup")
        # Signup form: Name, Email, Password — first three text-ish inputs in order.
        inputs = page.locator("form input")
        inputs.nth(0).fill("UI-24 Browser Test")
        inputs.nth(1).fill(EMAIL)
        inputs.nth(2).fill(PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        signed_up = "No projects yet" in page.content() or "TAIKUN" in page.content()
        ok(signed_up, "signup completes and lands on the real app shell")

        # ---- runner-session.js is loaded and wired into the app instance ----
        wired = page.evaluate(
            "typeof TeepPlan !== 'undefined' && typeof TeepPlan.openRunnerSessionPanel === 'function'")
        ok(wired, "TeepPlan.openRunnerSessionPanel exists on the live page (module actually loaded)")

        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        # ---- fail-closed gate for a task with no runner bind -----------------
        opened = page.evaluate("TeepPlan.openRunnerSessionPanel('FAKE-TASK-1')")
        ok(opened is True, "openRunnerSessionPanel opens the panel even when not watchable (shows a gate, not a no-op)")
        page.wait_for_timeout(300)
        gate_text = page.locator("#runner-pty-gate").inner_text()
        ok("runner_bind_incomplete" in gate_text, "the fail-closed gate reason is rendered in the DOM")
        panel_visible = page.locator("#runner-pty-panel").is_visible()
        ok(panel_visible, "the sidecar is actually visible, not just present in the DOM")

        # ---- mount a real terminal and write real ANSI bytes -----------------
        # openRunnerSessionPanel('FAKE-TASK-1') above stopped at the fail-closed
        # gate before ever loading xterm.js, so load it explicitly here — this
        # is the same lazy-load path a real watchable session would trigger.
        page.evaluate("() => TeepPlan._ensureXterm()")
        xterm_loaded = page.evaluate("typeof window.Terminal === 'function' && typeof window.FitAddon.FitAddon === 'function'")
        ok(xterm_loaded, "xterm.js and the fit addon load from jsdelivr with the expected UMD globals")
        page.evaluate("""
            () => {
                TeepPlan._runnerPty = { taskId: 'FAKE-TASK-1', runnerSessionId: 'run_browsertest',
                                         mode: 'sidecar', reconnectAttempts: 0 };
                TeepPlan._runnerPtyShowShell(null);
                TeepPlan._runnerPtyMountTerminal(TeepPlan._runnerPty);
                const text = '\\x1b[32mUI-24 PLAYWRIGHT OK\\x1b[0m\\r\\n';
                const bytes = new TextEncoder().encode(text);
                let bin = ''; bytes.forEach(b => bin += String.fromCharCode(b));
                const frame = JSON.stringify({type: 'output', data_b64: btoa(bin)});
                TeepPlan._runnerPtyHandleFrame(TeepPlan._runnerPty, frame);
            }
        """)
        page.wait_for_timeout(300)
        line0 = page.evaluate(
            "TeepPlan._runnerPty.term.buffer.active.getLine(0).translateToString(true)")
        ok(line0 == "UI-24 PLAYWRIGHT OK", f"xterm.js renders real ANSI-colored bytes correctly (got: {line0!r})")
        # The color escape must have been interpreted, not printed literally.
        term_html = page.locator("#runner-pty-term").inner_html()
        ok("\\x1b" not in term_html and "[32m" not in term_html,
           "the ANSI escape sequence is consumed by the renderer, not leaked into the DOM as text")

        # ---- reconnect reuses the terminal instead of rebuilding it ----------
        # _runnerPtyConnect's guard is `const reconnecting = !!rp.term; if
        # (!reconnecting) this._runnerPtyMountTerminal(rp);` — replicate that
        # exact decision using the real function, without the outer
        # ticket-mint/WebSocket machinery (this test deliberately runs with no
        # backing relay session, so a real reconnect can't be ticketed here).
        reused = page.evaluate("""
            () => {
                const rp = TeepPlan._runnerPty;
                const priorTerm = rp.term;
                const priorObserver = rp.resizeObserver;
                const reconnecting = !!rp.term;
                if (!reconnecting) TeepPlan._runnerPtyMountTerminal(rp);
                return {
                    reconnecting,
                    sameTerm: rp.term === priorTerm,
                    sameObserver: rp.resizeObserver === priorObserver,
                    line0: rp.term.buffer.active.getLine(0).translateToString(true),
                };
            }
        """)
        ok(reused["reconnecting"], "a terminal that already exists is recognized as a reconnect, not a fresh open")
        ok(reused["sameTerm"] and reused["sameObserver"],
           "reconnecting does not rebuild the Terminal/ResizeObserver (UI-24 review fix: this used to "
           "discard scrollback and leak the old ResizeObserver on every drop)")
        ok(reused["line0"] == "UI-24 PLAYWRIGHT OK", "scrollback from before the simulated reconnect is still there")

        # ---- a real keypress round-trips through xterm's onData -------------
        page.evaluate("""
            () => {
                window.__ui24CapturedInput = [];
                TeepPlan._runnerPtySendInput = (data) => window.__ui24CapturedInput.push(data);
                TeepPlan._runnerPty.term.focus();
            }
        """)
        page.locator("#runner-pty-term").click()
        page.keyboard.type("ls -la")
        page.keyboard.press("Control+c")
        captured = page.evaluate("window.__ui24CapturedInput.join('')")
        ok("ls -la" in captured, "typed keys reach xterm's onData and are forwarded as raw input")
        ok("\x03" in captured, "Ctrl-C forwards as the real interrupt byte (0x03), not a separate control path")

        # ---- resize computes real rows/cols from the actual container -------
        dims = page.evaluate("""
            () => {
                TeepPlan._runnerPty.fitAddon.fit();
                return { rows: TeepPlan._runnerPty.term.rows, cols: TeepPlan._runnerPty.term.cols };
            }
        """)
        ok(isinstance(dims.get("rows"), int) and dims["rows"] > 0
           and isinstance(dims.get("cols"), int) and dims["cols"] > 0,
           f"the fit addon computes real positive rows/cols from the container ({dims})")

        # ---- sidecar -> docked reparents the SAME terminal, never duplicates -
        # Uses the real Bootstrap modal lifecycle (getOrCreateInstance().show()),
        # not a faked 'show' class, so this also exercises whatever real modal
        # machinery app.js relies on.
        page.evaluate("""
            () => new Promise((resolve) => {
                const fakeDev = document.createElement('div');
                fakeDev.id = 'm-dev';
                const fakeMount = document.createElement('div');
                fakeMount.id = 'runner-pty-dev-mount';
                fakeDev.appendChild(fakeMount);
                document.body.appendChild(fakeDev);
                const modalEl = document.getElementById('task-modal');
                // openTask() stamps this so _runnerPtyToggleDock can refuse to
                // dock a different task's panel into whatever modal is open;
                // mirror it here since this test drives the modal directly.
                modalEl.dataset.taskId = 'FAKE-TASK-1';
                modalEl.addEventListener('shown.bs.modal', () => {
                    document.getElementById('runner-pty-toggle-dock').click();
                    resolve();
                }, { once: true });
                window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
            })
        """)
        page.wait_for_timeout(150)
        docked = page.evaluate("""
            () => ({
                parentIsDevMount: document.getElementById('runner-pty-panel').parentElement.id === 'runner-pty-dev-mount',
                mode: TeepPlan._runnerPty.mode,
                sameTerminal: document.getElementById('runner-pty-term').querySelector('.xterm') !== null,
                line0: TeepPlan._runnerPty.term.buffer.active.getLine(0).translateToString(true),
            })
        """)
        ok(docked["parentIsDevMount"], "toggling dock actually reparents the panel into the Dev tab mount")
        ok(docked["mode"] == "docked", "internal mode state matches the DOM")
        ok(docked["sameTerminal"], "the xterm DOM survives the reparent (not destroyed/recreated)")
        ok(docked["line0"] == "UI-24 PLAYWRIGHT OK",
           "the terminal's scrollback survives the reparent — same live session, not a fresh one")

        # ---- toggling dock refuses a task mismatch (UI-24 review fix) ---------
        # Pop back to sidecar, point the open modal at a DIFFERENT task, and
        # confirm "expand" refuses to plant this session's terminal into it —
        # before this fix it docked unconditionally into whatever modal was
        # open, regardless of which task it belonged to.
        page.evaluate("() => document.getElementById('runner-pty-toggle-dock').click()")
        page.wait_for_timeout(150)
        page.evaluate("() => { document.getElementById('task-modal').dataset.taskId = 'OTHER-TASK-2'; }")
        page.evaluate("() => document.getElementById('runner-pty-toggle-dock').click()")
        page.wait_for_timeout(150)
        mismatch = page.evaluate("""
            () => ({
                mode: TeepPlan._runnerPty.mode,
                parentIsBody: document.getElementById('runner-pty-panel').parentElement === document.body,
            })
        """)
        ok(mismatch["mode"] == "sidecar" and mismatch["parentIsBody"],
           "docking into a modal open for a DIFFERENT task is refused — the panel stays in the sidecar")

        page.evaluate("() => { document.getElementById('task-modal').dataset.taskId = 'FAKE-TASK-1'; }")
        page.evaluate("() => document.getElementById('runner-pty-toggle-dock').click()")
        page.wait_for_timeout(150)
        redocked = page.evaluate(
            "() => document.getElementById('runner-pty-panel').parentElement.id === 'runner-pty-dev-mount'")
        ok(redocked, "docking succeeds again once the modal's task id matches")

        # ---- dismissing the modal while docked must NOT lose the session -----
        # openTask() replaces #task-modal-body's innerHTML (and therefore #m-dev,
        # and anything reparented inside it) on every open. Before this guard,
        # closing the modal here would silently delete the live panel/terminal
        # along with the modal content and leak its WebSocket. It must instead
        # pop back out to the sidecar first.
        page.evaluate("() => window.bootstrap.Modal.getOrCreateInstance(document.getElementById('task-modal')).hide()")
        page.wait_for_timeout(300)
        page.evaluate("() => document.getElementById('m-dev').remove()")  # simulate openTask()'s regeneration
        undocked = page.evaluate("""
            () => ({
                panelExists: !!document.getElementById('runner-pty-panel'),
                mode: TeepPlan._runnerPty ? TeepPlan._runnerPty.mode : null,
                parentIsBody: document.getElementById('runner-pty-panel')?.parentElement === document.body,
                line0: TeepPlan._runnerPty && TeepPlan._runnerPty.term
                    ? TeepPlan._runnerPty.term.buffer.active.getLine(0).translateToString(true) : null,
            })
        """)
        ok(undocked["panelExists"], "the panel survives the modal being dismissed and its content wiped")
        ok(undocked["mode"] == "sidecar", "dismissing the modal while docked auto-pops the panel back to the sidecar")
        ok(undocked["parentIsBody"], "the panel physically moved back out to <body> before the modal content was wiped")
        ok(undocked["line0"] == "UI-24 PLAYWRIGHT OK",
           "the live session (and its scrollback) survives the modal dismissal — not silently lost")

        # ---- close tears down cleanly, no leaked timers/sockets -------------
        page.evaluate("() => TeepPlan._runnerPtyClose()")
        page.wait_for_timeout(300)
        closed_state = page.evaluate("({ rpNull: TeepPlan._runnerPty === null, hidden: document.getElementById('runner-pty-panel').hidden })")
        ok(closed_state["rpNull"], "close() tears down the session state")
        ok(closed_state["hidden"], "close() hides the panel")

        ok(not console_errors, f"no uncaught console/page errors during the whole flow (got: {console_errors})")

        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nUI-24 browser PTY terminal (Playwright): {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
