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
from switchboard.storage.repositories import (  # noqa: E402
    attention as attention_repository,
    autopilot_scopes,
    completion_runs,
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
    _upsert_git_state(connection, task_id, {
        "head_sha": "c" * 40,
        "pr_number": 812,
        "pr_url": "https://github.com/example/projectplanner/pull/812",
    })
completion_run = completion_runs.transition_completion_run({
    "task_id": task_id,
    "pr_number": 812,
    "head_sha": "c" * 40,
    "state": "blocked",
    "route": "human",
    "reason_code": "credentialed_live_proof_unavailable",
    "desired_role": "",
    "board_status": "Blocked",
    "evidence_refs": {"human": {"status": "required"}},
}, actor="ui41-browser", project="switchboard")
created = default_attention_repository.create_request({
    "task_id": task_id,
    "provider": "switchboard.completion",
    "provider_request_id": (
        f"{completion_run['run_id']}:{completion_run['state_version']}"
    ),
    "schema_version": "switchboard.completion_human_closeout.v1",
    "prompt": "Supply an authenticated credential so live proof can finish.",
    "choices": [
        {"id": "supply_credential", "label": "Supply credential",
         "description": "Recommended: resume the exact live-proof step.",
         "effect": "resume_assessment"},
        {"id": "accept_without_live_proof", "label": "Accept current proof",
         "effect": "remain_blocked"},
    ],
    "recommended_default": {"id": "supply_credential"},
    "idempotency_key": (
        f"completion-human:{completion_run['run_id']}:"
        f"{completion_run['state_version']}"
    ),
    "host_id": "host/ui41",
    "runner_session_id": "run-ui41",
    "context": {
        "schema": "switchboard.completion_human_closeout.v1",
        "task_id": task_id, "deliverable_id": "",
        "completion_run_id": completion_run["run_id"],
        "state_version": completion_run["state_version"],
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


def wait_completion_wake(status, timeout=10):
    deadline = time.time() + timeout
    wake = {}
    request = {}
    while time.time() < deadline:
        request = default_attention_repository.get_request(
            request_id, project="switchboard")
        wake = request.get("completion_wake") or {}
        if wake.get("status") == status:
            return wake
        time.sleep(0.1)
    raise AssertionError(
        f"completion wake did not reach {status}: request={request}, wake={wake}")


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

        with page.expect_response(
            lambda response: f"/api/attention/requests/{request_id}/decide"
            in response.url
        ) as response_info:
            page.locator('[data-choice="supply_credential"]').click()
        decision_response = response_info.value
        assert decision_response.ok, decision_response.text()
        page.wait_for_selector("#needs-flash")
        # The reserved completion provider cannot use the generic Agent Host
        # claim/delivery endpoints. The API callback must create and accept a
        # fenced Autopilot wake, while the UI remains Resuming until the
        # completion owner records an exact tick receipt.
        wake = wait_completion_wake("failed")
        assert page.locator("text=Resumed — provider receipt verified.").count() == 0
        now = time.time()
        with _conn("switchboard") as connection:
            connection.execute(
                "INSERT OR REPLACE INTO agent_presence("
                "agent_id,runtime,lane,task_id,registered_at,heartbeat_at,"
                "ttl_s,control) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "ui41-completion-owner", "codex", "COORD", task_id,
                    now, now, 120, "{}",
                ),
            )

        # The API process cannot impersonate the scoped completion daemon. Its
        # first wake safely remains durable when no owner lease exists. Model
        # the daemon's restart drain: reuse the created task scope, acquire the
        # sole fenced lease, and accept the same wake without a second decision.
        authority_box = {}

        def wake_owner(payload):
            scope = autopilot_scopes.start_autopilot_scope(
                project="switchboard",
                scope_type="task",
                deliverable_id=str(payload.get("deliverable_id") or ""),
                task_project="switchboard",
                task_id=task_id,
                runtime="codex",
                actor="ui41-completion-owner",
            )
            authority = autopilot_scopes.acquire_autopilot_scope_lease(
                scope["scope_id"],
                holder_agent_id="ui41-completion-owner",
                project="switchboard",
                ttl_seconds=120,
            )
            authority_box.update(authority)
            return authority

        wake = attention_repository.attempt_completion_wake(
            request_id,
            wake_completion_owner=wake_owner,
            actor="ui41-completion-owner",
            project="switchboard",
            now=time.time() + 6,
        )
        assert wake["status"] == "accepted", wake
        authority = dict(authority_box)
        assert not authority.get("error"), authority
        reassessed = completion_runs.transition_completion_run({
            "task_id": task_id,
            "pr_number": 812,
            "head_sha": "c" * 40,
            "state": "waiting",
            "route": "review_merge",
            "reason_code": "human_decision_recorded",
            "desired_role": "review_merge",
            "board_status": "In Review",
            "evidence_refs": {"human_decision": {"status": "recorded"}},
        }, actor="ui41-completion-owner", project="switchboard")
        tick = {
            "schema": "switchboard.completion_tick.v1",
            "task_id": task_id,
            "snapshot": {
                "schema": "switchboard.completion_snapshot.v1",
                "task_id": task_id, "head_sha": "c" * 40, "pr_number": 812,
            },
            "decision": {
                "schema": "switchboard.completion_decision.v1",
                "route": "review_merge",
            },
            "plan": {
                "schema": "switchboard.completion_effect.v1",
                "task_id": task_id, "head_sha": "c" * 40, "pr_number": 812,
                "route": "review_merge", "effect": "ensure_review_generation",
                "idem_key": "ui41-resume-assessment",
            },
            "execution": {
                "run": reassessed,
                "receipt": {
                    "schema": "switchboard.completion_effect_receipt.v1",
                    "effect": "ensure_review_generation",
                    "idem_key": "ui41-resume-assessment",
                    "verified": True,
                    "pending": False,
                },
            },
        }
        receipt = attention_repository.complete_completion_wake_for_tick(
            task_id,
            tick=tick,
            scope_authority=authority,
            actor="ui41-completion-owner",
            project="switchboard",
        )
        assert receipt["status"] == "resolved", receipt
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
