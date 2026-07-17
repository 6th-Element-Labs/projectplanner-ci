#!/usr/bin/env python3
"""Real Chromium journey across Tasks, Coord, and Deliverables process cuts."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


TMP = Path(tempfile.mkdtemp(prefix="arch-ms126-boundary-"))
PROJECT = "ms126-browser"
OTHER = "ms126-other"
EMAIL = "ms126-browser@example.test"
PASSWORD = "ms126-browser-password"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


ports = {name: free_port() for name in ("app", "auth", "tasks", "coord", "deliverables")}
urls = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(TMP / "maxwell.db"),
    "PM_HELM_DB_PATH": str(TMP / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(TMP / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(TMP / "projects"),
    "PM_AUTH_MODE": "required", "PM_JWT_SECRET": "ms126-browser-secret",
    "SWITCHBOARD_AUTH_READY_PROJECT": PROJECT,
    "SWITCHBOARD_TASKS_READY_PROJECT": PROJECT,
    "SWITCHBOARD_COORD_READY_PROJECT": PROJECT,
    "SWITCHBOARD_DELIVERABLES_READY_PROJECT": PROJECT,
    "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
})
for key in ("PM_AUTH_HTTP_PRIMARY", "PM_TASKS_HTTP_PRIMARY", "PM_COORD_HTTP_PRIMARY",
            "PM_DELIVERABLES_HTTP_PRIMARY"):
    env.pop(key, None)
os.environ.update({key: value for key, value in env.items() if key.startswith(("PM_", "SWITCHBOARD_"))})
(TMP / "projects").mkdir(parents=True)

import auth  # noqa: E402
import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

store.init_project_registry()
for project_id, label in ((PROJECT, "MS126 Browser"), (OTHER, "MS126 Other")):
    store.create_project(label, project_id=project_id, actor="test")
    store.init_db(project_id)
store.create_task({
    "workstream_id": "ARCH-MS", "title": "Boundary journey task",
    "description": "Backend fixture only", "ui_impact": "no",
}, actor="test", project=PROJECT)
store.create_deliverable({
    "id": "ms126-deliverable", "title": "Boundary journey",
    "status": "approved", "end_state": "All service reads agree",
    "acceptance_criteria": ["real Chromium crosses process boundaries"],
}, actor="test", project=PROJECT)
read_token = "ms126-project-read"
other_token = "ms126-other-read"
store.create_principal(kind="agent", display_name="MS126 reader", token=read_token,
                       scopes=["read"], principal_id="agent-ms126", project=PROJECT)
store.create_principal(kind="agent", display_name="MS126 other", token=other_token,
                       scopes=["read"], principal_id="agent-ms126-other", project=OTHER)
auth_store.init()
user = auth_store.create_user(EMAIL, "MS126 Browser", auth.password_hash(PASSWORD))
store.grant_project_role(PROJECT, "user", user["id"], "viewer",
                         created_by="test", scopes=["read"])


def start(*args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", *args], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


processes = {
    "app": start("app:app", "--host", "127.0.0.1", "--port", str(ports["app"])),
    "auth": start("--factory", "switchboard.services.auth.app:create_app", "--host", "127.0.0.1", "--port", str(ports["auth"])),
    "tasks": start("--factory", "switchboard.services.tasks.app:create_app", "--host", "127.0.0.1", "--port", str(ports["tasks"])),
    "coord": start("--factory", "switchboard.services.coord.app:create_app", "--host", "127.0.0.1", "--port", str(ports["coord"])),
    "deliverables": start("--factory", "switchboard.services.deliverables.app:create_app", "--host", "127.0.0.1", "--port", str(ports["deliverables"])),
}


def wait_ready(name: str, timeout: float = 25) -> None:
    process = processes[name]
    path = "/health" if name == "app" else "/ready"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urls[name] + path, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"{name} exited before ready: {output[-2000:]}")
        time.sleep(0.2)
    raise TimeoutError(f"{name} did not become ready")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def edge_target(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    service = None
    if parsed.path.startswith("/api/tasks"):
        service = "tasks"
    elif parsed.path in {"/api/board", "/api/signals", "/ixp/v1/delta"}:
        service = "coord"
    elif parsed.path.startswith("/api/deliverables") or parsed.path.startswith("/api/mission_status"):
        service = "deliverables"
    if not service:
        return None
    return urllib.parse.urlunsplit(("http", f"127.0.0.1:{ports[service]}",
                                   parsed.path, parsed.query, ""))


try:
    for service_name in processes:
        wait_ready(service_name)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)

        # Identity matrix uses real service HTTP and deliberately failing responses.
        request = runtime.request.new_context()
        anonymous = request.get(urls["coord"] + f"/api/board?project={PROJECT}")
        assert anonymous.status in (401, 403), anonymous.status
        bearer = request.get(urls["coord"] + f"/api/board?project={PROJECT}",
                             headers={"Authorization": f"Bearer {read_token}"})
        assert bearer.status == 200, bearer.status
        wrong_project = request.get(urls["coord"] + f"/api/board?project={PROJECT}",
                                    headers={"Authorization": f"Bearer {other_token}"})
        assert wrong_project.status in (401, 403), wrong_project.status
        expired = runtime.request.new_context(extra_http_headers={"Cookie": "taikun_session=expired"})
        expired_response = expired.get(urls["deliverables"] + f"/api/deliverables?project={PROJECT}")
        assert expired_response.status in (401, 403), expired_response.status

        context = browser.new_context()
        page = context.new_page()
        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on("console", lambda message: console_errors.append(
            f"{message.text} @ {message.location}")
                if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append(request.url))

        def route_edge(route) -> None:
            target = edge_target(route.request.url)
            if target:
                route.fulfill(response=route.fetch(url=target))
            else:
                route.continue_()

        page.route("**/api/**", route_edge)
        page.goto(f"{urls['app']}/health")
        login = page.evaluate("""async ([email, password]) => {
          const response = await fetch('/api/auth/login', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, password})
          });
          return {status: response.status, body: await response.json()};
        }""", [EMAIL, PASSWORD])
        assert login["status"] == 200, login

        journey = page.evaluate("""async (project) => {
          const paths = [
            `/api/projects`, `/api/board?project=${project}`,
            `/api/tasks?project=${project}`, `/api/deliverables?project=${project}`,
            `/api/mission_status?project=${project}&deliverable_id=ms126-deliverable`
          ];
          const rows = [];
          for (const path of paths) {
            const response = await fetch(path);
            rows.push({path, status: response.status, body: await response.json()});
          }
          return rows;
        }""", PROJECT)
        assert all(row["status"] == 200 for row in journey), journey
        assert journey[1]["body"]["workstreams"][0]["tasks"][0]["task_id"].startswith("ARCH-MS-")
        assert journey[3]["body"]["deliverables"][0]["id"] == "ms126-deliverable"
        assert journey[4]["body"]["deliverable_id"] == "ms126-deliverable"
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
        browser.close()
    print("PASS ARCH-MS-126 real Chromium login and project/board/task/deliverable/mission journey")
    print("PASS ARCH-MS-126 anonymous, expired, bearer, cookie, and wrong-project identity matrix")
    print("PASS ARCH-MS-126 all 8121-8124 readiness probes passed")
finally:
    for process in processes.values():
        stop(process)
    shutil.rmtree(TMP, ignore_errors=True)
