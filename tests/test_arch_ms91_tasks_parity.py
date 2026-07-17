#!/usr/bin/env python3
"""ARCH-MS-91: Tasks side-by-side :8122 + hermetic parity (pre-Caddy).

Compares Mode A day-one surfaces on:
  (1) in-process monolith-style routers (fat ``thin_mode_a=False`` baseline)
  (2) ``switchboard.services.tasks.create_app`` (Mode A thin cut on :8122)

Parity is status-code equality for CRUD + TXP claims. Sibling BC routes
(dispatch/chat/review) must stay mounted only on the baseline. No live Caddy
traffic / production systemd unit (Path B waive still holds for live cut).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms91-tasks-parity-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms91"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

PROJECT = "ms91-alpha"
passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _make_baseline_client() -> TestClient:
    """In-process fat Tasks + claims routers (monolith composition shape).

    BUG-69: the monolith's actual protection for these routes is the global auth
    gate app_impl.py registers via register_middleware — list_tasks/get_task below
    call resolve_principal nowhere; only the write handlers do. A baseline that
    omits register_auth_gate is not actually representative of app_impl.py, so an
    anonymous-read "parity" check against it would trivially pass even with the
    gate missing from BOTH sides. Registering it here is a no-op for every existing
    assertion in this file (they all run under PM_AUTH_MODE=dev-open with no bearer
    token, which the gate short-circuits) and makes the fixture answer the question
    the file's docstring claims to answer.
    """
    from switchboard.api import deps
    from switchboard.api.middleware import register_auth_gate
    from switchboard.api.routers import claims as claims_router
    from switchboard.api.routers import tasks as tasks_router
    from switchboard.api.tasks_port_adapters import (
        configure_tasks_ports,
        ensure_tasks_runtime,
    )

    configure_tasks_ports()
    ensure_tasks_runtime()
    app = FastAPI(title="arch-ms91-baseline")
    register_auth_gate(
        app,
        global_user_scopes=deps.global_user_scopes,
        global_principal=deps.global_principal,
        admin_scopes=deps.ADMIN_SCOPES,
    )
    app.include_router(
        tasks_router.create_router(
            resolve_project=deps.resolve_project,
            resolve_principal=deps.resolve_principal,
            thin_mode_a=False,
        )
    )
    app.include_router(
        claims_router.create_router(
            resolve_project=deps.resolve_project,
            resolve_principal=deps.resolve_principal,
            resolve_body_project=deps.resolve_body_project,
        )
    )
    return TestClient(app)


def _make_cut_client() -> TestClient:
    from switchboard.services.tasks import create_app
    from switchboard.services.tasks.settings import TasksServiceSettings

    return TestClient(create_app(TasksServiceSettings(
        service_name="arch-ms91-test",
        host="127.0.0.1",
        port=8122,
    )))


def _parity(name: str, baseline_resp, cut_resp, *, expect_status: int | None = None) -> None:
    ok(
        baseline_resp.status_code == cut_resp.status_code,
        f"{name} status parity baseline={baseline_resp.status_code} cut={cut_resp.status_code}",
    )
    if expect_status is not None:
        ok(
            baseline_resp.status_code == expect_status,
            f"{name} expected status {expect_status} (got {baseline_resp.status_code})",
        )


import store  # noqa: E402

store.init_project_registry()
store.create_project("MS91 Alpha", project_id=PROJECT, actor="test")
store.init_db(PROJECT)

baseline = _make_baseline_client()
cut = _make_cut_client()

# --- cut health + side-by-side port contract ---------------------------------
health = cut.get("/health")
ok(health.status_code == 200, f"cut /health status {health.status_code}")
ok(health.json().get("status") == "ok", "cut /health status=ok")
ok(health.json().get("service") == "arch-ms91-test", "cut /health service name")
ok(
    baseline.get("/health").status_code == 404,
    "baseline has no /health (Tasks health lives on cut only)",
)

# --- Mode A read/list parity -------------------------------------------------
_parity(
    "list_tasks",
    baseline.get("/api/tasks", params={"project": PROJECT}),
    cut.get("/api/tasks", params={"project": PROJECT}),
    expect_status=200,
)

# --- create → get → patch parity (shared project DB) -------------------------
b_create = baseline.post(
    "/api/tasks",
    params={"project": PROJECT},
    json={"workstream_id": "ARCH-MS", "title": "ms91 baseline create"},
)
c_create = cut.post(
    "/api/tasks",
    params={"project": PROJECT},
    json={"workstream_id": "ARCH-MS", "title": "ms91 cut create"},
)
_parity("create_task", b_create, c_create, expect_status=200)
b_id = (b_create.json() or {}).get("task_id") or ""
c_id = (c_create.json() or {}).get("task_id") or ""
ok(bool(b_id and c_id), f"create returns task_ids baseline={b_id!r} cut={c_id!r}")

if b_id and c_id:
    _parity(
        "get_task",
        baseline.get(f"/api/tasks/{b_id}", params={"project": PROJECT}),
        cut.get(f"/api/tasks/{c_id}", params={"project": PROJECT}),
        expect_status=200,
    )
    _parity(
        "patch_task",
        baseline.patch(
            f"/api/tasks/{b_id}",
            params={"project": PROJECT},
            json={"title": "ms91 baseline patched"},
        ),
        cut.patch(
            f"/api/tasks/{c_id}",
            params={"project": PROJECT},
            json={"title": "ms91 cut patched"},
        ),
        expect_status=200,
    )
    _parity(
        "comment",
        baseline.post(
            f"/api/tasks/{b_id}/comment",
            params={"project": PROJECT},
            json={"text": "ms91 baseline comment"},
        ),
        cut.post(
            f"/api/tasks/{c_id}/comment",
            params={"project": PROJECT},
            json={"text": "ms91 cut comment"},
        ),
        expect_status=200,
    )

# --- TXP claim surface mounted on both ---------------------------------------
b_claim = baseline.post("/txp/v1/claim_next", json={
    "agent_id": "arch-ms91-baseline", "project": PROJECT,
})
c_claim = cut.post("/txp/v1/claim_next", json={
    "agent_id": "arch-ms91-cut", "project": PROJECT,
})
ok(b_claim.status_code != 404, f"baseline claim_next mounted ({b_claim.status_code})")
ok(c_claim.status_code != 404, f"cut claim_next mounted ({c_claim.status_code})")
_parity("claim_next", b_claim, c_claim)

# Same for claim_task shape (404 task is fine; mount matters)
missing = "MS91-MISSING-TASK"
b_ct = baseline.post("/txp/v1/claim_task", json={
    "task_id": missing, "agent_id": "arch-ms91-baseline", "project": PROJECT,
})
c_ct = cut.post("/txp/v1/claim_task", json={
    "task_id": missing, "agent_id": "arch-ms91-cut", "project": PROJECT,
})
_parity("claim_task", b_ct, c_ct)

# --- Mode A thin lock: sibling BC only on baseline ---------------------------
task_for_sibling = c_id or b_id or "x"
dispatch_b = baseline.post(
    f"/api/tasks/{task_for_sibling}/dispatch",
    json={"project": PROJECT},
)
dispatch_c = cut.post(
    f"/api/tasks/{task_for_sibling}/dispatch",
    json={"project": PROJECT},
)
ok(dispatch_b.status_code != 404,
   f"baseline keeps dispatch (got {dispatch_b.status_code})")
ok(dispatch_c.status_code == 404,
   f"cut omits dispatch Mode A (got {dispatch_c.status_code})")

chat_c = cut.post(
    f"/api/tasks/{task_for_sibling}/chat",
    params={"project": PROJECT},
    json={"message": "hello"},
)
ok(chat_c.status_code == 404, f"cut omits chat Mode A (got {chat_c.status_code})")

review_c = cut.get(
    f"/api/tasks/{task_for_sibling}/review_verdict",
    params={"project": PROJECT},
)
ok(review_c.status_code == 404, f"cut omits review Mode A (got {review_c.status_code})")

# --- 401 write parity under required auth (fresh clients) --------------------
os.environ["PM_AUTH_MODE"] = "required"
# Re-import auth helpers pick up env; clients use authenticate_request each call.
b_req = _make_baseline_client()
c_req = _make_cut_client()
b_unauth = b_req.post(
    "/api/tasks",
    params={"project": PROJECT},
    json={"workstream_id": "ARCH-MS", "title": "ms91 unauth"},
)
c_unauth = c_req.post(
    "/api/tasks",
    params={"project": PROJECT},
    json={"workstream_id": "ARCH-MS", "title": "ms91 unauth"},
)
ok(b_unauth.status_code == 401, f"baseline unauth create is 401 (got {b_unauth.status_code})")
ok(c_unauth.status_code == 401, f"cut unauth create is 401 (got {c_unauth.status_code})")
_parity("unauth_create_401", b_unauth, c_unauth, expect_status=401)
ok(b_unauth.status_code != 403 and c_unauth.status_code != 403,
   "unauth create never 403 (401 parity family)")

# --- BUG-69: 401 READ parity under required auth (the gap that let it ship) --
# list_tasks/get_task never call resolve_principal (see tasks.py) -- they rely
# entirely on the global auth gate registered outside the router. Before this
# fix, switchboard.services.tasks.app::create_app never registered it, so an
# anonymous GET here returned 200 with live task data on prod, twice
# (2026-07-15, 2026-07-17: anon GET /api/tasks?project=switchboard -> 200).
# Only testing writes (above) could not catch this -- reads are a materially
# different code path. b_req/c_req already carry PM_AUTH_MODE=required from
# the write-parity block above.
b_list_unauth = b_req.get("/api/tasks", params={"project": PROJECT})
c_list_unauth = c_req.get("/api/tasks", params={"project": PROJECT})
ok(b_list_unauth.status_code == 401, f"baseline unauth list is 401 (got {b_list_unauth.status_code})")
ok(c_list_unauth.status_code == 401, f"cut unauth list is 401 (got {c_list_unauth.status_code})")
_parity("unauth_list_401", b_list_unauth, c_list_unauth, expect_status=401)

if b_id and c_id:
    b_get_unauth = b_req.get(f"/api/tasks/{b_id}", params={"project": PROJECT})
    c_get_unauth = c_req.get(f"/api/tasks/{c_id}", params={"project": PROJECT})
    ok(b_get_unauth.status_code == 401, f"baseline unauth get_task is 401 (got {b_get_unauth.status_code})")
    ok(c_get_unauth.status_code == 401, f"cut unauth get_task is 401 (got {c_get_unauth.status_code})")
    _parity("unauth_get_task_401", b_get_unauth, c_get_unauth, expect_status=401)

ok(b_list_unauth.status_code != 403 and c_list_unauth.status_code != 403,
   "unauth read never 403 (401 parity family, matches the write check above)")

# /health must stay open on the cut even under required auth -- it is the
# liveness probe the deploy runbook curls before ever touching Caddy.
health_req = c_req.get("/health")
ok(health_req.status_code == 200, f"cut /health stays open under required auth (got {health_req.status_code})")

# Restore open mode for any further local reuse
os.environ["PM_AUTH_MODE"] = "dev-open"

# --- side-by-side deploy surface; no live traffic ----------------------------
unit = ROOT / "deploy" / "tasks" / "switchboard-tasks.service.example"
unit_live = ROOT / "deploy" / "switchboard-tasks.service"
frag = ROOT / "deploy" / "skeleton" / "Caddyfile.tasks-fragment.example"
caddy = ROOT / "deploy" / "Caddyfile"

ok(unit.is_file(), "side-by-side systemd example exists")
unit_text = unit.read_text(encoding="utf-8")
ok("switchboard.services.tasks.app:create_app" in unit_text,
   "systemd example points at Tasks create_app")
ok("8122" in unit_text, "systemd example uses :8122")
ok(any(
    line.startswith("ExecStart=") and "create_app" in line and not line.strip().startswith("#")
    for line in unit_text.splitlines()
), "active ExecStart for side-by-side factory")

ok(unit_live.is_file(), "production deploy/switchboard-tasks.service present (ARCH-MS-92)")
ok(frag.is_file(), "Caddy tasks fragment remains as historical drill reference")

caddy_text = caddy.read_text(encoding="utf-8") if caddy.is_file() else ""
live = "\n".join(
    line for line in caddy_text.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8122" in live and "/api/tasks" in live,
   "live Caddy routes Mode A Tasks to :8122 (ARCH-MS-92)")
ok("@tasks_sibling path_regexp tasks_sibling" in live and "handle @tasks_sibling" in live,
   "live Caddy carves dispatch/chat/review siblings to monolith")

app_impl_src = entrypoint_source("app")
ok("PM_TASKS_HTTP_PRIMARY" in app_impl_src,
   "monolith dual-strips Tasks via PM_TASKS_HTTP_PRIMARY (ARCH-MS-92)")

from switchboard.services.tasks.settings import TasksServiceSettings  # noqa: E402
ok(TasksServiceSettings.from_env().port == 8122, "default Tasks port remains 8122")

waive = ROOT / "docs" / "phase3" / "tasks_cut_waived.md"
ok(waive.is_file(), "Path B waive artifact retained (superseded)")
waive_text = waive.read_text(encoding="utf-8")
ok("ARCH-MS-91" in waive_text or "superseded" in waive_text.lower(),
   "waive artifact still references ARCH-MS-91 lineage")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
