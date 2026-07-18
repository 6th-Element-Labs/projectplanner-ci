#!/usr/bin/env python3
"""Durable deliverable-scoped coordinator daemon (COORD-8).

The daemon is intentionally a thin control shell around the existing mission
coordinator. It adds a single-leader lease, presence heartbeat, project/lane
allowlists, durable pause controls, and a crash-safe per-deliverable cursor.
All task effects still pass through the existing claim/wake/review policy gates.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import signal
import time
from typing import Any, Dict, Iterable, Mapping, Optional
import uuid


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
    worker_agent_id: str = ""
    worker_runtime: str = "codex"
    act: bool = False
    poll_seconds: int = 30
    heartbeat_seconds: int = 30
    lease_ttl_seconds: int = 120
    max_deliverables_per_tick: int = 8

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
            worker_agent_id=(env.get("PM_COORDINATOR_AUTOPILOT_WORKER_AGENT") or "").strip(),
            worker_runtime=(env.get("PM_COORDINATOR_AUTOPILOT_WORKER_RUNTIME")
                            or "codex").strip(),
            act=enabled_from_env("PM_COORDINATOR_AUTOPILOT_ACT", False, env),
            poll_seconds=max(1, int(env.get("PM_COORDINATOR_AUTOPILOT_POLL_SECONDS", "30"))),
            heartbeat_seconds=heartbeat,
            lease_ttl_seconds=max(
                heartbeat * 3,
                int(env.get("PM_COORDINATOR_AUTOPILOT_LEASE_TTL_SECONDS", "120")),
            ),
            max_deliverables_per_tick=max(
                1, int(env.get("PM_COORDINATOR_AUTOPILOT_MAX_DELIVERABLES", "8"))),
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
                 instance_id: str = "", clock: Any = None, sleeper: Any = None) -> None:
        if store_mod is None:
            import store as store_mod
        self.store = store_mod
        self.config = config
        self.instance_id = instance_id or uuid.uuid4().hex
        self.agent_id = f"{config.actor}/{self.instance_id[:12]}"
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self._stop = False

    def _state(self, project: str) -> Dict[str, Any]:
        saved = self.store.get_meta(
            _state_key(self.config.profile_id), {}, project=project) or {}
        return {
            "schema": STATE_SCHEMA,
            "profile_id": self.config.profile_id,
            "project_id": project,
            "sequence": int(saved.get("sequence") or 0),
            "last_deliverable_id": saved.get("last_deliverable_id") or "",
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
                "mode": "deliverable_autopilot",
                "profile_id": self.config.profile_id,
                "project_allowlist": list(self.config.projects),
                "lane_allowlist": list(self.config.allowed_lanes),
                "acting": self.config.act,
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

    def _ordered_deliverables(self, project: str, state: Dict[str, Any]) -> list[Dict[str, Any]]:
        rows = [
            row for row in self.store.list_deliverables(
                project=project, include_task_snapshots=False)
            if str(row.get("status") or "").strip().lower()
            not in TERMINAL_DELIVERABLE_STATUSES
        ]
        rows.sort(key=lambda row: str(row.get("id") or ""))
        last = state.get("last_deliverable_id") or ""
        ids = [str(row.get("id") or "") for row in rows]
        if last in ids:
            index = ids.index(last) + 1
            rows = rows[index:] + rows[:index]
        return rows[:self.config.max_deliverables_per_tick]

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
        for deliverable in self._ordered_deliverables(project, state):
            # Controls are re-read between effects so an operator pause is bounded
            # by one deliverable tick rather than the whole project sweep.
            control = get_control(self.store, project, self.config.profile_id)
            if control.get("paused"):
                break
            deliverable_id = str(deliverable.get("id") or "")
            sequence = int(state.get("sequence") or 0)
            idem_key = (
                f"coord8:{self.config.profile_id}:{project}:{sequence}:{deliverable_id}"
            )
            policy = {
                "auto_refresh_brief": True,
                "auto_claim": bool(self.config.act and self.config.worker_agent_id),
                "auto_wake": bool(self.config.act and not self.config.worker_agent_id),
                "worker_agent_id": self.config.worker_agent_id,
                "worker_wake_selector": ({"runtime": self.config.worker_runtime}
                                         if self.config.act else {}),
                "allowed_lanes": list(self.config.allowed_lanes),
                "denied_lanes": control.get("paused_lanes") or [],
            }
            result = self.store.run_mission_coordinator_tick(
                project=project,
                deliverable_id=deliverable_id,
                coordinator_agent_id=self.agent_id,
                actor=self.config.actor,
                policy=policy,
                idem_key=idem_key,
            )
            receipts.append({
                "deliverable_id": deliverable_id,
                "status": result.get("status"),
                "decision_id": result.get("decision_id"),
                "dispatch": result.get("dispatch"),
                "error": result.get("error"),
                "idem_key": idem_key,
            })
            # Persist only after the idempotent deliverable tick returns. A crash
            # before this write reuses the same idem_key on restart.
            state.update({
                "sequence": sequence + 1,
                "last_deliverable_id": deliverable_id,
                "last_activity_cursor": int(self.store._activity_cursor(project)),
                "last_heartbeat_at": float(self.clock()),
                "status": "running",
                "last_result": receipts[-1],
            })
            self._save_state(project, state)

        status = "running" if receipts else "idle"
        state.update({"status": status, "last_heartbeat_at": float(self.clock()),
                      "last_result": receipts[-1] if receipts else {"control": control}})
        self._save_state(project, state)
        self.store.append_activity(
            "coordinator.daemon.tick", self.config.actor,
            {"schema": RUN_SCHEMA, "profile_id": self.config.profile_id,
             "instance_id": self.instance_id, "project": project,
             "status": status, "acting": self.config.act,
             "receipt_count": len(receipts),
             "deliverable_ids": [row["deliverable_id"] for row in receipts],
             "sequence": state.get("sequence")},
            project=project,
        )
        return {"schema": RUN_SCHEMA, "project": project, "status": status,
                "leader": True, "acting": self.config.act, "receipts": receipts,
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
