#!/usr/bin/env python3
"""Required Chromium proof: browser cookie crosses into Deliverables :8124."""
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
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="arch-ms111-browser-cut-"))
project = "ms111-browser"
env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "required", "PM_JWT_SECRET": "ms111-browser-secret",
    "SWITCHBOARD_DELIVERABLES_READY_PROJECT": project,
    "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
})
os.environ.update({key: value for key, value in env.items() if key.startswith("PM_")})
(tmp / "projects").mkdir(parents=True)

import auth  # noqa: E402
import store  # noqa: E402
from switchboard.api.routers.auth import session as auth_session  # noqa: E402
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

store.init_project_registry()
store.create_project("MS111 browser", project_id=project, actor="test")
store.init_db(project)
store.create_deliverable({
    "id": "ms111-browser-deliverable", "title": "Browser boundary",
    "status": "proposed", "end_state": "Cookie authorizes on 8124",
    "acceptance_criteria": ["real Chromium receives the committed read"],
}, actor="test", project=project)
auth_store.init()
user = auth_store.create_user(
    "ms111-browser@example.test", "MS111 Browser",
    auth.password_hash("ms111-browser-password"),
)
store.grant_project_role(project, "user", user["id"], "viewer",
                         created_by="test", scopes=["read"])
cookie, _ = auth_session.issue(user)

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "--factory",
    "switchboard.services.deliverables.app:create_app",
    "--host", "127.0.0.1", "--port", str(port),
], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

try:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/ready", timeout=1) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    else:
        raise RuntimeError("Deliverables service did not become ready")

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([{
            "name": auth_session.COOKIE_NAME, "value": cookie,
            "url": base, "httpOnly": True, "sameSite": "Lax",
        }])
        page = context.new_page()
        page.goto(base + "/health")
        result = page.evaluate("""async ([project]) => {
          const response = await fetch(`/api/deliverables?project=${project}`);
          return {status: response.status, body: await response.json()};
        }""", [project])
        assert result["status"] == 200, result
        assert result["body"]["deliverables"][0]["id"] == "ms111-browser-deliverable"
        browser.close()
    print("PASS required Chromium cookie reaches Deliverables-owned read on :8124")
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
