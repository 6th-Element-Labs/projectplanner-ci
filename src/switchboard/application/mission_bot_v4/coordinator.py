"""Isolated-test composition for the Mission Bot v4 scoped writer."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from coordinator_daemon import summarize_scope_result
from scoped_completion_coordinator import ScopedCompletionCoordinator


class V4ScopedCompletionCoordinator(ScopedCompletionCoordinator):
    """Use v4 as the sole pager for scopes this coordinator fences under W2."""

    def _completion_tick(
        self,
        task_id: str,
        *,
        task_project: str,
        scope_project: str,
        authority: Dict[str, Any],
    ) -> Dict[str, Any]:
        from switchboard.application.mission_bot_v4 import run_scoped_mission_tick

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
        scope: Dict[str, Any],
        authority: Dict[str, Any],
        tasks: Iterable[Tuple[str, str]],
    ) -> Dict[str, Any]:
        """Project canonical Done into v4 before closing its W2 scope.

        The shared scope owner legitimately treats canonical task provenance as
        sufficient to close a v1 scope.  V4 additionally owns an append-only
        mission journal, so its scope must remain active until the existing
        fenced v4 tick confirms that same provenance in the journal.
        """
        receipts = []
        for task_project, task_id in tasks:
            try:
                tick = self._completion_tick(
                    task_id,
                    task_project=task_project,
                    scope_project=project,
                    authority=authority,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the active scope
                tick = {
                    "schema": "switchboard.mission_worker_tick.v4",
                    "task_id": task_id,
                    "action": "block_release",
                    "reason": "terminal_projection_failed",
                    "release_blocked": True,
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
                "release_blocked": True,
                "receipts": receipts,
            }
            self.store.update_autopilot_scope(
                scope["scope_id"],
                project=project,
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
            scope["scope_id"],
            project=project,
            status="completed",
            last_result=summarize_scope_result(result),
            ticked_at=float(self.clock()),
        )
        return result

    def _run_standalone_task_scope(
        self,
        project: str,
        scope: Dict[str, Any],
        authority: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_project = str(scope.get("task_project") or project)
        task_id = str(scope.get("task_id") or "").upper()
        detail = self.store.get_task(task_id, project=task_project) or {}
        if self.config.act and detail and self._terminal_task(detail):
            return self._complete_terminal_scope_after_projection(
                project=project,
                scope=scope,
                authority=authority,
                tasks=((task_project, task_id),),
            )
        return super()._run_standalone_task_scope(project, scope, authority)

    def run_scope(
        self,
        project: str,
        scope: Dict[str, Any],
        denied_lanes: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """Ensure terminal deliverable scopes also close their v4 journals."""
        deliverable_id = str(scope.get("deliverable_id") or "")
        if not self.config.act or not deliverable_id:
            return super().run_scope(project, scope, denied_lanes)

        mission_status = self.store.get_mission_status(
            project=project, deliverable_id=deliverable_id,
        )
        if mission_status.get("error") or not self._scope_complete(
            scope, mission_status,
        ):
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
        authority = self.store.acquire_autopilot_scope_lease(
            scope["scope_id"],
            holder_agent_id=self.agent_id,
            project=project,
            ttl_seconds=self.config.lease_ttl_seconds,
            now=float(self.clock()),
        )
        if authority.get("error"):
            return {
                "status": "scope_authority_denied",
                "scope_id": scope.get("scope_id"),
                "error": authority.get("error"),
            }
        return self._complete_terminal_scope_after_projection(
            project=project,
            scope=scope,
            authority=authority,
            tasks=terminal_tasks,
        )


__all__ = ["V4ScopedCompletionCoordinator"]
