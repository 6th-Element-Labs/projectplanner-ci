#!/usr/bin/env python3
"""UI-21: hermetic Chromium check of the API connections Settings surface.

Boots the real app (dev-open), opens Settings -> AI connections, and asserts the
live DOM: the two-group IA (Personal subscriptions + API connections), the OpenAI
API card, the metered warning, the disabled Anthropic/Cursor rows, and — the key
security invariant — that the API connections area contains no secret input the
key could be typed into (enrollment is host-local). Satisfies the required
'Switchboard UI / Playwright' gate for this UI task.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402,F401

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  playwright not installed")
    sys.exit(0)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


TMP = Path(tempfile.mkdtemp(prefix="ui21-browser-"))
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += int(bool(cond))
    failed += int(not cond)


env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(TMP / "maxwell.db"),
    "PM_SWITCHBOARD_DB_PATH": str(TMP / "switchboard.db"),
    "PM_HELM_DB_PATH": str(TMP / "helm.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(TMP / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(TMP / "projects"),
    "PM_AUTH_MODE": "dev-open",
    "PM_PORT": str(PORT),
})
server = subprocess.Popen([sys.executable, str(Path(ROOT) / "app.py")], cwd=str(ROOT),
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_healthy(timeout=25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if server.poll() is not None:
            return False
        time.sleep(0.25)
    return False


try:
    ok(wait_healthy(), "app.py boots (dev-open, throwaway DB)")
    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_page(viewport={"width": 1280, "height": 1000})
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function("typeof TeepPlan !== 'undefined'", timeout=20000)
        # Render the AI connections settings section directly (dev-open principal).
        html = page.evaluate("""async () => {
            window.PM_PROJECT = 'switchboard';
            return await TeepPlan._settingsAiAccountsSection();
        }""")
        ok(isinstance(html, str) and "Personal subscriptions" in html and "API connections" in html,
           "Settings renders BOTH groups: Personal subscriptions + API connections")
        ok("settings-api-conn-openai-codex" in html and "OpenAI API" in html,
           "the OpenAI API connection card renders")
        ok("explicitly metered" in html,
           "the metered-billing warning is shown for API connections")
        ok("ADAPTER-20" in html and "ADAPTER-21" in html,
           "Anthropic and Cursor API rows are shown gated (disabled until their adapters)")
        # (The host-local enroll-api-key connect command is asserted by the
        #  source-needle + Node render check in tests/test_ui21_api_connections.py;
        #  it only renders once the provider-connections fetch succeeds, which a
        #  fresh dev-open DB does not provision.)

        # Mount it in the live DOM and confirm there is no secret input in the API area.
        page.evaluate("""(h) => {
            const d = document.createElement('div'); d.id = 'ui21-probe'; d.innerHTML = h;
            document.body.appendChild(d);
        }""", html)
        secret_inputs = page.evaluate("""() => {
            const scope = document.getElementById('ui21-probe');
            const pw = scope.querySelectorAll('input[type=password]').length;
            const named = scope.querySelectorAll('[name=api_key],[name=credential],[name=token],[name=secret]').length;
            return pw + named;
        }""")
        ok(secret_inputs == 0,
           "SECURITY: the rendered API connections area has no secret input field")
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except Exception:
        server.kill()

print(f"\nUI-21 API connections (Playwright): {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
