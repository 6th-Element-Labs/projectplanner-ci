"""One fenced completion owner for one operator-started Autopilot scope."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Iterable

from coordinator_daemon import CoordinatorDaemon, DaemonConfig


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


    def _run_standalone_task_scope(self, project: str, scope: Dict[str, Any],
                                   authority: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one task from Start to Done without a deliverable.

        Reuses the existing completion owner rather than running a second
        lifecycle: mission_coordinator decides which role the task needs next,
        and Task Execution's start_task creates that generation. This method
        only supplies the target and the scope authority.
        """
        import mission_coordinator

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
                last_result=result, ticked_at=float(self.clock()))
            return result

        if not self.config.act:
            return {"status": "observed", "scope_id": scope.get("scope_id"),
                    "task_id": task_id, "task_status": detail.get("status")}

        role = mission_coordinator._lifecycle_role(
            self.store, task_project, task_id)
        head_sha = str((detail.get("git_state") or {}).get("head_sha") or "").strip()
        if detail.get("status") == "In Review" and role != "remediation":
            role = "review_merge"
        if role in {"review_merge", "remediation"} and not head_sha:
            # Refusing loudly beats dispatching a review generation that cannot
            # bind to an exact head.
            return {"status": "dispatch_blocked", "scope_id": scope.get("scope_id"),
                    "task_id": task_id, "role": role,
                    "error": "review_head_sha_required"}

        from switchboard.application.commands import task_execution
        try:
            dispatch = task_execution.start_task(
                task_id, project=task_project, actor=self.config.actor,
                agent_id=self.agent_id, role=role,
                source_sha=head_sha or "")
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            dispatch = {"action": "refused", "error": type(exc).__name__,
                        "reason": str(exc)}

        result = {
            "status": "dispatched", "scope_id": scope.get("scope_id"),
            "task_id": task_id, "task_project": task_project, "role": role,
            "head_sha": head_sha or None,
            "generation": authority.get("generation"),
            "fence_epoch": authority.get("fence_epoch"),
            "receipts": [dispatch],
        }
        self.store.update_autopilot_scope(
            scope["scope_id"], project=project, last_result=result,
            ticked_at=float(self.clock()))
        return result

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
                last_result=result, ticked_at=float(self.clock()))
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
            revision = self._candidate_revision(mission_status, candidate)
            wake_generation = self._wake_generation(
                project, deliverable_id, task_id)
            policy = {
                "auto_refresh_brief": not receipts,
                "auto_start": bool(self.config.act),
                "allowed_lanes": list(self.config.allowed_lanes),
                "denied_lanes": list(denied_lanes),
                "target_task_id": task_id,
                "target_project_id": task_project,
            }
            policy_revision = hashlib.sha256(
                json.dumps(policy, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
            idem_key = (
                f"s15:{scope['scope_id']}:{authority['generation']}:"
                f"{authority['fence_epoch']}:{task_id}:{revision}:"
                f"wake-generation-{wake_generation}:policy-{policy_revision}"
            )
            result = self.store.run_mission_coordinator_tick(
                project=project,
                deliverable_id=deliverable_id,
                coordinator_agent_id=self.agent_id,
                actor=self.config.actor,
                policy=policy,
                scope_authority=authority,
                idem_key=idem_key,
            )
            receipts.append({
                "task_id": task_id,
                "task_project": task_project,
                "status": result.get("status"),
                "decision_id": result.get("decision_id"),
                "dispatch": result.get("dispatch"),
                "error": result.get("error"),
                "idem_key": idem_key,
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
            scope["scope_id"], project=project, last_result=result,
            ticked_at=float(self.clock()))
        return result


__all__ = ["ScopedCompletionCoordinator"]
