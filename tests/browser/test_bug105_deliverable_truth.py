#!/usr/bin/env python3
"""BUG-105: Chromium proves Done work and current counts stay visible."""
from __future__ import annotations

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


tmp = Path(tempfile.mkdtemp(prefix="bug105-browser-"))
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
os.environ.update({key: value for key, value in env.items() if key.startswith("PM_")})
(tmp / "projects").mkdir(parents=True)

import mission_narrative  # noqa: E402
import store  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
deliverable_id = "bug105-deliverable"
store.create_deliverable({
    "id": deliverable_id,
    "title": "Lifecycle truth",
    "status": "in_review",
    "end_state": "Every linked task stays visible.",
}, actor="test", project="maxwell")

task_specs = [
    ("Not Started", "Queued work"),
    ("In Progress", "Active work"),
    ("In Review", "Review work"),
    ("In Review", "Merged work"),
]
task_ids: list[str] = []
for status, title in task_specs:
    task = store.create_task(
        {"workstream_id": "LIFE", "title": title, "status": status},
        actor="test", project="maxwell",
    )
    task_ids.append(task["task_id"])
    store.link_task_to_deliverable(
        deliverable_id, "maxwell", task["task_id"],
        data={"role": "implementation", "blocks_deliverable": True},
        actor="test", project="maxwell",
    )

before = store.get_mission_status(project="maxwell", deliverable_id=deliverable_id)
fingerprint = mission_narrative.brief_source_fingerprint(before)
store.set_deliverable_narration(
    deliverable_id,
    "**Lifecycle truth** is now _in_review_ — 0 of 4 linked tasks complete.",
    source_fingerprint=fingerprint,
    model="deterministic",
    actor="test",
    project="maxwell",
)
store.mark_task_merged(
    task_ids[-1], "a" * 40, pr_number=105,
    pr_url="https://github.com/example/project/pull/105",
    actor="github-webhook", project="maxwell",
)

status = store.get_mission_status(project="maxwell", deliverable_id=deliverable_id)
assert status["progress"]["linked_task_count"] == 4, status["progress"]
assert status["progress"]["done_with_proof_count"] == 1, status["progress"]
assert status["ceo_narrative_state"]["display_source"] == "live_projection"
assert "1 of 4 linked tasks complete" in status["ceo_narrative"]
assert "0 of 4" in status["ceo_narrative_raw"]

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)


def wait_ready(timeout: float = 25) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if server.poll() is not None:
            output = server.stdout.read() if server.stdout else ""
            raise RuntimeError(f"app exited before ready: {output[-2000:]}")
        time.sleep(0.2)
    raise TimeoutError("app did not become ready")


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        page.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        page.goto(base + f"/?project=maxwell&deliverable={deliverable_id}#tab-mission")
        ledger = page.locator('[data-mission-work-ledger="all-linked-tasks"]')
        ledger.wait_for(state="visible")
        assert page.locator("#mission-detail").get_attribute("open") is None
        assert ledger.locator("tbody tr").count() == 4
        headings = ledger.locator("thead").inner_text().lower()
        assert "role" in headings and "autopilot" in headings, headings
        for expected_status in ("Not Started", "In Progress", "In Review", "Done"):
            assert ledger.locator(f'tr[data-task-status="{expected_status}"]').count() == 1
        active_row = ledger.locator(f'tr[data-mission-task-row="{task_ids[1]}"]')
        assert "implementation" in active_row.inner_text()
        assert active_row.locator('[data-autopilot-scope="task"]').count() == 1
        done_row = ledger.locator(f'tr[data-mission-task-row="{task_ids[-1]}"]')
        assert done_row.is_visible()
        assert "Merged code" in done_row.inner_text()
        assert done_row.locator('[data-autopilot-scope="task"]').count() == 0
        page_text = page.locator("#mission-page").inner_text()
        assert "1 of 4 linked tasks complete" in page_text
        assert "0 of 4 linked tasks complete" not in page_text
        assert "Stored narrative is updating; counts above are live." in page_text
        assert not console_errors, console_errors
        browser.close()
    print("PASS BUG-105 all lifecycle states remain visible in the primary work ledger")
    print("PASS BUG-105 stale stored prose is hidden behind a live progress summary")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
