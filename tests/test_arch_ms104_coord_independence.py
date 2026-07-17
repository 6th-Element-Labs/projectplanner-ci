#!/usr/bin/env python3
"""ARCH-MS-104 executable Coord independence verdict and port boundary."""
from __future__ import annotations

import importlib.util
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from path_setup import ROOT


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


spec = importlib.util.spec_from_file_location(
    "arch_ms104_coord_independence",
    ROOT / "scripts" / "arch_ms104_coord_independence.py",
)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

report = gate.evaluate(ROOT, run_probe=True)
ok(report.get("ok") is True, "executable independence artifact is internally consistent")
ok(report.get("verdict") == "go", "verdict is explicit Go")
ok(report.get("process_cut_authorized") is True, "Go authorizes the side-by-side process build")
ok(report.get("failed_gates") == [], "all six independence gates pass")
ok(report.get("forbidden_imports") == {}, "Coord package has zero forbidden monolith imports")
ok((report.get("sqlite_probe") or {}).get("ok") is True,
   "WAL reader/writer contention probe passes")
ok((report.get("sqlite_probe") or {}).get("lock_errors") == 0,
   "WAL reader/writer contention has zero lock errors")

verdict = gate.load_verdict(ROOT / "docs" / "coord" / "coord_independence_verdict.json")
ok(verdict.get("writer_inventory") == [], "day-one Coord owns no writers")
ok("ARCH-MS-105" in (verdict.get("go_only_tasks") or []),
   "machine verdict identifies the Go-only standalone service task")
ok((verdict.get("tasks_production_acceptance") or {}).get("green") is True,
   "Tasks production acceptance remains green")

from switchboard.services.coord.ports import CoordQueryPort, CoordReadAuthPort  # noqa: E402
from switchboard.services.coord.router import create_router  # noqa: E402
from switchboard.api.coord_port_adapters import (  # noqa: E402
    ProjectScopedCoordReadAuth,
    RepositoryCoordQueries,
)
from switchboard.storage.repositories import activity as activity_repo  # noqa: E402
from switchboard.storage.repositories import access as access_repo  # noqa: E402
from switchboard.storage.repositories import coordination as coordination_repo  # noqa: E402
from switchboard.storage.repositories import decisions as decisions_repo  # noqa: E402
from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
import read_cache  # noqa: E402


class FakeQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def board(self, project: str, *, cards: bool = False) -> dict[str, Any]:
        self.calls.append(("board", project, cards))
        return {"project": project, "cards": cards}

    def signals(self, project: str) -> dict[str, Any]:
        self.calls.append(("signals", project))
        return {"project": project, "counts": {}}

    def delta(self, project: str, *, since_cursor: int = 0,
              lane: str = "") -> dict[str, Any]:
        self.calls.append(("delta", project, since_cursor, lane))
        return {"project": project, "cursor": since_cursor, "lane": lane, "updates": []}

    def coordination(self, project: str, *, limit: int = 500) -> dict[str, Any]:
        self.calls.append(("coordination", project, limit))
        return {"project": project, "agents": [], "messages": [], "decisions": [],
                "coordinator_decisions": []}

    def coordinator_decisions(self, project: str, *, task_id: str = "",
                              deliverable_id: str = "", decision_kind: str = "",
                              limit: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("coordinator_decisions", project, task_id,
                           deliverable_id, decision_kind, limit))
        return []


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def authorize(self, request, project: str) -> dict[str, Any]:
        token = request.headers.get("authorization") or ""
        if not token:
            raise HTTPException(401, "not authenticated")
        if token != "Bearer alpha-read" or project != "alpha":
            raise HTTPException(403, "forbidden")
        self.calls.append(project)
        return {"principal_id": "arch-ms104", "scopes": ["read"]}


queries = FakeQueries()
read_auth = FakeAuth()
ok(isinstance(queries, CoordQueryPort), "query adapter satisfies CoordQueryPort")
ok(isinstance(read_auth, CoordReadAuthPort), "Auth adapter satisfies CoordReadAuthPort")
ok(isinstance(RepositoryCoordQueries(), CoordQueryPort),
   "production repository adapter satisfies CoordQueryPort")
ok(isinstance(ProjectScopedCoordReadAuth(), CoordReadAuthPort),
   "production shared-Auth adapter satisfies CoordReadAuthPort")

# Execute the production query adapter against patched package repositories. This
# proves the binding calls repository modules rather than merely naming them.
patches = {
    (tasks_repo, "list_tasks_for_board"): tasks_repo.list_tasks_for_board,
    (tasks_repo, "board_rollups"): tasks_repo.board_rollups,
    (tasks_repo, "project_task_stamp"): tasks_repo.project_task_stamp,
    (access_repo, "projects"): access_repo.projects,
    (access_repo, "project_access"): access_repo.project_access,
    (activity_repo, "get_meta"): activity_repo.get_meta,
    (activity_repo, "get_activity_delta"): activity_repo.get_activity_delta,
    (coordination_repo, "list_active_agents"): coordination_repo.list_active_agents,
    (coordination_repo, "list_agent_messages"): coordination_repo.list_agent_messages,
    (decisions_repo, "list_decisions"): decisions_repo.list_decisions,
    (decisions_repo, "list_coordinator_decisions"): decisions_repo.list_coordinator_decisions,
    (read_cache, "ttl_read_cache"): read_cache.ttl_read_cache,
}
try:
    tasks_repo.list_tasks_for_board = lambda project="": []
    tasks_repo.board_rollups = lambda project="", tasks=None: {
        "total_tasks": len(tasks or [])
    }
    tasks_repo.project_task_stamp = lambda project="": (project, 1)
    access_repo.projects = lambda: [{"id": "alpha", "label": "Alpha"}]
    access_repo.project_access = lambda project="": {"purpose": "", "boundary": ""}
    activity_repo.get_meta = lambda key, default=None, project="": default
    activity_repo.get_activity_delta = lambda since_cursor=0, lane="", project="": {
        "project": project, "cursor": since_cursor, "lane": lane, "updates": []
    }
    coordination_repo.list_active_agents = lambda project="": [{"agent_id": project}]
    coordination_repo.list_agent_messages = lambda project="", limit=500: [{"limit": limit}]
    decisions_repo.list_decisions = lambda project="", limit=0: [{"project": project}]
    decisions_repo.list_coordinator_decisions = lambda **kwargs: [{"query": kwargs}]
    read_cache.ttl_read_cache = lambda namespace, ident, stamp, builder: builder()

    production = RepositoryCoordQueries()
    board = production.board("alpha", cards=True)
    ok(board.get("project") == {"id": "alpha", "label": "Alpha"}
       and board.get("rollups") == {"total_tasks": 0},
       "production board query executes through package repositories")
    ok(production.signals("alpha").get("counts", {}).get("ready") == 0,
       "production signals query executes through task/meta repositories")
    ok(production.delta("alpha", since_cursor=9, lane="ARCH-MS").get("cursor") == 9,
       "production delta query executes through activity repository")
    rollup = production.coordination("alpha", limit=17)
    ok(rollup.get("agents") == [{"agent_id": "alpha"}]
       and rollup.get("messages") == [{"limit": 17}],
       "production coordination query executes through coordination repositories")
    ok(production.coordinator_decisions("alpha", task_id="ARCH-MS-104")[0]["query"]["project"] == "alpha",
       "production decision query executes through decisions repository")
finally:
    for (module, name), original in patches.items():
        setattr(module, name, original)


def resolve_project(project: str) -> str:
    if project not in {"alpha", "beta"}:
        raise HTTPException(400, "unknown project")
    return project


app = FastAPI()
app.include_router(create_router(
    resolve_project=resolve_project,
    etag_json=lambda request, payload, max_age=0: payload,
    queries=queries,
    auth=read_auth,
))
client = TestClient(app)

missing = client.get("/ixp/v1/delta")
unauth = client.get("/ixp/v1/delta", params={"project": "alpha"})
cross_project = client.get(
    "/api/board", params={"project": "beta"},
    headers={"Authorization": "Bearer alpha-read"},
)
ok(missing.status_code == 422, f"every route requires explicit project (got {missing.status_code})")
ok(unauth.status_code == 401, f"Coord reads reject missing bearer (got {unauth.status_code})")
ok(cross_project.status_code == 403,
   f"Coord reads reject bearer outside project scope (got {cross_project.status_code})")

headers = {"Authorization": "Bearer alpha-read"}
responses = [
    client.get("/api/board", params={"project": "alpha", "view": "cards"}, headers=headers),
    client.get("/api/signals", params={"project": "alpha"}, headers=headers),
    client.get("/ixp/v1/delta", params={"project": "alpha", "lane": "ARCH-MS",
                                       "since_cursor": 7}, headers=headers),
    client.get("/api/coordination", params={"project": "alpha", "limit": 33}, headers=headers),
    client.get("/api/coordinator_decisions", params={"project": "alpha",
                                                      "task_id": "ARCH-MS-104"}, headers=headers),
]
ok(all(response.status_code == 200 for response in responses),
   "all five day-one routes execute through injected ports")
ok(read_auth.calls == ["alpha"] * 5,
   f"all five routes bind project-scoped read Auth ({read_auth.calls!r})")
ok([call[0] for call in queries.calls] == [
    "board", "signals", "delta", "coordination", "coordinator_decisions"
], f"exact day-one query surface is called ({queries.calls!r})")

print(f"\nARCH-MS-104 Coord independence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
