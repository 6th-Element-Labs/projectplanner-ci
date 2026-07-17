#!/usr/bin/env python3
"""BUG-80: every chartered Coord response releases free native arenas."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401
from switchboard.services.coord import heap
from switchboard.services.coord.router import create_router


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class Queries:
    def board(self, project: str, *, cards: bool = False) -> dict[str, Any]:
        return {"project": project, "cards": cards}

    def signals(self, project: str) -> dict[str, Any]:
        return {"project": project, "counts": {}}

    def delta(self, project: str, *, since_cursor: int = 0,
              lane: str = "") -> dict[str, Any]:
        return {"project": project, "cursor": since_cursor, "lane": lane, "updates": []}

    def coordination(self, project: str, *, limit: int = 500) -> dict[str, Any]:
        return {"project": project, "agents": [], "messages": [], "decisions": []}

    def coordinator_decisions(self, project: str, **_kwargs) -> list[dict[str, Any]]:
        return []


class Auth:
    def authorize(self, _request, project: str) -> dict[str, Any]:
        return {"principal_id": project, "scopes": ["read"]}


calls: list[str] = []
original_release = heap.release_native_heap
heap.release_native_heap = lambda: calls.append("trim")
try:
    app = FastAPI()
    app.include_router(create_router(
        resolve_project=lambda project: project,
        etag_json=lambda _request, _payload, max_age=0: Response(
            content=b"{}", media_type="application/json"
        ),
        queries=Queries(),
        auth=Auth(),
    ))
    client = TestClient(app)
    responses = [
        client.get("/api/board", params={"project": "alpha"}),
        client.get("/api/signals", params={"project": "alpha"}),
        client.get("/ixp/v1/delta", params={"project": "alpha"}),
        client.get("/api/coordination", params={"project": "alpha"}),
        client.get("/api/coordinator_decisions", params={"project": "alpha"}),
    ]
finally:
    heap.release_native_heap = original_release

ok(all(response.status_code == 200 for response in responses),
   "all five chartered routes still return 200")
ok(calls == ["trim"] * 5,
   "all five responses release free native arenas after their bodies are sent")
ok(responses[0].json() == {}, "board ETag response semantics are preserved")
ok(responses[2].json()["project"] == "alpha", "JSON response semantics are preserved")

print(f"\nBUG-80 Coord heap release: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
