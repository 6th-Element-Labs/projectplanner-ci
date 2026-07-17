"""Production adapters for Coord query/Auth ports.

This composition module lives outside ``services.coord`` and binds the thin
service directly to package repositories.  Root Auth remains behind the shared
Auth port, matching the Auth and Tasks process-cut pattern.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import Request

from constants import META_SECTIONS
from switchboard.api import deps as api_deps
from switchboard.services.coord import deps as coord_deps
from switchboard.services.coord.ports import CoordQueryPort, CoordReadAuthPort
from switchboard.services.coord.signals import compute_plan_signals
from switchboard.storage.repositories import access as access_repo
from switchboard.storage.repositories import activity as activity_repo
from switchboard.storage.repositories import coordination as coordination_repo
from switchboard.storage.repositories import decisions as decisions_repo
from switchboard.storage.repositories import tasks as tasks_repo

import read_cache


class RepositoryCoordQueries:
    """Read-only Coord projection implemented against package repositories."""

    def board(self, project: str, *, cards: bool = False) -> dict[str, Any]:
        stamp = tasks_repo.project_task_stamp(project)
        cache_key = f"{project}\x00cards" if cards else project
        return dict(read_cache.ttl_read_cache(
            "coord_board",
            cache_key,
            stamp,
            lambda: self._build_board(project, cards=cards),
        ))

    def _build_board(self, project: str, *, cards: bool) -> dict[str, Any]:
        tasks = tasks_repo.list_tasks_for_board(project=project)
        payload = {
            key: activity_repo.get_meta(key, project=project)
            for key in META_SECTIONS
        }
        payload["project"] = next(
            (item for item in access_repo.projects() if item["id"] == project),
            {
                "id": project,
                "label": project,
                "pretitle": "",
                "purpose": access_repo.project_access(project).get("purpose") or "",
                "boundary": access_repo.project_access(project).get("boundary") or "",
            },
        )
        payload["rollups"] = tasks_repo.board_rollups(project=project, tasks=tasks)
        drop = tasks_repo._BOARD_CARDS_DROP if cards else tasks_repo._BOARD_LITE_DROP
        projected = [{key: value for key, value in task.items() if key not in drop}
                     for task in tasks]
        by_workstream: dict[str, dict[str, Any]] = {}
        for task in projected:
            workstream = by_workstream.setdefault(task["_wsId"], {
                "workstream_id": task["_wsId"],
                "name": task["_wsName"],
                "tasks": [],
            })
            workstream["tasks"].append(task)
        payload["workstreams"] = list(by_workstream.values())
        return payload

    def list_tasks(self, project: str) -> list[dict[str, Any]]:
        return list(tasks_repo.list_tasks_for_board(project=project))

    def get_meta(self, key: str, default: Any, project: str) -> Any:
        return activity_repo.get_meta(key, default, project=project)

    def signals(self, project: str) -> dict[str, Any]:
        stamp = tasks_repo.project_task_stamp(project)
        return dict(read_cache.ttl_read_cache(
            "coord_plan_signals",
            project,
            stamp,
            lambda: compute_plan_signals(self, project=project),
        ))

    def delta(self, project: str, *, since_cursor: int = 0,
              lane: str = "") -> dict[str, Any]:
        return dict(activity_repo.get_activity_delta(
            since_cursor=since_cursor, lane=lane, project=project
        ))

    def coordination(self, project: str, *, limit: int = 500) -> dict[str, Any]:
        return {
            "project": project,
            "agents": coordination_repo.list_active_agents(project=project),
            "messages": coordination_repo.list_agent_messages(project=project, limit=limit),
            "decisions": decisions_repo.list_decisions(project=project, limit=limit),
            "coordinator_decisions": decisions_repo.list_coordinator_decisions(
                project=project, limit=min(limit, 200)
            ),
        }

    def coordinator_decisions(
        self,
        project: str,
        *,
        task_id: str = "",
        deliverable_id: str = "",
        decision_kind: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return list(decisions_repo.list_coordinator_decisions(
            task_id=task_id,
            deliverable_id=deliverable_id,
            decision_kind=decision_kind,
            limit=limit,
            project=project,
        ))


class ProjectScopedCoordReadAuth:
    """Adapter over the shared project-scoped principal resolver."""

    def __init__(self, resolver: Callable[..., dict[str, Any]] | None = None):
        self._resolver = resolver or api_deps.resolve_principal

    def authorize(self, request: Request, project: str) -> dict[str, Any]:
        return dict(self._resolver(request, project, ("read",), dev_actor="coord"))


def configure_coord_ports(
    *,
    queries: CoordQueryPort | None = None,
    auth: CoordReadAuthPort | None = None,
) -> None:
    coord_deps.configure(
        queries=queries or RepositoryCoordQueries(),
        auth=auth or ProjectScopedCoordReadAuth(),
    )


def probe_coord_readiness(project: str = "") -> dict[str, Any]:
    """Fail closed across DB/schema, browser auth, and one Coord-owned query."""
    from switchboard.api.routers.auth import session as auth_session
    from switchboard.storage.repositories import projects as projects_repo

    project = project or os.environ.get("SWITCHBOARD_COORD_READY_PROJECT", "switchboard").strip()
    checks: dict[str, Any] = {}
    db_error = projects_repo.probe_project_db(project)
    checks["database_schema"] = "ok" if db_error is None else db_error
    try:
        auth_session.require_production_secret()
        checks["browser_session_auth"] = "ok" if auth_session.COOKIE_NAME else "cookie_name_missing"
    except Exception as exc:
        checks["browser_session_auth"] = type(exc).__name__
    try:
        RepositoryCoordQueries().board(project, cards=False)
        checks["coord_repository_read"] = "ok"
    except Exception as exc:
        checks["coord_repository_read"] = type(exc).__name__
    return {"ok": all(value == "ok" for value in checks.values()), "checks": checks}
