#!/usr/bin/env python3
"""PROTO-8 project-scoped operator/Agent Host API and concurrency proof."""
from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="proto8-attention-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from switchboard.api.routers.attention import create_router  # noqa: E402
from switchboard.application.attention import default_attention_service  # noqa: E402
from switchboard.domain.projects.context import ProjectContext  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def project(raw):
    if raw not in {"switchboard", "helm"}:
        raise HTTPException(400, "unknown project")
    return raw


def body_project(body):
    if not body.get("project"):
        raise HTTPException(400, "project is required")
    return project(body["project"])


def principal(_request, project_id, scopes=("read",), dev_actor="test"):
    return {
        "id": f"principal/{project_id}",
        "kind": "service",
        "project": project_id,
        "scopes": ["read", "write:ixp", "admin"],
        "effective_scopes": ["read", "write:ixp", "admin"],
        "display_name": dev_actor,
    }


def question(number=1, **overrides):
    payload = {
        "project": "switchboard",
        "provider": "provider-neutral",
        "provider_request_id": f"provider-{number}",
        "schema_version": "provider.question.v1",
        "prompt": f"Question {number}",
        "choices": [{"id": "yes"}, {"id": "no"}],
        "idempotency_key": f"request-{number}",
        "host_id": "host/proto8",
        "runner_session_id": "run-proto8",
        "context": {"frozen": True},
    }
    payload.update(overrides)
    return payload


try:
    store.init_db("switchboard")
    store.init_db("helm")
    app = FastAPI()
    app.include_router(create_router(
        resolve_project=project,
        resolve_principal=principal,
        resolve_body_project=body_project,
        list_pending_acks=lambda **_: [],
        list_inbox=lambda *_args, **_kwargs: [],
    ))
    client = TestClient(app)

    # Explicit project scope, idempotency, and auth isolation.
    missing = client.get("/api/attention/requests")
    ok(missing.status_code == 422, "operator queue refuses missing project scope")
    created = client.post("/ixp/v1/attention/requests", json=question()).json()
    replay = client.post("/ixp/v1/attention/requests", json=question()).json()
    request_id = created["request"]["request_id"]
    ok(created["created"] is True and replay["idempotent_replay"] is True,
       "Agent Host request upsert is idempotent")
    isolated = client.get(
        f"/api/attention/requests/{request_id}?project=helm")
    ok(isolated.status_code == 404,
       "detail lookup cannot cross the authorized project boundary")

    # Bell count and list use the same durable queue predicate.
    queue = client.get("/api/attention/requests?project=switchboard").json()
    bell = client.get("/api/attention/count?project=switchboard").json()
    ok(queue["count"] == bell["count"] == len(queue["items"]) == 1,
       "bell count and queue list agree before a decision")

    decision = client.post(
        f"/api/attention/requests/{request_id}/decide?project=switchboard",
        json={"expected_version": 1, "choice": {"id": "yes"},
              "idempotency_key": "decision-1"},
    )
    stale = client.post(
        f"/api/attention/requests/{request_id}/decide?project=switchboard",
        json={"expected_version": 1, "choice": {"id": "no"},
              "idempotency_key": "decision-stale"},
    )
    ok(decision.status_code == 200 and stale.status_code == 409,
       "conditional decision write rejects a stale request version")
    queue = client.get("/api/attention/requests?project=switchboard").json()
    bell = client.get("/api/attention/count?project=switchboard").json()
    ok(queue["count"] == bell["count"] == 0,
       "decision-recorded work leaves both operator queue surfaces together")

    claimed = client.post("/ixp/v1/attention/decisions/claim", json={
        "project": "switchboard", "host_id": "host/proto8",
        "provider": "provider-neutral",
    }).json()
    duplicate = client.post("/ixp/v1/attention/decisions/claim", json={
        "project": "switchboard", "host_id": "host/proto8",
        "provider": "provider-neutral",
    }).json()
    ok(claimed["claimed"] is True and duplicate["claimed"] is False
       and claimed["delivery"]["request"]["status"] == "delivering",
       "delivery claim reports honest state and cannot be delivered twice")
    wrong_host = client.post(
        f"/ixp/v1/attention/requests/{request_id}/delivery",
        json={"project": "switchboard", "host_id": "host/other",
              "expected_version": 3, "receipt": {"provider_ack": "stolen"}},
    )
    ok(wrong_host.status_code == 403,
       "completion acknowledgement is fenced to the request's Agent Host")
    completed = client.post(
        f"/ixp/v1/attention/requests/{request_id}/delivery",
        json={"project": "switchboard", "host_id": "host/proto8",
              "expected_version": 3, "receipt": {"provider_ack": "ack-1"}},
    ).json()
    ok(completed["status"] == "resolved"
       and completed["delivery_receipt"]["provider_ack"] == "ack-1",
       "Agent Host completion acknowledgement records its receipt")

    # Production-shaped two-connection race: exactly one host call claims delivery.
    race = client.post("/ixp/v1/attention/requests", json=question(2)).json()
    race_id = race["request"]["request_id"]
    client.post(
        f"/api/attention/requests/{race_id}/decide?project=switchboard",
        json={"expected_version": 1, "choice": {"id": "yes"},
              "idempotency_key": "decision-race"},
    )
    ctx = ProjectContext(
        project_id="switchboard", source="test",
        principal_id="principal/host", effective_scopes=("write:ixp",))

    def race_claim():
        return default_attention_service.claim_decision(
            ctx, host_id="host/proto8", provider="provider-neutral",
            actor="host/proto8", request_id=race_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: race_claim(), range(8)))
    ok(sum(result is not None for result in results) == 1,
       "concurrent Agent Host claimers produce exactly one delivery")

    router_source = (
        ROOT / "src/switchboard/api/routers/attention.py"
    ).read_text(encoding="utf-8")
    ok("service." in router_source
       and "default_attention_repository." not in router_source,
       "REST handlers remain thin adapters over the authoritative service")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
