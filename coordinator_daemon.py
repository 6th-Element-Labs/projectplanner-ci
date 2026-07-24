#!/usr/bin/env python3
"""Global coordinator janitor (SIMPLIFY-15).

This process owns bounded cleanup and observation only. Scoped completion
coordinators own implementation, review, remediation, merge, and reconciliation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import signal
import time
from typing import Any, Dict, Iterable, Mapping, Optional
import uuid

import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable
from switchboard.domain.completion import routing as completion_routing


STATE_SCHEMA = "switchboard.coordinator_daemon_state.v1"
CONTROL_SCHEMA = "switchboard.coordinator_daemon_control.v1"
RUN_SCHEMA = "switchboard.coordinator_daemon_run.v1"
LEADER_RESOURCE_TYPE = "coordinator_leader"
TERMINAL_DELIVERABLE_STATUSES = frozenset({
    "archived", "cancelled", "canceled", "complete", "completed", "done",
})


def enabled_from_env(name: str, default: bool = False,
                     environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: Any, *, upper: bool = False) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else (value or [])
    clean = {str(item).strip() for item in raw if str(item).strip()}
    if upper:
        clean = {item.upper() for item in clean}
    return tuple(sorted(clean))


@dataclass(frozen=True)
class DaemonConfig:
    profile_id: str = "autopilot-default"
    projects: tuple[str, ...] = ("switchboard",)
    allowed_lanes: tuple[str, ...] = ()
    actor: str = "switchboard/coordinator-autopilot"
    act: bool = False
    poll_seconds: int = 30
    heartbeat_seconds: int = 30
    lease_ttl_seconds: int = 120
    # Batch enough selected work to cover the product's 60-deliverable fanout
    # scenario in one sweep. Execution capacity and fleet budget controls remain
    # the admission boundary; these are scheduling batch sizes, not concurrency
    # limits.
    max_deliverables_per_tick: int = 64
    max_tasks_per_scope_tick: int = 64
    lifecycle_enabled: bool = True
    review_reserved_slots: int = 1

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "DaemonConfig":
        env = os.environ if environ is None else environ
        heartbeat = max(10, int(env.get("PM_COORDINATOR_AUTOPILOT_HEARTBEAT_SECONDS", "30")))
        return cls(
            profile_id=(env.get("PM_COORDINATOR_AUTOPILOT_PROFILE")
                        or "autopilot-default").strip(),
            projects=_csv(env.get("PM_COORDINATOR_AUTOPILOT_PROJECTS", "switchboard")),
            allowed_lanes=_csv(env.get("PM_COORDINATOR_AUTOPILOT_LANES", ""), upper=True),
            actor=(env.get("PM_COORDINATOR_AUTOPILOT_ACTOR")
                   or "switchboard/coordinator-autopilot").strip(),
            act=enabled_from_env("PM_COORDINATOR_AUTOPILOT_ACT", False, env),
            poll_seconds=max(1, int(env.get("PM_COORDINATOR_AUTOPILOT_POLL_SECONDS", "30"))),
            heartbeat_seconds=heartbeat,
            lease_ttl_seconds=max(
                heartbeat * 3,
                int(env.get("PM_COORDINATOR_AUTOPILOT_LEASE_TTL_SECONDS", "120")),
            ),
            max_deliverables_per_tick=max(
                1, int(env.get("PM_COORDINATOR_AUTOPILOT_MAX_DELIVERABLES", "64"))),
            max_tasks_per_scope_tick=max(
                1, int(env.get("PM_COORDINATOR_AUTOPILOT_MAX_TASKS_PER_SCOPE", "64"))),
            lifecycle_enabled=enabled_from_env(
                "PM_COORDINATOR_AUTOPILOT_LIFECYCLE", True, env),
            review_reserved_slots=max(
                1, int(env.get("PM_COORDINATOR_REVIEW_RESERVED_SLOTS", "1"))),
        )


def _state_key(profile_id: str) -> str:
    return f"coordinator_daemon.state:{profile_id}"


def _control_key(profile_id: str) -> str:
    return f"coordinator_daemon.control:{profile_id}"


def get_control(store_mod: Any, project: str, profile_id: str) -> Dict[str, Any]:
    value = store_mod.get_meta(_control_key(profile_id), {}, project=project) or {}
    return {
        "schema": CONTROL_SCHEMA,
        "profile_id": profile_id,
        "project_id": project,
        "paused": bool(value.get("paused")),
        "paused_lanes": list(_csv(value.get("paused_lanes"), upper=True)),
        "generation": int(value.get("generation") or 0),
        "updated_at": value.get("updated_at"),
        "updated_by": value.get("updated_by"),
    }


def set_control(store_mod: Any, project: str, profile_id: str, *, actor: str,
                paused: Optional[bool] = None, pause_lane: str = "",
                resume_lane: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    observed = time.time() if now is None else float(now)
    current = get_control(store_mod, project, profile_id)
    lanes = set(current.get("paused_lanes") or [])
    if pause_lane:
        lanes.add(pause_lane.strip().upper())
    if resume_lane:
        lanes.discard(resume_lane.strip().upper())
    if paused is not None:
        current["paused"] = bool(paused)
    current.update({
        "paused_lanes": sorted(lanes),
        "generation": int(current.get("generation") or 0) + 1,
        "updated_at": observed,
        "updated_by": actor,
    })
    store_mod.set_meta(_control_key(profile_id), current, project=project)
    store_mod.append_activity(
        "coordinator.daemon.control", actor, current, project=project)
    return current


class CoordinatorDaemon:
    def __init__(self, config: DaemonConfig, *, store_mod: Any = None,
                 instance_id: str = "", clock: Any = None, sleeper: Any = None,
                 lifecycle_runner: Any = None) -> None:
        if store_mod is None:
            import store as store_mod
        self.store = store_mod
        self.config = config
        self.instance_id = instance_id or uuid.uuid4().hex
        self.agent_id = f"{config.actor}/{self.instance_id[:12]}"
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self.lifecycle_runner = lifecycle_runner
        self._stop = False

    def _drain_lifecycle(self, project: str) -> Dict[str, Any]:
        """Publish an auditable zero-work-driving action census."""
        return {
            "status": "janitor_only",
            "decision_stream": [],
            "action_census": {
                "cleanup": 0,
                "start_task": 0,
                "review": 0,
                "remediation": 0,
                "merge": 0,
                "retry": 0,
                "message": 0,
                "send_agent_message": 0,
                "work_instruction": 0,
                "acknowledgement_monitor": 0,
                "agent_directed_message": 0,
            },
        }

    def _drive_scope(self, project: str, scope: Dict[str, Any]) -> Dict[str, Any]:
        """One scope's action for this tick.

        The base daemon is a janitor (ADR-0008 W3): it observes and closes
        terminal scopes but drives no lifecycle work. ACT=1 swaps in
        ScopedCompletionCoordinator, which overrides this to drive the scope's
        task(s) through the completion owner.
        """
        return self._janitor_scope(project, scope)

    def _janitor_scope(self, project: str, scope: Dict[str, Any]) -> Dict[str, Any]:
        """Close terminal scope records without driving task lifecycle work."""
        deliverable_id = str(scope.get("deliverable_id") or "")
        mission_status = self.store.get_mission_status(
            project=project, deliverable_id=deliverable_id)
        if mission_status.get("error"):
            return {"status": "observed_error", "error": mission_status.get("error")}
        if not self._scope_complete(scope, mission_status):
            return {"status": "observed_active"}
        result = {
            "status": "completed",
            "scope_id": scope.get("scope_id"),
            "deliverable_id": deliverable_id,
            "janitor": True,
        }
        self.store.update_autopilot_scope(
            scope["scope_id"], project=project, status="completed",
            last_result=result, ticked_at=float(self.clock()))
        return result

    def _state(self, project: str) -> Dict[str, Any]:
        saved = self.store.get_meta(
            _state_key(self.config.profile_id), {}, project=project) or {}
        return {
            "schema": STATE_SCHEMA,
            "profile_id": self.config.profile_id,
            "project_id": project,
            "sequence": int(saved.get("sequence") or 0),
            "last_deliverable_id": saved.get("last_deliverable_id") or "",
            "last_scope_id": saved.get("last_scope_id") or "",
            "last_activity_cursor": int(saved.get("last_activity_cursor") or 0),
            "leader_lease_id": saved.get("leader_lease_id") or "",
            "leader_expires_at": saved.get("leader_expires_at"),
            "instance_id": saved.get("instance_id") or "",
            "status": saved.get("status") or "new",
            "last_heartbeat_at": saved.get("last_heartbeat_at"),
            "last_result": saved.get("last_result") or {},
        }

    def _save_state(self, project: str, state: Dict[str, Any]) -> None:
        state = {**state, "schema": STATE_SCHEMA, "profile_id": self.config.profile_id,
                 "project_id": project}
        self.store.set_meta(_state_key(self.config.profile_id), state, project=project)

    def _register_or_heartbeat(self, project: str) -> Dict[str, Any]:
        presence = self.store.heartbeat(self.agent_id, project=project, actor=self.config.actor)
        if not presence.get("error"):
            return presence
        return self.store.register_agent(
            self.agent_id,
            runtime="coordinator-daemon",
            lane="COORD",
            ttl_s=max(self.config.heartbeat_seconds * 3, 30),
            control={
                "mode": "janitor",
                "profile_id": self.config.profile_id,
                "project_allowlist": list(self.config.projects),
                "lane_allowlist": list(self.config.allowed_lanes),
                "acting": False,
            },
            actor=self.config.actor,
            project=project,
        )

    def _acquire_leadership(self, project: str, state: Dict[str, Any]) -> Dict[str, Any]:
        now = float(self.clock())
        lease_name = f"{self.config.profile_id}:{project}"
        lease = self.store.claim_resources(
            self.agent_id,
            LEADER_RESOURCE_TYPE,
            [lease_name],
            task_id="COORD-8",
            ttl_seconds=self.config.lease_ttl_seconds,
            actor=self.config.actor,
            idem_key=f"coord8-leader:{self.instance_id}:{project}:{uuid.uuid4().hex}",
            project=project,
        )
        if lease.get("conflict") or lease.get("error"):
            # A standby must not overwrite the active leader's durable cursor or
            # status. Its presence heartbeat plus the lease conflict is enough.
            return {"leader": False, "lease": lease}
        previous = state.get("leader_lease_id")
        state.update({
            "leader_lease_id": lease.get("lease_id"),
            "leader_expires_at": lease.get("expires_at"),
            "instance_id": self.instance_id,
            "last_heartbeat_at": now,
        })
        self._save_state(project, state)
        if previous and previous != lease.get("lease_id"):
            self.store.release_resource_lease(
                previous, actor=self.config.actor, project=project)
        return {"leader": True, "lease": lease}

    def _ordered_scopes(self, project: str, state: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Round-robin only through durable scopes an operator explicitly started."""
        rows = list(self.store.list_autopilot_scopes(
            project=project, profile_id=self.config.profile_id,
            status="active", limit=2000))
        rows.sort(key=lambda row: str(row.get("scope_id") or ""))
        last = state.get("last_scope_id") or ""
        ids = [str(row.get("scope_id") or "") for row in rows]
        if last in ids:
            index = ids.index(last) + 1
            rows = rows[index:] + rows[:index]
        return rows[:self.config.max_deliverables_per_tick]

    @staticmethod
    def _task_detail(mission_status: Dict[str, Any], task_id: str,
                     task_project: str = "") -> Dict[str, Any]:
        target = str(task_id or "").upper()
        for link in mission_status.get("linked_tasks") or []:
            if str(link.get("task_id") or "").upper() != target:
                continue
            if task_project and str(link.get("project_id") or "") != task_project:
                continue
            return link.get("task_detail") or {}
        return {}

    @staticmethod
    def _terminal_task(detail: Dict[str, Any]) -> bool:
        return bool(detail.get("status") == "Done"
                    and (detail.get("provenance") or {}).get("terminal"))

    @staticmethod
    def _scope_dispatch_links(scope: Dict[str, Any],
                              mission_status: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Return links explicitly covered by one operator-started scope.

        Generic mission planning deliberately excludes non-blocking links unless
        their metadata opts in.  A live deliverable scope is the operator's
        explicit opt-in for ordinary flow roles, though: clicking ``Start
        deliverable`` must not arm an inert scope merely because older/default
        links were stored as ``contributes`` with ``blocks_deliverable=false``.
        Context roles and skipped milestones remain excluded by their distinct
        reason codes.
        """
        links = list((mission_status.get("dispatch_scope") or {}).get("links") or [])
        if scope.get("scope_type") == "task":
            task_id = str(scope.get("task_id") or "").upper()
            task_project = str(scope.get("task_project") or "")
            return [
                row for row in links
                if str(row.get("task_id") or "").upper() == task_id
                and (not task_project or str(row.get("project_id") or "") == task_project)
                and row.get("automatic_dispatch_eligible")
            ]
        return [
            row for row in links
            if row.get("automatic_dispatch_eligible")
        ]

    def _scope_complete(self, scope: Dict[str, Any], mission_status: Dict[str, Any]) -> bool:
        if str((mission_status.get("deliverable") or {}).get("status") or "").lower() \
                in TERMINAL_DELIVERABLE_STATUSES:
            return True
        if scope.get("scope_type") == "task":
            return self._terminal_task(self._task_detail(
                mission_status, scope.get("task_id") or "",
                scope.get("task_project") or ""))
        eligible = {
            (str(row.get("project_id") or ""), str(row.get("task_id") or "").upper())
            for row in self._scope_dispatch_links(scope, mission_status)
        }
        if not eligible:
            return False
        return all(self._terminal_task(self._task_detail(mission_status, task_id, task_project))
                   for task_project, task_id in eligible)

    @staticmethod
    def _task_ready_for_dispatch(detail: Dict[str, Any],
                                 route: str | None = None) -> bool:
        # Selection is route-aware, not status-only: Blocked(route=remediation)
        # must stay dispatchable while Blocked(route=human) must not. See
        # switchboard.domain.completion.routing for the shared contract.
        return completion_routing.task_ready_for_dispatch(detail, route=route)

    def _scope_candidates(self, scope: Dict[str, Any],
                          mission_status: Dict[str, Any]) -> list[Dict[str, Any]]:
        if scope.get("scope_type") == "task":
            task_id = str(scope.get("task_id") or "").upper()
            task_project = str(scope.get("task_project") or "")
            dispatch_link = next(iter(self._scope_dispatch_links(
                scope, mission_status)), None)
            if dispatch_link is None:
                return []
            detail = self._task_detail(
                mission_status, task_id, task_project)
            if not detail or self._terminal_task(detail):
                return []
            return [{"task_id": task_id, "task_project": task_project,
                     "action": "target_task"}]
        eligible = {
            (str(row.get("project_id") or ""), str(row.get("task_id") or "").upper())
            for row in self._scope_dispatch_links(scope, mission_status)
        }
        by_task: Dict[tuple[str, str], Dict[str, Any]] = {}
        for action in mission_status.get("next_actions") or []:
            task_id = str(action.get("task_id") or "").upper()
            key = (str(action.get("project_id") or ""), task_id)
            if key not in eligible:
                continue
            if action.get("action") not in {
                "claim_task", "resume_or_claim", "verify_merge_provenance",
            }:
                continue
            by_task.setdefault(key, dict(action))
        # ``next_actions`` is the generic planner's view and intentionally omits
        # non-blocking links.  The operator-started deliverable scope is the
        # explicit opt-in, so target any covered non-terminal task that the
        # generic plan did not enumerate and let the exact-task coordinator
        # perform the normal dependency/claim/policy checks.
        for task_project, task_id in sorted(eligible):
            key = (task_project, task_id)
            if key in by_task:
                continue
            detail = self._task_detail(mission_status, task_id, task_project)
            if not detail or self._terminal_task(detail):
                continue
            # The mission projection rarely carries the completion run, so a
            # Blocked candidate resolves its route from the durable authority.
            route = completion_routing.resolve_completion_route(
                detail, store=self.store, project=task_project)
            if not self._task_ready_for_dispatch(detail, route):
                continue
            by_task[key] = {
                "action": "target_task",
                "task_id": task_id,
                "task_project": task_project,
                "project_id": task_project,
                "completion_route": route or None,
            }
        return [by_task[key] for key in sorted(by_task)][:self.config.max_tasks_per_scope_tick]

    def _candidate_revision(self, mission_status: Dict[str, Any], candidate: Dict[str, Any]) -> str:
        detail = self._task_detail(
            mission_status, candidate.get("task_id") or "",
            candidate.get("task_project") or candidate.get("project_id") or "")
        snapshot = {
            "status": detail.get("status"),
            "claims": sorted(str(row.get("claim_id") or "")
                             for row in detail.get("active_claims") or []),
            "dependency": detail.get("dependency_state") or {},
            "provenance_terminal": (detail.get("provenance") or {}).get("terminal"),
            "action": candidate.get("action"),
        }
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def _wake_generation(self, project: str, deliverable_id: str,
                         task_id: str) -> int:
        """Return the completed dispatch generation for an exact task.

        The task/dependency snapshot does not change when a host startup dies before
        it creates a claim.  Keying daemon ticks only from that snapshot therefore
        replays the old coordinator result forever.  Terminal exact wakes are the
        durable retry boundary; active wakes deliberately do not advance it so crash
        replay still deduplicates an in-flight launch.

        BUG-138: Connect dispatch wakes carry NO deliverable_id in their selector
        (agent/lane/provider/runtime/task only), so filtering on it counted nothing
        and live scopes replayed ``wake-generation-0`` into "idempotency conflict"
        on every tick.  Count terminal wakes for the exact task whether or not the
        selector names this deliverable; exclude only wakes explicitly bound to a
        DIFFERENT deliverable.
        """
        list_wakes = getattr(self.store, "list_wake_intents", None)
        if not callable(list_wakes):
            return 0
        try:
            rows = list_wakes(project=project, task_id=task_id)
        except Exception:
            return 0
        if not isinstance(rows, list):
            return 0
        terminal = {"completed", "failed", "cancelled", "expired"}
        return sum(
            1 for row in rows
            if str(row.get("task_id") or "") == task_id
            and str((row.get("selector") or {}).get("deliverable_id") or "")
            in ("", deliverable_id)
            and str(row.get("status") or "") in terminal
        )

    def tick_project(self, project: str) -> Dict[str, Any]:
        if project not in self.config.projects:
            return {"project": project, "status": "denied_project"}
        now = float(self.clock())
        self._register_or_heartbeat(project)
        state = self._state(project)
        leadership = self._acquire_leadership(project, state)
        if not leadership.get("leader"):
            return {"project": project, "status": "standby", **leadership}
        control = get_control(self.store, project, self.config.profile_id)
        if control.get("paused"):
            state.update({"status": "paused", "last_heartbeat_at": now,
                          "last_result": {"control": control}})
            self._save_state(project, state)
            return {"project": project, "status": "paused", "control": control}

        receipts = []
        lifecycle = self._drain_lifecycle(project)
        decision_stream = list(lifecycle.get("decision_stream") or [])
        for scope in self._ordered_scopes(project, state):
            # Controls are re-read between effects so an operator pause is bounded
            # by one scope tick rather than the whole project sweep.
            control = get_control(self.store, project, self.config.profile_id)
            if control.get("paused"):
                break
            deliverable_id = str(scope.get("deliverable_id") or "")
            sequence = int(state.get("sequence") or 0)
            result = self._drive_scope(project, scope)
            receipts.append({
                "scope_id": scope.get("scope_id"),
                "scope_type": scope.get("scope_type"),
                "deliverable_id": deliverable_id,
                "status": result.get("status"),
                "task_id": scope.get("task_id") or None,
                "candidate_count": int(result.get("candidate_count") or 0),
                "task_receipts": result.get("receipts") or result.get("task_receipts") or [],
                "error": result.get("error"),
            })
            # Persist only after the scope's idempotent task ticks return. A crash
            # before this write reuses the same candidate revision keys on restart.
            state.update({
                "sequence": sequence + 1,
                "last_deliverable_id": deliverable_id,
                "last_scope_id": scope.get("scope_id") or "",
                "last_activity_cursor": int(self.store._activity_cursor(project)),
                "last_heartbeat_at": float(self.clock()),
                "status": "running",
                "last_result": receipts[-1],
            })
            self._save_state(project, state)

        status = "running" if receipts else "idle"
        lifecycle["action_census"]["cleanup"] = sum(
            1 for receipt in receipts if receipt.get("status") == "completed")
        state.update({"status": status, "last_heartbeat_at": float(self.clock()),
                      "last_result": receipts[-1] if receipts else {"control": control}})
        self._save_state(project, state)
        self.store.append_activity(
            "coordinator.daemon.tick", self.config.actor,
            {"schema": RUN_SCHEMA, "profile_id": self.config.profile_id,
             "instance_id": self.instance_id, "project": project,
             "status": status, "acting": self.config.act,
             "lifecycle_status": lifecycle.get("status"),
             "decision_stream": decision_stream,
             "receipt_count": len(receipts),
             "scope_ids": [row["scope_id"] for row in receipts],
             "deliverable_ids": [row["deliverable_id"] for row in receipts],
             "sequence": state.get("sequence")},
            project=project,
        )
        return {"schema": RUN_SCHEMA, "project": project, "status": status,
                "leader": True, "acting": self.config.act, "receipts": receipts,
                "lifecycle": lifecycle, "decision_stream": decision_stream,
                "state": state}

    def tick(self) -> Dict[str, Any]:
        receipts = [self.tick_project(project) for project in self.config.projects]
        return {"schema": RUN_SCHEMA, "profile_id": self.config.profile_id,
                "instance_id": self.instance_id, "projects": receipts,
                "ok": bool(receipts) and all(
                    row.get("status") not in {"denied_project"} for row in receipts)}

    def stop(self, *_args: Any) -> None:
        self._stop = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self._stop:
            result = self.tick()
            print(json.dumps(result, sort_keys=True, default=str), flush=True)
            if not result.get("ok"):
                raise RuntimeError("coordinator daemon tick failed closed")
            if not self._stop:
                self.sleeper(self.config.poll_seconds)


def _projects(config: DaemonConfig, selected: Iterable[str]) -> tuple[str, ...]:
    values = tuple(project for project in selected if project)
    return values or config.projects


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="COORD-8 durable deliverable autopilot")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--project", action="append", default=[])
    for name in ("pause-project", "resume-project", "pause-lane", "resume-lane"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True)
        if name.endswith("lane"):
            command.add_argument("--lane", required=True)
    args = parser.parse_args(argv)
    import store

    config = DaemonConfig.from_env()
    if config.act:
        # ACT=1: this process owns and drives operator-armed scopes. ACT=0 stays
        # the janitor above. One switch, matching docs/COORDINATOR-AUTOPILOT.md.
        from scoped_completion_coordinator import ScopedCompletionCoordinator
        daemon = ScopedCompletionCoordinator(
            config, store_mod=store,
            agent_id=f"{config.actor}/{uuid.uuid4().hex[:12]}")
    else:
        daemon = CoordinatorDaemon(config, store_mod=store)
    if args.command == "run":
        if args.once:
            print(json.dumps(daemon.tick(), indent=2, sort_keys=True, default=str))
        else:
            daemon.run_forever()
        return 0
    if args.command == "status":
        rows = []
        for project in _projects(config, args.project):
            rows.append({
                "project": project,
                "control": get_control(store, project, config.profile_id),
                "state": store.get_meta(_state_key(config.profile_id), {}, project=project),
            })
        print(json.dumps({"schema": STATE_SCHEMA, "projects": rows}, indent=2,
                         sort_keys=True, default=str))
        return 0
    kwargs: Dict[str, Any] = {}
    if args.command == "pause-project":
        kwargs["paused"] = True
    elif args.command == "resume-project":
        kwargs["paused"] = False
    elif args.command == "pause-lane":
        kwargs["pause_lane"] = args.lane
    elif args.command == "resume-lane":
        kwargs["resume_lane"] = args.lane
    result = set_control(
        store, args.project, config.profile_id, actor=config.actor, **kwargs)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
