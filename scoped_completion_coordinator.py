"""One fenced completion owner for one operator-started Autopilot scope."""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable

from coordinator_daemon import (
    CoordinatorDaemon,
    DaemonConfig,
    summarize_scope_result,
)


class ScopedCompletionCoordinator(CoordinatorDaemon):
    """Drive only scopes whose exact lease/generation/fence this agent holds."""

    def __init__(self, config: DaemonConfig, *, store_mod: Any,
                 agent_id: str, clock: Any = None) -> None:
        super().__init__(
            config, store_mod=store_mod, instance_id="scoped-owner",
            clock=clock)
        self.agent_id = str(agent_id or "").strip()
        if not self.agent_id:
            raise ValueError("agent_id required")

    def scope_candidates(self, scope: Dict[str, Any],
                         mission_status: Dict[str, Any]) -> list[Dict[str, Any]]:
        return self._scope_candidates(scope, mission_status)

    def _register_or_heartbeat(self, project: str) -> Dict[str, Any]:
        presence = self.store.heartbeat(
            self.agent_id, project=project, actor=self.config.actor)
        if not presence.get("error"):
            return presence
        return self.store.register_agent(
            self.agent_id,
            runtime="scoped-completion-coordinator",
            lane="COORD",
            ttl_s=max(self.config.heartbeat_seconds * 3, 30),
            control={
                "mode": "scoped_completion_owner",
                "profile_id": self.config.profile_id,
                "acting": bool(self.config.act),
            },
            actor=self.config.actor,
            project=project,
        )

    def _completion_tick(
        self,
        task_id: str,
        *,
        task_project: str,
        scope_project: str,
        authority: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Require an explicit lifecycle engine implementation.

        The production entrypoint constructs ``V4ScopedCompletionCoordinator``.
        Keeping this base class engine-free prevents a retired controller from
        remaining as a hidden fallback or second writer.
        """
        raise RuntimeError(
            "scoped completion coordinator requires an explicit lifecycle engine"
        )


    def _run_standalone_task_scope(self, project: str, scope: Dict[str, Any],
                                   authority: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one task through the same Mission Bot tick as every other scope."""
        task_project = str(scope.get("task_project") or project)
        task_id = str(scope.get("task_id") or "").upper()
        detail = self.store.get_task(task_id, project=task_project) or {}
        if not detail or detail.get("error"):
            return {"status": "failed", "error": "unknown task",
                    "scope_id": scope.get("scope_id"), "task_id": task_id}

        if self._terminal_task(detail):
            result = {"status": "completed", "scope_id": scope.get("scope_id"),
                      "task_id": task_id, "receipts": []}
            self.store.update_autopilot_scope(
                scope["scope_id"], project=project, status="completed",
                last_result=summarize_scope_result(result),
                ticked_at=float(self.clock()))
            return result

        if not self.config.act:
            return {"status": "observed", "scope_id": scope.get("scope_id"),
                    "task_id": task_id, "task_status": detail.get("status")}

        try:
            tick = self._completion_tick(
                task_id,
                task_project=task_project,
                scope_project=project,
                authority=authority,
            )
            result = {
                "status": "completion_tick",
                "scope_id": scope.get("scope_id"),
                "task_id": task_id,
                "task_project": task_project,
                "generation": authority.get("generation"),
                "fence_epoch": authority.get("fence_epoch"),
                "receipts": [tick],
            }
        except Exception as exc:  # noqa: BLE001 - durable run preserves intent
            result = {
                "status": "completion_tick_failed",
                "scope_id": scope.get("scope_id"),
                "task_id": task_id,
                "task_project": task_project,
                "error": type(exc).__name__,
                "reason": str(exc),
                "receipts": [],
            }
        self.store.update_autopilot_scope(
            scope["scope_id"], project=project,
            last_result=summarize_scope_result(result),
            ticked_at=float(self.clock()))
        return result

    def _drive_scope(self, project: str, scope: Dict[str, Any]) -> Dict[str, Any]:
        """The tick's per-scope action: drive this scope through its lifecycle."""
        return self.run_scope(project, scope)

    def run_scope(self, project: str, scope: Dict[str, Any],
                  denied_lanes: Iterable[str] = ()) -> Dict[str, Any]:
        self._register_or_heartbeat(project)
        authority = self.store.acquire_autopilot_scope_lease(
            scope["scope_id"], holder_agent_id=self.agent_id,
            project=project, ttl_seconds=self.config.lease_ttl_seconds,
            now=float(self.clock()))
        if authority.get("error"):
            return {
                "status": "scope_authority_denied",
                "scope_id": scope.get("scope_id"),
                "error": authority.get("error"),
            }
        deliverable_id = str(scope.get("deliverable_id") or "")
        if scope.get("scope_type") == "task" and not deliverable_id:
            # A standalone task scope carries one task from Start to Done. There
            # is no deliverable to read a mission from, so drive the task itself
            # through the same completion owner every other path uses.
            return self._run_standalone_task_scope(project, scope, authority)
        mission_status = self.store.get_mission_status(
            project=project, deliverable_id=deliverable_id)
        if mission_status.get("error"):
            return {"status": "failed", "error": mission_status.get("error"),
                    "deliverable_id": deliverable_id}
        if self._scope_complete(scope, mission_status):
            result = {
                "status": "completed", "deliverable_id": deliverable_id,
                "scope_id": scope.get("scope_id"), "receipts": [],
            }
            self.store.update_autopilot_scope(
                scope["scope_id"], project=project, status="completed",
                last_result=summarize_scope_result(result),
                ticked_at=float(self.clock()))
            return result

        candidates = self._scope_candidates(scope, mission_status)
        receipts = []
        for candidate in candidates:
            task_id = str(candidate.get("task_id") or "").upper()
            task_project = (
                candidate.get("task_project")
                or candidate.get("project_id")
                or project
            )
            if task_project != project:
                self._register_or_heartbeat(task_project)
            if self.config.act:
                try:
                    tick = self._completion_tick(
                        task_id,
                        task_project=task_project,
                        scope_project=project,
                        authority=authority,
                    )
                    receipts.append({
                        "task_id": task_id,
                        "task_project": task_project,
                        "status": "completion_tick",
                        "completion": tick,
                    })
                except Exception as exc:  # noqa: BLE001 - expose failed tick
                    receipts.append({
                        "task_id": task_id,
                        "task_project": task_project,
                        "status": "completion_tick_failed",
                        "error": type(exc).__name__,
                        "reason": str(exc),
                    })
                continue
            receipts.append({
                "task_id": task_id,
                "task_project": task_project,
                "status": "observed",
            })
        result = {
            "status": "running" if receipts else "waiting",
            "scope_id": scope.get("scope_id"),
            "scope_type": scope.get("scope_type"),
            "deliverable_id": deliverable_id,
            "task_id": scope.get("task_id") or None,
            "candidate_count": len(candidates),
            "receipts": receipts,
            "authority": authority,
            "ticked_at": time.time(),
        }
        self.store.update_autopilot_scope(
            scope["scope_id"], project=project,
            last_result=summarize_scope_result(result),
            ticked_at=float(self.clock()))
        return result


__all__ = ["ScopedCompletionCoordinator"]
