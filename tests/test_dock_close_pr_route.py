#!/usr/bin/env python3
"""The close-PR route: closes an open PR, refuses one the merge queue owns.

Exercises the route itself rather than grepping it — the browser test cannot
reach this guard, because a queued PR renders in the merge-queue stack and never
goes through the PR card at all.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="dock-close-pr-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    import open_prs  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  close-PR route proof requires optional dependency: {exc.name}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


OPEN_PR = {"number": 900, "title": "Unwanted", "queue_position": 0, "tasks": []}
QUEUED_PR = {"number": 901, "title": "Merging now", "queue_position": 1, "tasks": []}
calls: list = []

client = TestClient(app)
real_build, real_cmd, real_token = (
    open_prs.build_open_prs, open_prs.gh_command, open_prs._token)
try:
    open_prs.build_open_prs = lambda *a, **k: {
        "repo": "acme/widgets", "prs": [OPEN_PR, QUEUED_PR]}
    open_prs._token = lambda *a, **k: "tok"
    open_prs.gh_command = lambda argv, **k: (
        calls.append(list(argv)) or {"returncode": 0, "stdout": "closed"})

    r = client.post("/api/pull-requests/900/close", json={"project": P})
    ok(r.status_code == 200, f"an open PR closes: {r.status_code} {r.text[:120]}")
    ok(r.json().get("status") == "closed", f"the route reports closed: {r.json()}")
    ok(calls and calls[-1][:3] == ["pr", "close", "900"],
       f"it runs GitHub's close, not a delete: {calls}")
    ok(not any("delete" in " ".join(c) for c in calls),
       f"nothing is deleted: {calls}")

    before = len(calls)
    r = client.post("/api/pull-requests/901/close", json={"project": P})
    ok(r.status_code == 409,
       f"a PR the merge queue owns is refused: {r.status_code} {r.text[:160]}")
    ok("merge queue" in r.text.lower(),
       f"the refusal says why: {r.text[:160]}")
    ok(len(calls) == before,
       f"and nothing was sent to GitHub for it: {calls[before:]}")

    r = client.post("/api/pull-requests/4242/close", json={"project": P})
    ok(r.status_code == 404, f"an unknown PR is a 404: {r.status_code}")
finally:
    open_prs.build_open_prs, open_prs.gh_command, open_prs._token = (
        real_build, real_cmd, real_token)
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nClose-PR route: {passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
