"""T0 read-only coordinator audit loop (COORD-2).

The observation and planning core only uses SQLite ``mode=ro`` plus
``PRAGMA query_only``.  It returns recommendations; it never executes them.
The scheduled wrapper may append one bounded plan artifact to the project's
activity log, which is the sole persistent effect allowed at T0.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import scripts.switchboard_path  # noqa: F401 -- make src/switchboard importable
from switchboard.storage.repositories.project_impact import ReadOnlyDatabase


SNAPSHOT_SCHEMA = "switchboard.coordinator_audit_snapshot.v1"
PLAN_SCHEMA = "switchboard.coordinator_audit_plan.v1"
RUN_SCHEMA = "switchboard.coordinator_audit_run.v1"
ACTIVITY_SCHEMA = "switchboard.coordinator_audit_activity.v1"
ACTIVITY_KIND = "coordinator.audit.plan"
TIER = "T0"
CATEGORIES = ("assignment", "review", "merge", "reconcile", "stale_claim", "escalation")
TERMINAL_STATUSES = {"done", "archived", "cancelled", "canceled"}
ACTIVE_SESSION_STATUSES = {"active", "proposed"}
GREEN_CI = {"success", "succeeded", "passed", "pass", "green"}
RED_CI = {"failure", "failed", "error", "cancelled", "canceled", "timed_out"}
PENDING_CI = {"requested", "mirrored", "triggered", "pending", "queued", "running", "in_progress"}
HUMAN_GATE_MARKERS = (
    "human_gate required", "human_gate:required", "human_gate=true",
)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if value in (None, ""):
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _depends_on(value: Any) -> list[str]:
    if isinstance(value, list):
        parsed = value
    else:
        try:
            candidate = json.loads(value) if isinstance(value, str) else None
        except (ValueError, json.JSONDecodeError):
            candidate = None
        parsed = candidate if isinstance(candidate, list) else None
    if parsed is not None:
        return sorted({str(item).strip().upper() for item in parsed if str(item).strip()})
    return sorted({part.strip().upper() for part in re.split(r"[,\s]+", str(value or ""))
                   if part.strip()})


def _human_gate(task: Mapping[str, Any]) -> bool:
    gate = task.get("human_gate")
    if isinstance(gate, Mapping):
        required = bool(gate.get("required") or gate.get("blocked"))
        return required and not bool(gate.get("approved"))
    if isinstance(gate, bool):
        return gate
    text = " ".join(str(task.get(key) or "") for key in (
        "title", "description", "owner_person_or_role", "phase",
        "entry_criteria", "exit_criteria",
    )).lower()
    return any(marker in text for marker in HUMAN_GATE_MARKERS)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_rows(db: ReadOnlyDatabase, table: str, columns: str,
               *, where: str = "", order_by: str = "") -> list[dict[str, Any]]:
    return db.rows(table, columns, where=where, order_by=order_by)


def unavailable_snapshot(project: str, error_code: str, *, now: float | None = None,
                         error_type: str | None = None) -> dict[str, Any]:
    observed_at = float(time.time() if now is None else now)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "project": project,
        "observed_at": observed_at,
        "read_status": {
            "available": False,
            "error_code": error_code,
            "error_type": error_type,
            "mode": "sqlite_mode_ro_query_only",
        },
        "meta": {},
        "tasks": [],
        "git_states": [],
        "ci_runs": [],
        "agents": [],
        "hosts": [],
        "claims": [],
        "file_leases": [],
        "resource_leases": [],
        "monitors": [],
        "work_sessions": [],
        "reconcile_activity": [],
    }


def collect_snapshot(db_path: str, project: str, *, now: float | None = None) -> dict[str, Any]:
    """Read one project's coordinator inputs without opening a writable connection."""
    observed_at = float(time.time() if now is None else now)
    snapshot = unavailable_snapshot(project, "database_unavailable", now=observed_at)
    with ReadOnlyDatabase(db_path) as db:
        if not db.read_status()["available"]:
            status = db.read_status()
            snapshot["read_status"] = {**status, "mode": "sqlite_mode_ro_query_only"}
            return snapshot

        snapshot.update({
            "read_status": {
                "available": True,
                "error_code": None,
                "error_type": None,
                "mode": "sqlite_mode_ro_query_only",
            },
            "meta": {
                "canonical_main_sha": db.meta("canonical_main_sha", ""),
                "github_repo": db.meta("github_repo", ""),
            },
            "tasks": _read_rows(
                db, "tasks",
                "task_id, title, description, owner_person_or_role, assignee, phase, status, "
                "depends_on, entry_criteria, exit_criteria, risk_level, is_blocking, "
                "sort_order, updated_at",
                order_by="sort_order, task_id",
            ),
            "git_states": _read_rows(
                db, "task_git_state",
                "task_id, branch, head_sha, pushed_at, pr_number, pr_url, merged_sha, "
                "merged_at, in_main_content, published_ref, last_reconciled_at, "
                "evidence_json, updated_at",
                order_by="task_id",
            ),
            "ci_runs": _read_rows(
                db, "external_ci_runs",
                "run_id, source_sha, status_context, status, conclusion, run_url, failure_class, "
                "failure_reason, task_id, claim_id, agent_id, requested_at, completed_at, updated_at",
                order_by="updated_at DESC, run_id DESC",
            ),
            "agents": _read_rows(
                db, "agent_presence",
                "agent_id, runtime, model, lane, task_id, control, principal_id, "
                "registered_at, heartbeat_at, ttl_s",
                order_by="agent_id",
            ),
            "hosts": _read_rows(
                db, "agent_hosts",
                "host_id, hostname, agent_host_version, repo_root, runtimes_json, limits_json, "
                "capacity_json, principal_id, registered_at, heartbeat_at, heartbeat_ttl_s, "
                "status, last_error",
                order_by="host_id",
            ),
            "claims": _read_rows(
                db, "task_claims",
                "id, task_id, agent_id, principal_id, status, claimed_at, expires_at, "
                "completed_at, abandon_reason",
                order_by="claimed_at DESC, id",
            ),
            "file_leases": _read_rows(
                db, "file_leases",
                "id, agent_id, task_id, files, claimed_at, ttl_minutes, released_at",
                order_by="claimed_at DESC, id",
            ),
            "resource_leases": _read_rows(
                db, "resource_leases",
                "id, agent_id, principal_id, task_id, resource_type, names, claimed_at, "
                "ttl_seconds, released_at",
                order_by="claimed_at DESC, id",
            ),
            "monitors": _read_rows(
                db, "coordination_monitors",
                "id, kind, target_type, target_id, task_id, owner_agent, subject_agent, "
                "status, deadline, condition_json, on_timeout_json, result_json, created_at, "
                "updated_at, last_checked_at, fired_at, resolved_at",
                order_by="updated_at DESC, id",
            ),
            "work_sessions": _read_rows(
                db, "work_sessions",
                "work_session_id, task_id, claim_id, agent_id, runtime, repo_role, repo, "
                "default_branch, branch, upstream, base_sha, head_sha, storage_mode, status, "
                "dirty_status, conflict_marker_count, hygiene_json, policy_profile, "
                "updated_at, expires_at",
                order_by="updated_at DESC, work_session_id",
            ),
            "reconcile_activity": _read_rows(
                db, "activity", "id, kind, payload, created_at",
                where="kind LIKE 'reconcile.%' OR kind='reconcile'",
                order_by="created_at DESC, id DESC",
            )[:20],
        })
        final_status = db.read_status()
        if not final_status["available"]:
            snapshot["read_status"] = {**final_status, "mode": "sqlite_mode_ro_query_only"}
    return snapshot


def _ci_state(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "missing"
    conclusion = _status(row.get("conclusion"))
    status = _status(row.get("status"))
    if conclusion in GREEN_CI:
        return "green"
    if conclusion in RED_CI or status in RED_CI:
        return "red"
    if status in PENDING_CI or not conclusion:
        return "pending"
    return "unknown"


def _priority(score: int) -> str:
    if score >= 90:
        return "critical"
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def _task_bonus(task: Mapping[str, Any]) -> int:
    risk = _status(task.get("risk_level"))
    return (10 if task.get("is_blocking") else 0) + {
        "critical": 15, "high": 10, "medium": 5,
    }.get(risk, 0)


def build_plan(snapshot: Mapping[str, Any], *, max_recommendations: int = 100,
               reconcile_stale_seconds: int = 900) -> dict[str, Any]:
    """Build a ranked output-only plan from a previously collected snapshot."""
    project = str(snapshot.get("project") or "")
    observed_at = float(snapshot.get("observed_at") or time.time())
    recommendations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def recommend(category: str, action: str, target_type: str, target_id: str,
                  score: int, reason: str, evidence: Mapping[str, Any] | None = None,
                  escalation_class: str | None = None) -> None:
        key = (category, target_id, action)
        if key in seen:
            return
        seen.add(key)
        stable = {
            "project": project,
            "category": category,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "reason": reason,
        }
        row = {
            "recommendation_id": f"coord-rec-{_digest(stable)[:16]}",
            **stable,
            "score": max(0, min(100, int(score))),
            "priority": _priority(score),
            "reason": reason,
            "evidence": dict(evidence or {}),
            "mutates": False,
        }
        if escalation_class:
            row["escalation_class"] = escalation_class
        recommendations.append(row)

    read_status = dict(snapshot.get("read_status") or {})
    if not read_status.get("available"):
        recommend(
            "escalation", "restore_audit_read_path", "project", project, 100,
            "The coordinator cannot read the project database and must fail closed.",
            {"error_code": read_status.get("error_code"),
             "error_type": read_status.get("error_type"),
             "read_mode": read_status.get("mode")},
            "failed_gate",
        )

    tasks = [dict(row) for row in snapshot.get("tasks") or []]
    by_task = {str(row.get("task_id") or "").upper(): row for row in tasks}
    git_by_task = {str(row.get("task_id") or "").upper(): dict(row)
                   for row in snapshot.get("git_states") or []}

    latest_ci: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("ci_runs") or []:
        task_id = str(row.get("task_id") or "").upper()
        if task_id and task_id not in latest_ci:
            latest_ci[task_id] = dict(row)

    active_claims: dict[str, list[dict[str, Any]]] = {}
    stale_agent_ids: set[str] = set()
    active_agent_ids: set[str] = set()
    for row in snapshot.get("agents") or []:
        agent_id = str(row.get("agent_id") or "")
        expires = float(row.get("heartbeat_at") or 0) + int(row.get("ttl_s") or 120)
        (active_agent_ids if expires >= observed_at else stale_agent_ids).add(agent_id)

    active_hosts = []
    for row in snapshot.get("hosts") or []:
        expires = float(row.get("heartbeat_at") or 0) + int(row.get("heartbeat_ttl_s") or 60)
        if _status(row.get("status")) in {"online", "active", "ready"} and expires >= observed_at:
            active_hosts.append(row)

    for row in snapshot.get("claims") or []:
        claim = dict(row)
        if _status(claim.get("status")) != "active":
            continue
        claim_id = str(claim.get("id") or "")
        task_id = str(claim.get("task_id") or "").upper()
        expired = float(claim.get("expires_at") or 0) < observed_at
        owner_stale = str(claim.get("agent_id") or "") in stale_agent_ids
        if expired or owner_stale:
            recommend(
                "stale_claim", "inspect_and_release_stale_claim", "task_claim", claim_id,
                88 if expired else 82,
                "An active claim has expired." if expired else
                "An active claim is owned by an agent whose heartbeat expired.",
                {"task_id": task_id, "agent_id": claim.get("agent_id"),
                 "expires_at": claim.get("expires_at"), "claim_expired": expired,
                 "owner_heartbeat_expired": owner_stale},
                "unreachable_agent" if owner_stale else None,
            )
        else:
            active_claims.setdefault(task_id, []).append(claim)

    for row in snapshot.get("file_leases") or []:
        if row.get("released_at"):
            continue
        expires = float(row.get("claimed_at") or 0) + int(row.get("ttl_minutes") or 30) * 60
        if expires < observed_at:
            recommend(
                "stale_claim", "inspect_and_release_stale_file_lease", "file_lease",
                str(row.get("id") or ""), 78, "A file lease is past its TTL.",
                {"task_id": row.get("task_id"), "agent_id": row.get("agent_id"),
                 "expires_at": expires},
            )

    for row in snapshot.get("resource_leases") or []:
        if row.get("released_at"):
            continue
        expires = float(row.get("claimed_at") or 0) + int(row.get("ttl_seconds") or 1800)
        if expires < observed_at:
            recommend(
                "stale_claim", "inspect_and_release_stale_resource_lease", "resource_lease",
                str(row.get("id") or ""), 78, "A resource lease is past its TTL.",
                {"task_id": row.get("task_id"), "agent_id": row.get("agent_id"),
                 "resource_type": row.get("resource_type"), "expires_at": expires},
            )

    unsafe_tasks: set[str] = set()
    for row in snapshot.get("work_sessions") or []:
        if _status(row.get("status")) not in ACTIVE_SESSION_STATUSES:
            continue
        hygiene = _json(row.get("hygiene_json"), {})
        dirty = _status(row.get("dirty_status")) != "clean"
        conflicts = int(row.get("conflict_marker_count") or 0) > 0
        deny = list(hygiene.get("deny") or hygiene.get("denied") or [])
        if dirty or conflicts or deny:
            task_id = str(row.get("task_id") or "").upper()
            unsafe_tasks.add(task_id)
            recommend(
                "escalation", "repair_work_session_hygiene", "work_session",
                str(row.get("work_session_id") or ""), 90,
                "An active Work Session is unsafe; no merge should be recommended.",
                {"task_id": task_id, "dirty_status": row.get("dirty_status"),
                 "conflict_marker_count": row.get("conflict_marker_count"), "deny": deny},
                "failed_gate",
            )

    for row in snapshot.get("monitors") or []:
        status = _status(row.get("status"))
        deadline = float(row.get("deadline") or 0)
        fired = status == "fired"
        overdue = status in {"pending", "active"} and bool(deadline) and deadline < observed_at
        if fired or overdue:
            recommend(
                "escalation", "inspect_fired_or_overdue_monitor", "coordination_monitor",
                str(row.get("id") or ""), 92 if fired else 86,
                "A coordination monitor fired." if fired else
                "A coordination monitor is overdue without resolution.",
                {"task_id": row.get("task_id"), "monitor_kind": row.get("kind"),
                 "status": row.get("status"), "deadline": row.get("deadline"),
                 "target_type": row.get("target_type"), "target_id": row.get("target_id")},
                "unreachable_agent" if row.get("subject_agent") else "failed_gate",
            )

    ready_tasks: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "").upper()
        status = _status(task.get("status"))
        deps = _depends_on(task.get("depends_on"))
        missing_deps = [dep for dep in deps if dep not in by_task]
        open_deps = [dep for dep in deps if dep in by_task and
                     _status(by_task[dep].get("status")) not in TERMINAL_STATUSES]
        gate = _human_gate(task)
        git_state = git_by_task.get(task_id, {})
        evidence = _json(git_state.get("evidence_json"), {})
        has_provenance = bool(
            git_state.get("merged_sha") or git_state.get("in_main_content") or
            evidence.get("offline_evidence")
        )

        if missing_deps:
            recommend(
                "escalation", "repair_missing_dependency", "task", task_id, 94,
                "The task references dependencies that are absent from this project.",
                {"missing_dependencies": missing_deps}, "failed_gate",
            )

        if status == "done" and not has_provenance:
            recommend(
                "reconcile", "repair_done_provenance", "task", task_id, 96,
                "Done lacks merge/default-branch or verified offline provenance.",
                {"status": task.get("status"), "pr_number": git_state.get("pr_number"),
                 "merged_sha": git_state.get("merged_sha"),
                 "in_main_content": bool(git_state.get("in_main_content"))},
                "missing_provenance",
            )

        if status == "in progress" and git_state.get("pr_number") and not git_state.get("merged_sha"):
            recommend(
                "reconcile", "reconcile_open_pr_status", "task", task_id, 80,
                "The board says In Progress while recorded PR evidence says review has started.",
                {"status": task.get("status"), "pr_number": git_state.get("pr_number"),
                 "pr_url": git_state.get("pr_url")},
            )

        if status == "not started" and not missing_deps and not open_deps and not active_claims.get(task_id):
            if gate:
                recommend(
                    "escalation", "request_human_gate_decision", "task", task_id,
                    88 + _task_bonus(task),
                    "The ready task is explicitly human-gated and cannot be dispatched by T0.",
                    {"status": task.get("status"), "dependencies": deps,
                     "owner": task.get("owner_person_or_role")},
                    "human_gate_required",
                )
            else:
                ready_tasks.append(task)

        if status != "in review":
            continue
        ci = latest_ci.get(task_id)
        ci_state = _ci_state(ci)
        pr_number = git_state.get("pr_number")
        base_score = 72 + _task_bonus(task)
        if not pr_number:
            recommend(
                "review", "inspect_missing_pr_or_offline_evidence", "task", task_id,
                base_score, "In Review has no recorded PR; inspect its review/evidence path.",
                {"status": task.get("status"), "ci_state": ci_state},
            )
        elif ci_state == "red":
            recommend(
                "review", "fix_failed_ci_gate", "pull_request", str(pr_number),
                min(100, base_score + 10), "The latest board-recorded CI run is red.",
                {"task_id": task_id, "pr_url": git_state.get("pr_url"),
                 "run_id": ci.get("run_id") if ci else None,
                 "run_url": ci.get("run_url") if ci else None,
                 "failure_class": ci.get("failure_class") if ci else None},
            )
        elif ci_state in {"missing", "pending", "unknown"}:
            recommend(
                "review", "inspect_review_and_ci_state", "pull_request", str(pr_number),
                base_score, "The PR is recorded, but its latest required CI truth is not green.",
                {"task_id": task_id, "pr_url": git_state.get("pr_url"),
                 "ci_state": ci_state, "run_id": ci.get("run_id") if ci else None},
            )
        elif task_id in unsafe_tasks:
            recommend(
                "review", "repair_session_before_merge_gate", "pull_request", str(pr_number),
                base_score + 8, "CI is green, but an unsafe Work Session blocks merge evaluation.",
                {"task_id": task_id, "pr_url": git_state.get("pr_url")},
            )
        elif open_deps or missing_deps:
            recommend(
                "review", "hold_for_dependencies", "pull_request", str(pr_number),
                base_score, "CI is green, but task dependencies are not all complete.",
                {"task_id": task_id, "open_dependencies": open_deps,
                 "missing_dependencies": missing_deps},
            )
        else:
            recommend(
                "merge", "evaluate_safe_merge_gate", "pull_request", str(pr_number),
                68 + _task_bonus(task),
                "Board-recorded CI is green; run the canonical safe-merge gate before any merge.",
                {"task_id": task_id, "pr_url": git_state.get("pr_url"),
                 "head_sha": git_state.get("head_sha"),
                 "ci_run_id": ci.get("run_id") if ci else None,
                 "provider_truth": "not_read_live_by_t0_audit"},
            )

    if ready_tasks and not active_hosts:
        recommend(
            "escalation", "restore_eligible_agent_host", "project", project, 91,
            "Ready work exists, but no project host has a current online heartbeat.",
            {"ready_task_ids": [str(task.get("task_id") or "") for task in ready_tasks[:20]],
             "ready_task_count": len(ready_tasks), "active_host_count": 0},
            "no_host",
        )
    for task in ready_tasks:
        task_id = str(task.get("task_id") or "").upper()
        recommend(
            "assignment", "consider_assignment", "task", task_id,
            55 + _task_bonus(task),
            "The task is Not Started, has no active claim, and its recorded dependencies are done.",
            {"dependencies": _depends_on(task.get("depends_on")),
             "active_host_count": len(active_hosts), "risk_level": task.get("risk_level"),
             "is_blocking": bool(task.get("is_blocking"))},
        )

    if read_status.get("available"):
        meta = dict(snapshot.get("meta") or {})
        if not meta.get("canonical_main_sha"):
            recommend(
                "reconcile", "refresh_canonical_main_sha", "project", project, 84,
                "The project has no recorded canonical default-branch SHA.",
                {"github_repo": meta.get("github_repo")}, "missing_provenance",
            )
        reconcile_times = [float(row.get("created_at") or 0)
                           for row in snapshot.get("reconcile_activity") or []]
        reconcile_times.extend(float(row.get("last_reconciled_at") or 0)
                               for row in snapshot.get("git_states") or [])
        last_reconcile_at = max(reconcile_times or [0])
        stale = not last_reconcile_at or observed_at - last_reconcile_at > reconcile_stale_seconds
        if stale:
            recommend(
                "reconcile", "run_operator_reconcile", "project", project, 70,
                "No recent reconcile evidence is recorded for this project.",
                {"last_reconcile_at": last_reconcile_at or None,
                 "stale_after_seconds": reconcile_stale_seconds},
            )

    recommendations.sort(key=lambda row: (
        -int(row["score"]), CATEGORIES.index(row["category"]),
        row["target_type"], row["target_id"], row["action"],
    ))
    total_before_limit = len(recommendations)
    recommendations = recommendations[:max(0, int(max_recommendations))]
    queues = {category: [] for category in CATEGORIES}
    for rank, row in enumerate(recommendations, start=1):
        row["rank"] = rank
        queues[row["category"]].append(row["recommendation_id"])

    snapshot_fingerprint = {
        key: snapshot.get(key) for key in (
            "project", "read_status", "meta", "tasks", "git_states", "ci_runs", "agents",
            "hosts", "claims", "file_leases", "resource_leases", "monitors", "work_sessions",
            "reconcile_activity",
        )
    }
    input_digest = _digest(snapshot_fingerprint)
    decision_digest = _digest({
        "input_digest": input_digest,
        "observed_at": observed_at,
        "recommendations": recommendations,
    })
    counts = {
        "tasks": len(tasks),
        "agents": len(snapshot.get("agents") or []),
        "active_agents": len(active_agent_ids),
        "hosts": len(snapshot.get("hosts") or []),
        "active_hosts": len(active_hosts),
        "active_claims": sum(len(rows) for rows in active_claims.values()),
        "open_prs": sum(1 for row in snapshot.get("git_states") or []
                        if row.get("pr_number") and not row.get("merged_sha")),
        "monitors": len(snapshot.get("monitors") or []),
    }
    return {
        "schema": PLAN_SCHEMA,
        "plan_id": f"coord-plan-{decision_digest[:20]}",
        "project": project,
        "tier": TIER,
        "generated_at": observed_at,
        "read_only": True,
        "input_digest": input_digest,
        "decision_digest": decision_digest,
        "inputs": counts,
        "summary": {
            "recommendation_count": len(recommendations),
            "total_before_limit": total_before_limit,
            "truncated": total_before_limit > len(recommendations),
            "queue_counts": {category: len(queues[category]) for category in CATEGORIES},
        },
        "queues": queues,
        "recommendations": recommendations,
        "effects": {
            "work_state_executed": [],
            "network_calls": [],
            "allowed_persistence": [ACTIVITY_KIND],
        },
        "caveats": [
            "PR and CI evidence is board-recorded state, not live provider readback.",
            "Merge entries recommend evaluating the safe-merge gate; they do not assert mergeability.",
        ],
    }


def audit_projects(projects: Iterable[str], *, actor: str = "switchboard/coordinator-t0",
                   persist: bool = True, now: float | None = None,
                   max_recommendations: int = 100, reconcile_stale_seconds: int = 900,
                   db_path_resolver: Callable[[str], str] | None = None,
                   activity_writer: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Audit selected projects and optionally append each plan to its own activity log."""
    if db_path_resolver is None or (persist and activity_writer is None):
        import store
        if db_path_resolver is None:
            def resolve_store_db_path(project: str) -> str:
                return str(store._resolve(project)["db"])

            db_path_resolver = resolve_store_db_path
        if activity_writer is None:
            activity_writer = store.append_activity

    receipts = []
    for raw_project in projects:
        project = str(raw_project or "").strip()
        if not project:
            continue
        try:
            db_path = db_path_resolver(project)  # type: ignore[misc]
            snapshot = collect_snapshot(db_path, project, now=now)
        except Exception as exc:  # resolution is also fail-closed and visible
            snapshot = unavailable_snapshot(
                project, "project_resolution_failed", now=now, error_type=type(exc).__name__)
        plan = build_plan(
            snapshot,
            max_recommendations=max_recommendations,
            reconcile_stale_seconds=reconcile_stale_seconds,
        )
        activity_id = None
        persistence_error = None
        if persist and activity_writer is not None:
            payload = {
                "schema": ACTIVITY_SCHEMA,
                "project": project,
                "tier": TIER,
                "actor": actor,
                "plan": plan,
                "work_state_effects": [],
            }
            try:
                activity_id = activity_writer(ACTIVITY_KIND, actor, payload, project=project)
            except Exception as exc:
                persistence_error = {
                    "error_code": "audit_log_write_failed",
                    "error_type": type(exc).__name__,
                }
        receipts.append({
            "project": project,
            "plan": plan,
            "audit_activity_id": activity_id,
            "persistence_error": persistence_error,
            "ok": bool(snapshot["read_status"].get("available")) and not persistence_error,
        })

    return {
        "schema": RUN_SCHEMA,
        "tier": TIER,
        "actor": actor,
        "persisted": bool(persist),
        "projects": receipts,
        "ok": bool(receipts) and all(row["ok"] for row in receipts),
        "effects": {
            "work_state_executed": [],
            "audit_activity_ids": [row["audit_activity_id"] for row in receipts
                                   if row["audit_activity_id"] is not None],
        },
    }


def enabled_from_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}
