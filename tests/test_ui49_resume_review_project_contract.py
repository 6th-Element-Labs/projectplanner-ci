#!/usr/bin/env python3
"""UI-49: Resume review accepts both deployed project-routing contracts."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="ui49-resume-project-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def fake_resume(task_id, actor, project, principal_id):
    return {"resumed": True, "task_id": task_id, "project": project,
            "wake_id": "wake-ui49"}


try:
    client = TestClient(app)
    with patch("dispatch.resume_review", side_effect=fake_resume) as resume:
        cached = client.post(
            "/api/tasks/ARCH-MS-121/resume-review?project=switchboard")
        ok(cached.status_code == 200
           and cached.json().get("project") == "switchboard",
           "a cached query-only client routes ARCH-MS-121 to Switchboard")

        current = client.post(
            "/api/tasks/ARCH-MS-121/resume-review",
            json={"project": "switchboard"})
        ok(current.status_code == 200
           and current.json().get("project") == "switchboard",
           "the current typed-body client routes ARCH-MS-121 to Switchboard")

        dual = client.post(
            "/api/tasks/ARCH-MS-121/resume-review?project=switchboard",
            json={"project": "switchboard"})
        ok(dual.status_code == 200 and resume.call_count == 3,
           "the deployed dual-form request starts exactly one backend operation")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-49 resume project contract: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
