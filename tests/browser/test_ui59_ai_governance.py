#!/usr/bin/env python3
"""UI-59: operator AI allowance readback and server-authoritative controls."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="ui59-ai-governance-"))
env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "dev-open",
    "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
})
(tmp / "projects").mkdir(parents=True)

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_ready(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if server.poll() is not None:
            raise RuntimeError((server.stdout.read() if server.stdout else "")[-2000:])
        time.sleep(.2)
    raise TimeoutError("app did not become ready")


snapshot = {
    "schema": "switchboard.ai_governance.v1",
    "new_spend_disabled": False,
    "principals": [{
        "principal_id": "user-shared-7", "display_name": "shared@example.test",
        "use_llm_granted": True,
        "allowance": {"monthly_usd": 8, "prompts_per_hour": 5, "prompts_per_day": 20},
        "usage": {"monthly_cost_usd": 2.25, "reserved_cost_usd": .75,
                  "prompts_hour": 3, "prompts_day": 9, "active_jobs": 1, "queued_jobs": 1},
        "policy": {"max_prompt_chars": 12000, "max_completion_tokens": 1800,
                   "max_agent_iterations": 4, "allowed_models": ["gpt-5-mini"],
                   "service_tier": "default"},
        "last_denial": {"reason": "hourly_prompt_limit", "message": "5 prompts per hour"},
    }],
    "recent_denials": [{"principal_id": "user-shared-7", "reason": "budget_exhausted",
                        "message": "Monthly allowance exhausted", "entry_point": "mcp.ask_plan"}],
}

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("PASS " if condition else "FAIL ") + message)
    passed += int(bool(condition)); failed += int(not condition)


try:
    wait_ready()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        writes = []
        page.add_init_script("window.mermaid={initialize:()=>{},render:async()=>({svg:'<svg></svg>'})};")

        def governance(route):
            if route.request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(snapshot))
            else:
                writes.append({"method": route.request.method, "url": route.request.url,
                               "body": json.loads(route.request.post_data or "{}")})
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        page.route("**/api/projects/maxwell/ai-governance**", governance)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base + "/?project=maxwell#tab-settings/ai-governance")
        page.wait_for_selector("#settings-ai-governance")

        text = page.locator("#settings-ai-governance").inner_text()
        ok("shared@example.test" in text and "$3.00 / $8.00" in text,
           "shows per-principal actual plus reserved consumption against allowance")
        ok("3 / 5 hour" in text and "9 / 20 day" in text and "1 active · 1 queued" in text,
           "shows request, active-job, and queue consumption")
        ok("1,800 output tokens · 4 iterations" in text and "gpt-5-mini · default" in text,
           "shows bounded completion, iteration, model, and service-tier policy")
        ok("hourly_prompt_limit" in text and "budget_exhausted" in text and "mcp.ask_plan" in text,
           "shows attributed principal denial reasons and entry point")
        ok("cannot approve a call or bypass a denial" in text,
           "states that the UI is readback/control and the pre-call governor is authoritative")

        page.get_by_role("button", name="Revoke").click()
        page.wait_for_timeout(100)
        page.get_by_role("button", name="Stop new spend").click()
        page.wait_for_timeout(100)
        ok(any(w["url"].split("?")[0].endswith("/principals/user-shared-7/revoke")
               and w["body"] == {"scope": "use:llm", "reason": "operator_revoked"}
               for w in writes), "revoke sends only the use:llm control to the governor")
        ok(any(w["url"].split("?")[0].endswith("/kill-switch")
               and w["body"]["new_spend_disabled"] is True for w in writes),
           "global stop sends a server-side new-spend kill-switch command")
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\nUI-59 AI governance (Playwright): {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
