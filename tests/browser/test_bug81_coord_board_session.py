#!/usr/bin/env python3
"""BUG-81: CLI Playwright proof for browser auth across the Coord process cut.

Boots the real monolith (shell/login) and real Coord service on separate ports,
then uses Playwright request routing as the local edge: only /api/board is sent
to Coord, matching production Caddy.  A real Chromium login cookie must survive
that process boundary and the rendered app must report the seeded task.
"""
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

try:
    from playwright.sync_api import sync_playwright  # noqa: E402
except ImportError:
    print("SKIP playwright not installed — run: python -m playwright install chromium")
    raise SystemExit(0)


TMP = Path(tempfile.mkdtemp(prefix="bug81-coord-browser-"))
PROJECT = "bug81-browser"
EMAIL = "bug81-browser@example.test"
PASSWORD = "bug81-browser-password"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


APP_PORT = free_port()
COORD_PORT = free_port()
APP_URL = f"http://127.0.0.1:{APP_PORT}"
COORD_URL = f"http://127.0.0.1:{COORD_PORT}"

env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(TMP / "maxwell.db"),
    "PM_HELM_DB_PATH": str(TMP / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(TMP / "project_registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(TMP / "projects"),
    "PM_AUTH_MODE": "required",
    "PM_JWT_SECRET": "bug81-playwright-secret",
    "PM_COORD_HTTP_PRIMARY": "service",
    "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
})
env.pop("PM_AUTH_HTTP_PRIMARY", None)  # monolith owns login in this hermetic proof
os.environ.update({key: value for key, value in env.items() if key.startswith("PM_")})
os.environ.pop("PM_AUTH_HTTP_PRIMARY", None)
TMP.mkdir(parents=True, exist_ok=True)
(TMP / "projects").mkdir(parents=True, exist_ok=True)

import auth  # noqa: E402
import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

store.init_project_registry()
store.create_project("BUG-81 browser board", project_id=PROJECT, actor="test")
for project_id in store.project_ids():
    store.init_db(project_id)
store.create_task(
    {"workstream_id": "BUG", "title": "Browser session reaches Coord"},
    actor="test", project=PROJECT,
)
auth_store.init()
user = auth_store.create_user(
    EMAIL, "BUG-81 Browser", auth.password_hash(PASSWORD),
)
store.grant_project_role(
    PROJECT, "user", user["id"], "viewer", created_by="test", scopes=["read"],
)


def start(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", *args], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


app_server = start("app:app", "--host", "127.0.0.1", "--port", str(APP_PORT))
coord_server = start(
    "--factory", "switchboard.services.coord.app:create_app",
    "--host", "127.0.0.1", "--port", str(COORD_PORT),
)


def wait_healthy(url: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"server exited before healthy: {output[-2000:]}")
        time.sleep(0.2)
    raise TimeoutError(f"server did not become healthy: {url}")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


try:
    wait_healthy(APP_URL, app_server)
    wait_healthy(COORD_URL, coord_server)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        board_statuses: list[int] = []

        def route_board(route) -> None:
            parsed = urllib.parse.urlsplit(route.request.url)
            target = urllib.parse.urlunsplit((
                "http", f"127.0.0.1:{COORD_PORT}", parsed.path, parsed.query, "",
            ))
            response = route.fetch(url=target)
            board_statuses.append(response.status)
            route.fulfill(response=response)

        page.route("**/api/board?*", route_board)
        return_to = urllib.parse.quote(f"/?project={PROJECT}", safe="")
        page.goto(f"{APP_URL}/login?return_to={return_to}")
        page.get_by_label("Email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(f"{APP_URL}/?project={PROJECT}")
        page.wait_for_function(
            "document.querySelector('#data-status')?.textContent?.includes('tasks')",
            timeout=20_000,
        )

        status_text = page.locator("#data-status").inner_text()
        error_visible = page.get_by_role(
            "heading", name="Could not load the project plan",
        ).is_visible()
        task_count = page.evaluate("TeepPlan.tasks.length")

        assert board_statuses and board_statuses[-1] == 200, board_statuses
        assert status_text == "1 tasks", status_text
        assert not error_visible
        assert task_count == 1, task_count
        print("PASS Playwright login cookie authorized by Coord-owned /api/board (HTTP 200)")
        print("PASS rendered app reports 1 task and no board-load error")
        browser.close()
finally:
    stop(coord_server)
    stop(app_server)
    shutil.rmtree(TMP, ignore_errors=True)
