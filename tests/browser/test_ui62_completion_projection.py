#!/usr/bin/env python3
"""UI-62: one completion projection across board, task, mission, and PR dock."""
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


tmp = Path(tempfile.mkdtemp(prefix="ui62-browser-"))
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

import store  # noqa: E402
from switchboard.storage.repositories import completion_runs  # noqa: E402


store.init_project_registry()
store.init_db("maxwell")
route_810_id = store.create_task(
    {"workstream_id": "ROUTE", "title": "Red exact-head CI needs remediation",
     "status": "In Review"},
    actor="test", project="maxwell")["task_id"]
route_811_id = store.create_task(
    {"workstream_id": "ROUTE", "title": "Green draft needs coordination retry",
     "status": "In Review"},
    actor="test", project="maxwell")["task_id"]
route_admitted_id = store.create_task(
    {"workstream_id": "ROUTE", "title": "Remediation admitted to capacity",
     "status": "In Review"},
    actor="test", project="maxwell")["task_id"]
route_queue_id = store.create_task(
    {"workstream_id": "ROUTE", "title": "Exact head ready to queue",
     "status": "In Review"},
    actor="test", project="maxwell")["task_id"]
route_done_id = store.create_task(
    {"workstream_id": "ROUTE", "title": "Canonical merge reconciled",
     "status": "In Review"},
    actor="test", project="maxwell")["task_id"]

remediation_run = completion_runs.transition_completion_run({
    "task_id": route_810_id, "pr_number": 810, "head_sha": "8" * 40,
    "state": "blocked", "route": "remediation",
    "reason_code": "required_ci_failed", "desired_role": "remediation",
    "board_status": "Blocked", "next_retry_at": time.time() + 60,
    "evidence_refs": {"decision": {"effect": "start_remediation"}},
}, actor="test", project="maxwell")
retry_run = completion_runs.transition_completion_run({
    "task_id": route_811_id, "pr_number": 811, "head_sha": "9" * 40,
    "state": "blocked", "route": "coordination_retry",
    "reason_code": "current_head_review_merge_start_failed",
    "desired_role": "review_merge", "board_status": "In Review",
    "next_retry_at": time.time() + 30,
    "evidence_refs": {"decision": {"effect": "retry_coordination"}},
}, actor="test", project="maxwell")
admitted_run = completion_runs.transition_completion_run({
    "task_id": route_admitted_id, "pr_number": 812, "head_sha": "a" * 40,
    "state": "implementing", "route": "remediation",
    "reason_code": "remediation_admitted", "desired_role": "remediation",
    "board_status": "In Progress",
    "evidence_refs": {"decision": {"effect": "monitor_remediation"}},
}, actor="test", project="maxwell")
queue_run = completion_runs.transition_completion_run({
    "task_id": route_queue_id, "pr_number": 813, "head_sha": "b" * 40,
    "state": "ready_to_queue", "route": "review_merge",
    "reason_code": "exact_head_gates_green", "desired_role": "review_merge",
    "board_status": "In Review",
    "evidence_refs": {"decision": {"effect": "enqueue_merge"}},
}, actor="test", project="maxwell")
done_sha = "c" * 40
done_run = completion_runs.transition_completion_run({
    "task_id": route_done_id, "pr_number": 814, "head_sha": "d" * 40,
    "state": "done", "route": "none",
    "reason_code": "canonical_merge_reconciled", "desired_role": "",
    "board_status": "Done",
    "evidence_refs": {
        "decision": {"effect": "none"},
        "merge": {
            "merged_sha": done_sha,
            "provenance_source": "github_pr_merged",
            "repo_role": "canonical",
        },
    },
}, actor="test", project="maxwell")
store.mark_task_merged(
    route_done_id, done_sha, pr_number=814, head_sha="d" * 40,
    actor="test", project="maxwell", provenance_source="github_pr_merged")

store.create_deliverable({
    "id": "ui62-browser", "title": "Completion routing",
    "status": "in_progress", "end_state": "Every PR route is visible.",
}, actor="test", project="maxwell")
for task_id in (
    route_810_id, route_811_id, route_admitted_id, route_queue_id, route_done_id,
):
    store.link_task_to_deliverable(
        "ui62-browser", "maxwell", task_id,
        data={"role": "contributes", "blocks_deliverable": True},
        actor="test", project="maxwell")


def projection(run):
    return {
        "schema": "switchboard.completion_projection.v1",
        "task_id": run["task_id"], "pr_number": run["pr_number"],
        "head_sha": run["head_sha"], "desired_head": run["head_sha"],
        "state": run["state"], "route": run["route"],
        "reason_code": run["reason_code"], "desired_role": run["desired_role"],
        "route_owner": (
            "remediation agent" if run["route"] == "remediation" else "coordinator"),
        "retry_deadline": run["next_retry_at"],
        "current_effect": (
            "start_remediation" if run["route"] == "remediation"
            else "retry_coordination"),
        "board_status": run["board_status"], "attempt": run["attempt"],
        "state_version": run["state_version"], "merged_sha": None,
        "terminal": False,
    }


pr_payload = {
    "schema": "switchboard.open_prs.v1", "project": "maxwell",
    "repo": "example/projectplanner", "blocked_count": 1,
    "prs": [
        {
            "number": 810, "title": "ROUTE-810: repair red CI", "url": "#",
            "draft": True, "author": "agent", "head_sha": "8" * 40,
            "updated_at": time.time(), "stalled": False, "auto_merge": False,
            "queue_position": 0, "mergeable_state": "blocked",
            "ci_state": "failure", "ci_failing": ["Switchboard CI / VM gate"],
            "blocked": True, "blocked_reason": "VM gate failed",
            "tasks": [{"task_id": route_810_id, "status": "Blocked"}],
            "completion_projection": projection(remediation_run),
        },
        {
            "number": 811, "title": "ROUTE-811: retry review/merge", "url": "#",
            "draft": True, "author": "agent", "head_sha": "9" * 40,
            "updated_at": time.time(), "stalled": False, "auto_merge": False,
            "queue_position": 0, "mergeable_state": "clean",
            "ci_state": "success", "ci_failing": [],
            "blocked": False, "blocked_reason": "",
            "tasks": [{"task_id": route_811_id, "status": "In Review"}],
            "completion_projection": projection(retry_run),
        },
    ],
}

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


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
            raise RuntimeError(server.stdout.read() if server.stdout else "app exited")
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
        page.route("**/ixp/v1/open_prs?*", lambda route: route.fulfill(json=pr_payload))
        page.route("**/ixp/v1/deployments?*", lambda route: route.fulfill(
            json={"deployments": [], "undeployed_count": 0}))
        page.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        page.goto(base + "/?project=maxwell")
        page.locator('a[href="#tab-board"]:visible').first.click()
        page.wait_for_selector(f'#board [data-task="{route_810_id}"]')

        # Board and modal agree on remediation ownership and exact desired head.
        assert "remediation" in page.locator(
            f'#board [data-task="{route_810_id}"]').inner_text().lower()
        page.locator(f'#board [data-task="{route_810_id}"]').click()
        page.wait_for_selector("#task-modal.show")
        modal = page.locator("#task-modal-body").inner_text().lower()
        assert "required_ci_failed" in modal and "remediation agent" in modal
        assert "remediation @ 8888888" in modal
        page.evaluate("""() => {
          const modal = document.querySelector('#task-modal');
          modal.classList.remove('show');
          modal.style.display = 'none';
          modal.setAttribute('aria-hidden', 'true');
          document.querySelectorAll('.modal-backdrop').forEach((node) => node.remove());
        }""")

        # The remaining completion states are visible without conflating them
        # with process liveness.
        assert "remediation" in page.locator(
            f'#board [data-task="{route_admitted_id}"]').inner_text().lower()
        assert "review merge" in page.locator(
            f'#board [data-task="{route_queue_id}"]').inner_text().lower()
        page.evaluate("""() => {
          const hide = document.querySelector('#f-hidedone');
          if (hide) {
            hide.checked = false;
            hide.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }""")
        assert "provenance" in page.locator(
            f'#board [data-task="{route_done_id}"]').inner_text().lower()

        # PR #810 shows the red check and remediation owner despite Draft.
        page.wait_for_function(
            "() => (document.querySelector('#fleet-dock')?.textContent || '').includes('#810')")
        dock = page.locator("#fleet-dock").inner_text().lower()
        assert "#810" in dock and "vm gate failed" in dock
        assert "remediation owner" in dock and "draft" not in dock.split("#810")[0]
        assert "#811" in dock and "coordination retry" in dock

        # Deliverable-scoped work ledger consumes the same projection.
        page.goto(base + "/?project=maxwell&deliverable=ui62-browser#tab-mission")
        page.wait_for_selector(f'[data-mission-task-row="{route_810_id}"]')
        assert "remediation" in page.locator(
            f'[data-mission-task-row="{route_810_id}"]').inner_text().lower()
        assert "coordination retry" in page.locator(
            f'[data-mission-task-row="{route_811_id}"]').inner_text().lower()
        assert "review merge" in page.locator(
            f'[data-mission-task-row="{route_queue_id}"]').inner_text().lower()
        assert "provenance" in page.locator(
            f'[data-mission-task-row="{route_done_id}"]').inner_text().lower()

        # API task detail is the same projection rendered above.
        api = page.evaluate("""async () => await (await fetch(
          'api/tasks/%s?project=maxwell')).json()""" % route_811_id)
        assert api["completion_projection"]["route"] == "coordination_retry", api
        assert api["completion_projection"]["board_status"] == "In Review", api
        done_api = page.evaluate("""async () => await (await fetch(
          'api/tasks/%s?project=maxwell')).json()""" % route_done_id)
        assert done_api["completion_projection"]["terminal"] is True, done_api
        assert done_api["completion_projection"]["merged_sha"] == done_sha, done_api
        assert not console_errors, console_errors
        browser.close()
    print("PASS UI-62 board, modal, mission, API, and PR dock share completion route")
    print("PASS UI-62 draft red CI exposes remediation; green draft exposes coordination retry")
    print("PASS UI-62 admitted remediation, queue readiness, and canonical Done provenance render")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
