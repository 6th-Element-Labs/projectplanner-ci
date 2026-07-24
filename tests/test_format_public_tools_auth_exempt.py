#!/usr/bin/env python3
"""format.taikunai.com self-serve tools must stay anonymously callable.

The landing page at format.taikunai.com is a public drop-a-file surface
(deck rebrand + PDF OCR). Production runs PM_AUTH_MODE=required, so these
POST endpoints must be on the auth exempt list — otherwise uploads 401
before the handler runs and the vanity URL looks "down".
"""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401 -- installs repo and src on sys.path

os.environ["PM_AUTH_MODE"] = "required"
os.environ.setdefault("PM_JWT_SECRET", "format-public-test-secret")

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from switchboard.api.middleware import register_auth_gate


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


app = FastAPI()


@app.post("/api/rebrand")
async def rebrand(file: UploadFile = File(...)):
    return {"ok": True, "tool": "rebrand", "name": file.filename}


@app.post("/api/ocr")
async def ocr(file: UploadFile = File(...)):
    return {"ok": True, "tool": "ocr", "name": file.filename}


@app.post("/api/tasks")
async def tasks():
    return {"ok": True, "leaked": True}


@app.get("/health")
def health():
    return {"ok": True}


register_auth_gate(
    app,
    global_user_scopes=lambda _user, _project: ["read"],
    global_principal=lambda user, scopes: {"user": user, "scopes": scopes},
    admin_scopes=["admin"],
)

client = TestClient(app)

for path, filename in (("/api/rebrand", "deck.pptx"), ("/api/ocr", "doc.pdf")):
    response = client.post(path, files={"file": (filename, b"x", "application/octet-stream")})
    ok(response.status_code == 200,
       f"anonymous POST {path} is allowed (got {response.status_code})")
    ok(response.json().get("ok") is True,
       f"anonymous POST {path} reaches the handler")

blocked = client.post("/api/tasks", json={"title": "nope"})
ok(blocked.status_code == 401,
   f"anonymous POST /api/tasks still 401 (got {blocked.status_code})")
ok("leaked" not in blocked.text,
   "anonymous POST /api/tasks does not leak payload")

health = client.get("/health")
ok(health.status_code == 200, "/health remains anonymously readable")

os.environ["PM_AUTH_MODE"] = "dev-open"

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
