#!/usr/bin/env python3
"""UI-41: real Chromium proof for authoritative Needs-you handoff."""
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


tmp = Path(tempfile.mkdtemp(prefix="ui41-browser-"))
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
from switchboard.storage.repositories.attention import (  # noqa: E402
    default_attention_repository,
)
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories.provenance import _upsert_git_state  # noqa: E402

store.init_project_registry()
store.init_db("switchboard")
task = store.create_task(
    {"workstream_id": "COORD", "title": "UI-41 exact-head browser fixture"},
    actor="ui41-browser", project="switchboard")
task_id = task["task_id"]
with _conn("switchboard") as connection:
    _upsert_git_state(connection, task_id, {"head_sha": "c" * 40})
created = default_attention_repository.create_request({
    "task_id": task_id,
    "provider": "switchboard.completion",
    "provider_request_id": "completion-run-812:7",
    "schema_version": "switchboard.completion_human_closeout.v1",
    "prompt": "Supply an authenticated credential so live proof can finish.",
    "choices": [
        {"id": "supply_credential", "label": "Supply credential",
         "description": "Recommended: resume the exact live-proof step."},
        {"id": "accept_without_live_proof", "label": "Accept current proof"},
    ],
    "recommended_default": {"id": "supply_credential"},
    "idempotency_key": "completion-human:completion-run-812:7",
    "host_id": "host/ui41",
    "runner_session_id": "run-ui41",
    "context": {
        "task_id": task_id, "deliverable_id": "alerts",
        "completion_run_id": "completion-run-812", "state_version": 7,
        "pr_number": 812, "head_sha": "c" * 40,
        "completed_work_summary": "Implementation and review are complete at PR #812.",
        "evidence_refs": {"pr": "#812", "tests": "green"},
        "reason_code": "credentialed_live_proof_unavailable",
        "why_automation_stopped": "No eligible authenticated credential was available.",
        "what_you_need_to_do": "Supply a credential or explicitly accept current proof.",
        "resume_condition": "An authorized versioned decision is recorded.",
        "next_automatic_action": "The completion owner reruns live proof.",
        "delivery_impact": "blocking", "blast_radius": {"tasks": 1},
    },
}, actor="ui41-browser", project="switchboard")
request_id = created["request"]["request_id"]

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)


def wait_ready(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise TimeoutError("app did not become ready")


def post(path, body):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("console", lambda message: errors.append(message.text)
                if message.type == "error" else None)
        page.goto(base + "/?project=switchboard", wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('#ack-inbox-count').textContent === '1'")
        page.locator("#btn-ack-inbox").click()
        page.wait_for_selector("#tab-needs.active")
        page.wait_for_selector("text=Implementation complete, human action required")
        assert page.locator('[data-nid^="provider:"]').count() == 1
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('#ack-inbox-count').textContent === '1'")
        page.locator("#btn-ack-inbox").click()
        page.wait_for_selector("text=Why automation stopped")
        assert page.locator('[data-nid^="provider:"]').count() == 1
        page.screenshot(path="/tmp/ui41-needs-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/ui41-needs-narrow.png", full_page=True)

        page.locator('[data-choice="supply_credential"]').click()
        page.wait_for_selector("text=Resuming")
        claimed = post("/ixp/v1/attention/decisions/claim", {
            "project": "switchboard", "host_id": "host/ui41",
            "provider": "switchboard.completion", "request_id": request_id,
            "runner_session_id": "run-ui41",
        })
        assert claimed["claimed"]
        version = claimed["delivery"]["request"]["version"]
        post(f"/ixp/v1/attention/requests/{request_id}/delivery", {
            "project": "switchboard", "host_id": "host/ui41",
            "provider": "switchboard.completion",
            "runner_session_id": "run-ui41",
            "expected_version": version,
            "receipt": {"execution_id": "exec-ui41", "provider_ack": "verified"},
        })
        page.wait_for_selector("text=Resumed — provider receipt verified.")
        assert not errors, errors
        browser.close()
    print("PASS UI-41 #812 handoff appears once and survives reload")
    print("PASS UI-41 versioned decide shows Resuming, then receipt-gated Resumed")
    print("PASS UI-41 desktop and narrow screenshots captured")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
