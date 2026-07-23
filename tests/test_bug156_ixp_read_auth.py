#!/usr/bin/env python3
"""BUG-156: operator protocol reads must never be anonymously readable."""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401 -- installs repo and src on sys.path

os.environ["PM_AUTH_MODE"] = "required"
os.environ.setdefault("PM_JWT_SECRET", "bug156-test-secret")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import store
from switchboard.api.middleware import register_auth_gate


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


app = FastAPI()


@app.get("/ixp/v1/open_prs")
def open_prs(project: str):
    return {"project": project, "prs": [{"title": "private"}]}


@app.get("/ixp/v1/working_agreement")
def working_agreement(project: str):
    return {"project": project, "agreement": "private"}


@app.get("/tally/v1/outcomes")
def tally_outcomes(project: str):
    return {"project": project, "outcomes": ["private"]}


@app.get("/health")
def health():
    return {"ok": True}


register_auth_gate(
    app,
    global_user_scopes=lambda _user, _project: ["read"],
    global_principal=lambda user, scopes: {"user": user, "scopes": scopes},
    admin_scopes=["admin"],
)

# Keep the fixture independent of the project registry. Anonymous requests must
# be rejected before project existence or route execution is consulted.
original_has_project = store.has_project
store.has_project = lambda _project: True
try:
    client = TestClient(app)
    for path in (
        "/ixp/v1/open_prs",
        "/ixp/v1/working_agreement",
        "/tally/v1/outcomes",
    ):
        response = client.get(path, params={"project": "switchboard"})
        ok(response.status_code == 401,
           f"anonymous GET {path} is denied with 401 (got {response.status_code})")
        ok("private" not in response.text,
           f"anonymous GET {path} does not leak its payload")

    response = client.get("/health")
    ok(response.status_code == 200, "/health remains anonymously readable")
finally:
    store.has_project = original_has_project
    os.environ["PM_AUTH_MODE"] = "dev-open"

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
