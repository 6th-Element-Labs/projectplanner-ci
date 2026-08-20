"""Production coordinator adapter for Mission Bot v5."""
from __future__ import annotations

from typing import Any, Iterable

from coordinator_daemon import summarize_scope_result
from scoped_completion_coordinator import ScopedCompletionCoordinator
from switchboard.domain import execution_liveness


class V5ScopedCompletionCoordinator(ScopedCompletionCoordinator):
    """Use v5 as the sole pager for every exact W2 scope this process holds."""

    def _register_or_heartbeat(self, project: str) -> dict[str, Any]:
        """V5 scope leases need no coordinator agent registration."""
        return {
            "registered": False,
            "heartbeat": False,
            "project": project,
            "reason": "v5_scope_lease_is_identity",
        }

    def _acquire_scope_authority(
        self, project: str, scope: dict[str, Any],
    ) -> dict[str, Any]:
        return self.store.acquire_autopilot_scope_lease(
            scope["scope_id"], holder_agent_id=self.agent_id,
            project=project, ttl_seconds=self.config.lease_ttl_seconds,
            registration_required=False, now=float(self.clock()),
        )

    def _scope_candidates(
        self, scope: dict[str, Any], mission_status: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Cap starts with C1 runner truth, independent of wakes and claims."""
        candidates = super()._scope_candidates(scope, mission_status)
        projects = {
            str(row.get("task_project") or row.get("project_id") or "")
            for row in candidates
        }
        live = 0
        reader = getattr(self.store, "list_runner_sessions", None)
        if callable(reader):
            for project in sorted(projects):
                rows = reader(project=project, include_stale=False)
                live += sum(
                    1 for row in rows
                    if execution_liveness.is_live(row, now=float(self.clock()))
                )
        available = max(0, self.config.mission_bot_v5_max_concurrency - live)
        return candidates[:available]

    def _completion_tick(
        self,
        task_id: str,
        *,
        task_project: str,
        scope_project: str,
        authority: dict[str, Any],
    ) -> dict[str, Any]:
        from switchboard.application.mission_bot_v5 import run_scoped_mission_tick

        return run_scoped_mission_tick(
            task_id,
            project=task_project,
            scope_project=scope_project,
            scope_authority=authority,
            actor=self.config.actor,
            agent_id=self.agent_id,
            store_mod=self.store,
        )

    def _complete_terminal_scope_after_projection(
        self,
        *,
        project: str,
        scope: dict[str, Any],
        authority: dict[str, Any],
        tasks: Iterable[tuple[str, str]],
    ) -> dict[str, Any]:
        receipts = []
        for task_project, task_id in tasks:
            try:
                tick = self._completion_tick(
                    task_id,
                    task_project=task_project,
                    scope_project=project,
                    authority=authority,
                )
            except Exception as exc:  # keep the scope active and visible
                tick = {
                    "schema": "switchboard.mission_worker_tick.v5",
                    "task_id": task_id,
                    "action": "wait",
                    "reason": "terminal_projection_failed",
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            receipts.append(tick)

        unconfirmed = [
            tick for tick in receipts
            if tick.get("reason") != "terminal_provenance"
        ]
        if unconfirmed:
            result = {
                "status": "completion_tick_failed",
                "scope_id": scope.get("scope_id"),
                "deliverable_id": scope.get("deliverable_id") or "",
                "task_id": scope.get("task_id") or "",
                "reason": "terminal_projection_unconfirmed",
                "receipts": receipts,
            }
            self.store.update_autopilot_scope(
                scope["scope_id"], project=project,
                last_result=summarize_scope_result(result),
                ticked_at=float(self.clock()),
            )
            return result

        result = {
            "status": "completed",
            "scope_id": scope.get("scope_id"),
            "deliverable_id": scope.get("deliverable_id") or "",
            "task_id": scope.get("task_id") or "",
            "receipts": receipts,
        }
        self.store.update_autopilot_scope(
            scope["scope_id"], project=project, status="completed",
            last_result=summarize_scope_result(result),
            ticked_at=float(self.clock()),
        )
        return result

    def _run_standalone_task_scope(
        self,
        project: str,
        scope: dict[str, Any],
        authority: dict[str, Any],
    ) -> dict[str, Any]:
        task_project = str(scope.get("task_project") or project)
        task_id = str(scope.get("task_id") or "").upper()
        detail = self.store.get_task(task_id, project=task_project) or {}
        if self.config.act and detail and self._terminal_task(detail):
            return self._complete_terminal_scope_after_projection(
                project=project, scope=scope, authority=authority,
                tasks=((task_project, task_id),),
            )
        return super()._run_standalone_task_scope(project, scope, authority)

    def run_scope(
        self,
        project: str,
        scope: dict[str, Any],
        denied_lanes: Iterable[str] = (),
    ) -> dict[str, Any]:
        deliverable_id = str(scope.get("deliverable_id") or "")
        if not self.config.act or not deliverable_id:
            return super().run_scope(project, scope, denied_lanes)

        mission_status = self.store.get_mission_status(
            project=project, deliverable_id=deliverable_id,
        )
        if mission_status.get("error") or not self._scope_complete(scope, mission_status):
            return super().run_scope(project, scope, denied_lanes)

        terminal_tasks = []
        for link in self._scope_dispatch_links(scope, mission_status):
            task_project = str(link.get("project_id") or project)
            task_id = str(link.get("task_id") or "").upper()
            detail = self._task_detail(mission_status, task_id, task_project)
            if task_id and self._terminal_task(detail):
                terminal_tasks.append((task_project, task_id))
        if not terminal_tasks:
            return super().run_scope(project, scope, denied_lanes)

        self._register_or_heartbeat(project)
        authority = self._acquire_scope_authority(project, scope)
        if authority.get("error"):
            return {
                "status": "scope_authority_denied",
                "scope_id": scope.get("scope_id"),
                "error": authority.get("error"),
            }
        return self._complete_terminal_scope_after_projection(
            project=project, scope=scope, authority=authority,
            tasks=terminal_tasks,
        )


__all__ = ["V5ScopedCompletionCoordinator"]
