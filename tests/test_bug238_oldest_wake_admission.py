#!/usr/bin/env python3
"""BUG-238: the Agent Host pending-wake feed is FIFO, not starvation-prone LIFO."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug238-oldest-wake-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


PROJECT = "switchboard"

try:
    store.init_db(PROJECT)
    older = store.request_wake(
        {"runtime": "codex"},
        reason="older pending wake",
        source="bug238-test",
        project=PROJECT,
    )
    newer = store.request_wake(
        {"runtime": "codex"},
        reason="newer pending wake",
        source="bug238-test",
        project=PROJECT,
    )

    response = TestClient(app).get(
        "/txp/v1/list_wake_intents",
        params={"project": PROJECT, "status": "pending"},
    )
    wake_ids = [
        wake["wake_id"] for wake in response.json().get("wake_intents", [])
    ]

    assert response.status_code == 200, response.text
    assert wake_ids[:2] == [older["wake_id"], newer["wake_id"]], wake_ids
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("1 passed, 0 failed")
