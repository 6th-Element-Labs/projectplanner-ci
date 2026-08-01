#!/usr/bin/env python3
"""Run a bounded, audit-recorded Mission Bot v4 shadow comparison."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import store  # noqa: E402
from coordinator_daemon import DaemonConfig  # noqa: E402
from scoped_completion_coordinator import ScopedCompletionCoordinator  # noqa: E402
from switchboard.application.mission_bot_v4 import run_shadow_batch  # noqa: E402
from switchboard.storage.repositories.autopilot_scopes import (  # noqa: E402
    AUTOPILOT_SCOPE_AUTHORITY_SCHEMA,
    get_autopilot_scope,
    list_autopilot_scopes,
    scope_liveness,
)


def _authority(scope: dict[str, object]) -> dict[str, object]:
    return {
        "schema": AUTOPILOT_SCOPE_AUTHORITY_SCHEMA,
        **{
            key: scope.get(key)
            for key in (
                "scope_id", "holder_agent_id", "lease_id", "generation",
                "fence_epoch", "expires_at", "deliverable_id",
                "task_project", "task_id",
            )
        },
    }


def _candidate_ids(
    scope: dict[str, object],
    *,
    project: str,
    coordinator: ScopedCompletionCoordinator,
) -> list[str]:
    if scope.get("scope_type") == "task":
        task_id = str(scope.get("task_id") or "").strip().upper()
        return [task_id] if task_id else []
    deliverable_id = str(scope.get("deliverable_id") or "").strip()
    mission_status = store.get_mission_status(
        project=project,
        deliverable_id=deliverable_id,
    )
    if mission_status.get("error"):
        raise RuntimeError(str(mission_status["error"]))
    return [
        str(row.get("task_id") or "").strip().upper()
        for row in coordinator.scope_candidates(scope, mission_status)
        if str(row.get("task_id") or "").strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare v4 proposals with authoritative v1; execute no lifecycle effect.",
    )
    parser.add_argument("--project", default="switchboard")
    parser.add_argument("--actor", default="operator/mission-bot-v4-shadow")
    parser.add_argument(
        "--scope-id",
        action="append",
        default=[],
        help="exact active scope to compare; omitted means every active scope",
    )
    args = parser.parse_args()

    if args.scope_id:
        scopes = [
            get_autopilot_scope(value, project=args.project,
                                include_last_result=False)
            for value in args.scope_id
        ]
        missing = [value for value, row in zip(args.scope_id, scopes) if row is None]
        if missing:
            raise RuntimeError("autopilot_scope_not_found:" + ",".join(missing))
    else:
        scopes = list_autopilot_scopes(
            project=args.project,
            status="active",
            include_last_result=False,
        )
    coordinator = ScopedCompletionCoordinator(
        DaemonConfig(act=False),
        store_mod=store,
        agent_id="operator/mission-bot-v4-shadow",
    )
    batches = []
    scope_blockers = []
    for scope_value in scopes:
        scope = dict(scope_value or {})
        liveness = scope_liveness(scope)
        if liveness != "live":
            scope_blockers.append({
                "scope_id": scope.get("scope_id"),
                "reason": f"scope_{liveness}",
            })
            continue
        task_ids = _candidate_ids(
            scope, project=args.project, coordinator=coordinator,
        )
        if not task_ids:
            scope_blockers.append({
                "scope_id": scope.get("scope_id"),
                "reason": "no_v1_candidate_proposals",
            })
            continue
        batches.append(run_shadow_batch(
            task_ids,
            project=args.project,
            actor=args.actor,
            scope_authority=_authority(scope),
            scope_project=args.project,
        ))
    observations = [
        row for batch in batches for row in batch.get("observations", [])
    ]
    blocker_count = len(scope_blockers) + sum(
        int(batch.get("blocker_count") or 0) for batch in batches
    )
    result = {
        "schema": "switchboard.mission_bot_v4.shadow_scopes.v1",
        "project": args.project,
        "scope_count": len(scopes),
        "compared_scope_count": len(batches),
        "observation_count": len(observations),
        "scope_blockers": scope_blockers,
        "blocker_count": blocker_count + (1 if not scopes else 0),
        "release_blocked": blocker_count > 0 or not scopes or not observations,
        "cutover_authorized": False,
        "observations": observations,
    }
    result["passed"] = not result["release_blocked"]
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
