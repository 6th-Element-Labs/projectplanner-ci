"""Background job catalog and checkpoint runner for Switchboard (RECON-10).

Evaluates DBOS as invisible infrastructure for slow, resumable jobs only. The hot
coordination kernel (claims, leases, messages, activity append) stays on the
existing SQLite path. Jobs here are read-mostly projections, exports, and
reconciliation surfaces that can survive process restarts via step checkpoints.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

import store

SCHEMA = "switchboard.background_job_run.v1"
CATALOG_SCHEMA = "switchboard.background_job_catalog.v1"
EVAL_SCHEMA = "switchboard.dbos_runtime_evaluation.v1"

# Operations that must never be scheduled through this layer (borrowing map §4.4).
FORBIDDEN_HOT_PATH_OPERATIONS = frozenset({
    "claim_next",
    "claim_task",
    "complete_claim",
    "abandon_claim",
    "send_agent_message",
    "ack_message",
    "claim_files",
    "release_files",
    "claim_resource",
    "release_resource",
    "heartbeat",
    "activity_append",
    "register_agent",
})

COMPLETED_STEP_STATUSES = frozenset({"completed", "skipped"})
RUNNING_STATUSES = frozenset({"pending", "running"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_WORKERS: Dict[str, threading.Thread] = {}
_WORKERS_LOCK = threading.Lock()


class JobBoundaryError(ValueError):
    """Raised when a job would cross the hot-path / DBOS boundary."""


@dataclass(frozen=True)
class JobSpec:
    job_name: str
    title: str
    dbos_eligible: bool
    task_anchors: tuple[str, ...]
    description: str = ""


JOB_CATALOG: Dict[str, JobSpec] = {
    "plan_agent_run": JobSpec(
        job_name="plan_agent_run",
        title="Project-native plan agent run",
        dbos_eligible=True,
        task_anchors=("BUG-60", "UI-16"),
        description="Run Ask Taikun outside the HTTP/MCP request with a persisted checkpoint.",
    ),
    "audit_export_batch": JobSpec(
        job_name="audit_export_batch",
        title="Audit export across projects",
        dbos_eligible=True,
        task_anchors=("HARDEN-13", "RECON-10"),
        description="Generate audit export bundles per project with checkpointed steps.",
    ),
    "receipt_projection_batch": JobSpec(
        job_name="receipt_projection_batch",
        title="Coordination receipt projection batch",
        dbos_eligible=True,
        task_anchors=("RECON-9", "RECON-10"),
        description="Project coordination receipts per project without mutating activity.",
    ),
    "reconcile_alerts_resumable": JobSpec(
        job_name="reconcile_alerts_resumable",
        title="Resumable reconcile alerts",
        dbos_eligible=True,
        task_anchors=("RECON-10",),
        description="Run reconcile_alerts per project with checkpoint resume.",
    ),
    "drain_webhook_inbox": JobSpec(
        job_name="drain_webhook_inbox",
        title="Drain durable webhook inbox",
        dbos_eligible=False,
        task_anchors=("PERF-1",),
        description="Apply pending webhook-inbox events idempotently off the request path.",
    ),
    "reconcile_open_pr_merges": JobSpec(
        job_name="reconcile_open_pr_merges",
        title="Recover merges whose webhooks were lost",
        dbos_eligible=False,
        task_anchors=("PERF-1",),
        description=(
            "Sweep tasks that still record an open PR and stamp any GitHub "
            "reports merged. Recovers pull_request webhooks 502'd during a "
            "deploy restart, which GitHub never redelivers."
        ),
    ),
    "compact_decision_snapshots": JobSpec(
        job_name="compact_decision_snapshots",
        title="Compact decision-corpus snapshot bodies",
        dbos_eligible=True,
        task_anchors=("COORD-50",),
        description=(
            "Drop decision_records snapshot bodies past their TTL, keeping escalated, "
            "abandoned, and human-resolved episodes indefinitely as demonstrations."
        ),
    ),
}


def _stable_run_id(job_name: str, project: str, params: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"job_name": job_name, "project": project, "params": dict(params)},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"bgjob-{job_name}-{digest}"


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def assert_job_boundary(job_name: str) -> JobSpec:
    """Fail closed if the job is unknown or would touch forbidden hot-path ops."""
    spec = JOB_CATALOG.get(job_name)
    if not spec:
        raise JobBoundaryError(f"unknown background job: {job_name}")
    if job_name in FORBIDDEN_HOT_PATH_OPERATIONS:
        raise JobBoundaryError(f"job {job_name} is forbidden on the hot coordination path")
    return spec


def list_background_jobs() -> Dict[str, Any]:
    """Return the catalog with DBOS eligibility and forbidden hot-path ops."""
    jobs = []
    for spec in JOB_CATALOG.values():
        jobs.append({
            "job_name": spec.job_name,
            "title": spec.title,
            "description": spec.description,
            "dbos_eligible": spec.dbos_eligible,
            "task_anchors": list(spec.task_anchors),
        })
    return {
        "schema": CATALOG_SCHEMA,
        "forbidden_hot_path_operations": sorted(FORBIDDEN_HOT_PATH_OPERATIONS),
        "jobs": jobs,
        "runtime_default": _runtime_mode(),
    }


def evaluate_dbos_runtime() -> Dict[str, Any]:
    """Report whether DBOS is installed and the recommended runtime for eligible jobs."""
    mode = _runtime_mode()
    dbos_available = False
    dbos_version = None
    import_error = ""
    try:
        import dbos  # type: ignore  # noqa: F401
        dbos_available = True
        dbos_version = getattr(dbos, "__version__", None)
    except Exception as exc:
        import_error = str(exc)

    recommendation = "local_checkpoint"
    rationale = (
        "Use the built-in SQLite checkpoint runner. DBOS is optional infrastructure "
        "for long-running hosted jobs once TALLY-4/DISPATCH-7 land."
    )
    if dbos_available and os.environ.get("SWITCHBOARD_JOB_RUNTIME", "").lower() == "dbos":
        recommendation = "dbos"
        rationale = (
            "SWITCHBOARD_JOB_RUNTIME=dbos is set and the dbos package is importable. "
            "Eligible jobs may delegate to DBOS while preserving the public job contract."
        )

    return {
        "schema": EVAL_SCHEMA,
        "dbos_available": dbos_available,
        "dbos_version": dbos_version,
        "import_error": import_error or None,
        "configured_runtime": mode,
        "recommendation": recommendation,
        "rationale": rationale,
        "hot_path_independent": True,
        "eligible_job_count": sum(1 for j in JOB_CATALOG.values() if j.dbos_eligible),
    }


def _runtime_mode() -> str:
    raw = (os.environ.get("SWITCHBOARD_JOB_RUNTIME") or "local_checkpoint").strip().lower()
    if raw in ("dbos", "local", "local_checkpoint"):
        return "dbos" if raw == "dbos" else "local_checkpoint"
    return "local_checkpoint"


def _target_projects(project: str, params: Mapping[str, Any]) -> List[str]:
    if params.get("projects"):
        projects = store.coerce_csv_list(params["projects"])
    elif project and project != "all":
        projects = [project]
    else:
        projects = store.project_ids()
    unknown = [p for p in projects if not store.has_project(p)]
    if unknown:
        raise ValueError(f"unknown project(s): {', '.join(unknown)}")
    return projects


def _plan_steps(job_name: str, project: str, params: Mapping[str, Any]) -> List[Dict[str, Any]]:
    projects = _target_projects(project, params)
    steps: List[Dict[str, Any]] = []
    for proj in projects:
        steps.append({
            "step_id": f"{job_name}:{proj}",
            "project_id": proj,
            "status": "pending",
            "attempts": 0,
            "updated_at": None,
            "result": None,
            "error": None,
        })
    return steps


def _create_run_manifest(job_name: str, project: str, params: Mapping[str, Any],
                         *, run_id: str = "") -> Dict[str, Any]:
    spec = assert_job_boundary(job_name)
    now = time.time()
    manifest = {
        "schema": SCHEMA,
        "run_id": run_id or _stable_run_id(job_name, project, params),
        "job_name": job_name,
        "title": spec.title,
        "dbos_eligible": spec.dbos_eligible,
        "project": project,
        "params": dict(params),
        "runtime": _runtime_mode(),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "steps": _plan_steps(job_name, project, params),
        "summary": {},
        "error": None,
    }
    manifest["summary"] = summarize_run(manifest)
    return manifest


def summarize_run(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    steps = list(manifest.get("steps") or [])
    completed = sum(1 for s in steps if s.get("status") in COMPLETED_STEP_STATUSES)
    failed = sum(1 for s in steps if s.get("status") == "failed")
    pending = sum(1 for s in steps if s.get("status") in RUNNING_STATUSES)
    return {
        "step_count": len(steps),
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "ok": failed == 0 and pending == 0 and len(steps) > 0,
    }


def _persist_run(c: sqlite3.Connection, manifest: Mapping[str, Any]) -> None:
    payload = dict(manifest)
    payload["updated_at"] = time.time()
    payload["summary"] = summarize_run(payload)
    c.execute(
        """INSERT INTO background_job_runs
           (run_id, job_name, project, status, runtime, manifest_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
             status=excluded.status,
             runtime=excluded.runtime,
             manifest_json=excluded.manifest_json,
             updated_at=excluded.updated_at""",
        (
            payload["run_id"],
            payload["job_name"],
            payload.get("project") or store.DEFAULT_PROJECT,
            payload.get("status") or "pending",
            payload.get("runtime") or _runtime_mode(),
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            float(payload.get("created_at") or time.time()),
            float(payload["updated_at"]),
        ),
    )


def load_run(project: str, run_id: str) -> Dict[str, Any]:
    store.init_db(project)
    with store._conn(project) as c:
        row = c.execute(
            "SELECT manifest_json FROM background_job_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return {"error": "run_not_found", "run_id": run_id, "project": project}
    manifest = _json_load(row["manifest_json"], {})
    if manifest.get("schema") != SCHEMA:
        return {"error": "unsupported_run_schema", "run_id": run_id}
    return manifest


def list_job_runs(project: str, *, job_name: str = "", limit: int = 20) -> Dict[str, Any]:
    store.init_db(project)
    query = "SELECT run_id, job_name, project, status, runtime, created_at, updated_at FROM background_job_runs"
    args: List[Any] = []
    clauses = []
    if job_name:
        clauses.append("job_name=?")
        args.append(job_name)
    if project and project != "all":
        clauses.append("project=?")
        args.append(project)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    with store._conn(project) as c:
        rows = [dict(r) for r in c.execute(query, args).fetchall()]
    return {
        "schema": "switchboard.background_job_run_list.v1",
        "project": project,
        "count": len(rows),
        "runs": rows,
    }


def _step_handler(job_name: str) -> Callable[[str, Mapping[str, Any]], Dict[str, Any]]:
    handlers = {
        "plan_agent_run": _step_plan_agent,
        "audit_export_batch": _step_audit_export,
        "receipt_projection_batch": _step_receipt_projection,
        "reconcile_alerts_resumable": _step_reconcile_alerts,
        "drain_webhook_inbox": _step_drain_webhook_inbox,
        "reconcile_open_pr_merges": _step_reconcile_open_pr_merges,
        "compact_decision_snapshots": _step_compact_decision_snapshots,
    }
    handler = handlers.get(job_name)
    if not handler:
        raise JobBoundaryError(f"no step handler for job: {job_name}")
    return handler


def _step_plan_agent(project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    import agent

    question = str(params.get("question") or "").strip()
    if not question:
        raise ValueError("question required")
    history = params.get("history") if isinstance(params.get("history"), list) else []
    result = agent.run(None, question, history=history, project=project_id)
    answer = result.get("answer") or ""
    payload = {
        "run_id": params.get("run_id"),
        "proposals": result.get("proposals") or [],
        "new_tasks": result.get("new_tasks") or [],
        "sources": result.get("sources") or [],
    }
    if params.get("record_chat"):
        session = str(params.get("session") or "plan")
        prior = store.recent_chat(session, 100, project=project_id)
        already_recorded = any(
            item.get("role") == "assistant"
            and (item.get("payload") or {}).get("run_id") == params.get("run_id")
            for item in prior
        )
        if not already_recorded:
            store.add_chat(session, "assistant", answer, payload, project=project_id)
    return {
        "answer": answer,
        "proposal": result.get("proposal"),
        "proposals": payload["proposals"],
        "new_tasks": payload["new_tasks"],
        "sources": payload["sources"],
        "recipients": result.get("recipients"),
        "dispatch_targets": result.get("dispatch_targets") or [],
    }


def _step_compact_decision_snapshots(
        project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Bound decision-corpus storage by episode count, not tick rate (COORD-50).

    The projected half is ~200 bytes and is retained indefinitely; a hydrated
    snapshot body is 5-50 KB and is dropped past its TTL unless the episode is a
    demonstration (escalated, abandoned, or human-resolved). The box is a t4g.micro
    that has already wedged itself once on disk, so this is not optional.
    """
    from switchboard.storage.repositories import decision_records

    store.init_db(project_id)
    ttl_days = params.get("ttl_days")
    return decision_records.compact_decision_snapshots(
        project=project_id,
        ttl_days=(
            int(ttl_days) if ttl_days is not None
            else decision_records.DEFAULT_SNAPSHOT_TTL_DAYS
        ),
    )


def _step_audit_export(project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    store.init_db(project_id)
    bundle = store.audit_export(project=project_id)
    return {
        "schema": bundle.get("schema"),
        "task_count": len(bundle.get("tasks") or []),
        "activity_count": len(bundle.get("activity") or []),
        "side_effect_count": len(bundle.get("external_side_effects") or []),
    }


def _step_receipt_projection(project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    import coordination_receipts
    store.init_db(project_id)
    listed = coordination_receipts.list_coordination_receipts(
        project_id,
        task_id=str(params.get("task_id") or ""),
        agent_id=str(params.get("agent_id") or ""),
        limit=int(params.get("limit") or 500),
    )
    return {
        "receipt_count": int(listed.get("count") or 0),
        "task_id_filter": params.get("task_id") or None,
    }


def _step_reconcile_alerts(project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    store.init_db(project_id)
    store.seed_if_empty(project_id)
    return store.run_reconcile_alerts(
        project=project_id,
        alert_to=str(params.get("alert_to") or "switchboard/operator"),
        min_severity=str(params.get("min_severity") or "medium"),
        dedupe_window_s=int(params.get("dedupe_window_s") or 3600),
    )


def _step_drain_webhook_inbox(project_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    import webhook_inbox
    store.init_db(project_id)
    return webhook_inbox.drain(project_id, limit=int(params.get("limit") or 500))


def sweep_open_pr_merges(
    project_id: str,
    limit: int = 40,
    *,
    canonical_repo_for=None,
    fetch_pull_request=None,
) -> Dict[str, Any]:
    """Stamp merge provenance for tasks whose merge webhook was lost.

    A pull_request webhook that arrives while autodeploy is restarting the
    server is 502'd at the edge and GitHub never redelivers it, so the board
    keeps reporting an open PR on merged work (observed live on UI-72 /
    PR #1017, 2026-07-28). This sweep re-reads fresh GitHub state through the
    same per-task reconcile the operator route uses and stamps only what
    GitHub itself reports merged. Bounded, idempotent, and per-task failures
    are counted rather than fatal so one unreachable PR cannot block the rest.
    """
    from switchboard.application.commands.reconcile_task_merge import execute
    from switchboard.storage.repositories.provenance import (
        task_merge_reconcile_subject,
    )
    store.init_db(project_id)
    repo_for = canonical_repo_for or store.get_project_github_repo
    fetch = fetch_pull_request or (
        lambda repo, number: store._github_pr(
            repo, number, token=store._github_token(repo)))
    with store._conn(project_id) as c:
        rows = c.execute(
            "SELECT g.task_id FROM task_git_state g "
            "JOIN tasks t ON t.task_id = g.task_id "
            "WHERE g.pr_number IS NOT NULL "
            "AND (g.merged_sha IS NULL OR g.merged_sha = '') "
            "AND t.status NOT IN ('Done', 'Cancelled') "
            "ORDER BY g.updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    candidates = [str(r["task_id"]) for r in rows]
    checked = stamped = errors = 0
    stamped_tasks = []
    for task_id in candidates:
        checked += 1
        try:
            result = execute(
                task_id=task_id,
                project=project_id,
                actor="reconcile/webhook-loss-sweep",
                load_subject=task_merge_reconcile_subject,
                canonical_repo_for=repo_for,
                fetch_pull_request=fetch,
                mark_merged=store.mark_task_merged,
            )
        except Exception:  # noqa: BLE001 - one bad PR must not block the rest
            errors += 1
            continue
        if result.get("error"):
            errors += 1
        elif result.get("observed") in ("default_branch_merged", "pr_merged"):
            stamped += 1
            stamped_tasks.append(task_id)
    return {
        "schema": "switchboard.open_pr_merge_sweep.v1",
        "project": project_id,
        "candidates": len(candidates),
        "checked": checked,
        "stamped": stamped,
        "stamped_tasks": stamped_tasks,
        "errors": errors,
    }


def _step_reconcile_open_pr_merges(
    project_id: str, params: Mapping[str, Any],
) -> Dict[str, Any]:
    return sweep_open_pr_merges(
        project_id, limit=int(params.get("limit") or 40))


def _record_plan_chat_failure(manifest: Mapping[str, Any], error: str) -> None:
    params = manifest.get("params") or {}
    if manifest.get("job_name") != "plan_agent_run" or not params.get("record_chat"):
        return
    project = str(manifest.get("project") or store.DEFAULT_PROJECT)
    session = str(params.get("session") or "plan")
    run_id = str(params.get("run_id") or manifest.get("run_id") or "")
    prior = store.recent_chat(session, 100, project=project)
    already_recorded = any(
        item.get("role") == "assistant"
        and (item.get("payload") or {}).get("run_id") == run_id
        for item in prior
    )
    if not already_recorded:
        store.add_chat(
            session,
            "assistant",
            f"(agent error: {error})",
            {"run_id": run_id, "status": "failed", "error": error},
            project=project,
        )


def _execute_step(manifest: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    handler = _step_handler(manifest["job_name"])
    step = dict(step)
    step["status"] = "running"
    step["attempts"] = int(step.get("attempts") or 0) + 1
    step["updated_at"] = time.time()
    try:
        result = handler(step["project_id"], manifest.get("params") or {})
        step["status"] = "completed"
        step["result"] = result
        step["error"] = None
    except Exception as exc:
        step["status"] = "failed"
        step["error"] = str(exc)
        step["result"] = None
        try:
            _record_plan_chat_failure(manifest, str(exc))
        except Exception as chat_exc:
            step["chat_error"] = str(chat_exc)
    return step


def run_background_job(project: str, job_name: str, *,
                       run_id: str = "",
                       resume: bool = True,
                       params: Optional[Mapping[str, Any]] = None,
                       crash_after_step: Optional[int] = None,
                       actor: str = "background_job") -> Dict[str, Any]:
    """Run or resume a catalog job with per-project step checkpoints."""
    spec = assert_job_boundary(job_name)
    from switchboard.security import redact_provider_secrets
    params = redact_provider_secrets(dict(params or {}))
    store.init_db(project)

    manifest: Dict[str, Any]
    if resume and run_id:
        loaded = load_run(project, run_id)
        if loaded.get("error"):
            manifest = _create_run_manifest(job_name, project, params, run_id=run_id)
        else:
            manifest = loaded
    elif resume:
        candidate_id = _stable_run_id(job_name, project, params)
        loaded = load_run(project, candidate_id)
        if loaded.get("error"):
            manifest = _create_run_manifest(job_name, project, params)
        elif loaded.get("status") in TERMINAL_RUN_STATUSES:
            manifest = _create_run_manifest(job_name, project, params)
        else:
            manifest = loaded
    else:
        manifest = _create_run_manifest(job_name, project, params, run_id=run_id)

    if manifest.get("job_name") != job_name:
        raise JobBoundaryError("run_id job_name mismatch")

    admission_id = str((manifest.get("params") or {}).get("ai_admission_id") or "")
    if admission_id:
        from switchboard.storage.repositories import ai_admission
        decision = ai_admission.authorize_execution(
            project=project, admission_id=admission_id,
            authorization=(manifest.get("params") or {}).get("ai_authorization") or {})
        if not decision.allowed:
            if decision.status == ai_admission.QUEUED:
                return manifest
            manifest["status"] = "failed"
            manifest["error"] = decision.reason_code
            with store._conn(project) as connection:
                _persist_run(connection, manifest)
            return manifest

    manifest["status"] = "running"
    manifest["runtime"] = _runtime_mode()
    manifest["dbos_eligible"] = spec.dbos_eligible
    steps = [dict(s) for s in manifest.get("steps") or []]

    for index, step in enumerate(steps):
        if step.get("status") in COMPLETED_STEP_STATUSES:
            continue
        steps[index] = _execute_step(manifest, step)
        manifest["steps"] = steps
        manifest["summary"] = summarize_run(manifest)
        with store._conn(project) as c:
            _persist_run(c, manifest)
        if steps[index]["status"] == "failed":
            manifest["status"] = "failed"
            manifest["error"] = steps[index].get("error")
            break
        if crash_after_step is not None and index == crash_after_step:
            manifest["status"] = "running"
            with store._conn(project) as c:
                _persist_run(c, manifest)
            raise RuntimeError(f"simulated crash after step {index}")
    else:
        manifest["status"] = "completed"
        manifest["error"] = None

    manifest["summary"] = summarize_run(manifest)
    with store._conn(project) as c:
        _persist_run(c, manifest)
        now = time.time()
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (
                None,
                actor,
                "background_job.completed" if manifest["status"] == "completed" else "background_job.failed",
                json.dumps({
                    "run_id": manifest["run_id"],
                    "job_name": job_name,
                    "status": manifest["status"],
                    "summary": manifest["summary"],
                    "runtime": manifest.get("runtime"),
                }, sort_keys=True),
                now,
            ),
        )
    if admission_id:
        ai_admission.finish(project, admission_id, manifest["status"])
    return manifest


def _start_worker(project: str, job_name: str, run_id: str,
                  params: Mapping[str, Any], actor: str) -> bool:
    with _WORKERS_LOCK:
        if run_id in _WORKERS:
            return False

        def work() -> None:
            try:
                run_background_job(
                    project,
                    job_name,
                    run_id=run_id,
                    resume=True,
                    params=params,
                    actor=actor,
                )
            finally:
                with _WORKERS_LOCK:
                    _WORKERS.pop(run_id, None)

        worker = threading.Thread(
            target=work,
            name=f"switchboard-{job_name}-{run_id[-8:]}",
            daemon=True,
        )
        _WORKERS[run_id] = worker
        worker.start()
    return True


def ensure_background_job_running(project: str, run_id: str,
                                  actor: str = "background_job/resume") -> Dict[str, Any]:
    """Restart a persisted non-terminal run after a process/UI reconnect."""
    manifest = load_run(project, run_id)
    if manifest.get("error") or manifest.get("status") in TERMINAL_RUN_STATUSES:
        return manifest
    admission_id = str((manifest.get("params") or {}).get("ai_admission_id") or "")
    if admission_id:
        from switchboard.storage.repositories import ai_admission
        decision = ai_admission.authorize_execution(
            project=project, admission_id=admission_id,
            authorization=(manifest.get("params") or {}).get("ai_authorization") or {})
        if not decision.allowed:
            if decision.status == ai_admission.QUEUED:
                return manifest
            manifest["status"] = "failed"
            manifest["error"] = decision.reason_code
            with store._conn(project) as connection:
                _persist_run(connection, manifest)
            return manifest
    _start_worker(
        project,
        manifest["job_name"],
        run_id,
        manifest.get("params") or {},
        actor,
    )
    return manifest


def enqueue_background_job(project: str, job_name: str, *,
                           params: Optional[Mapping[str, Any]] = None,
                           actor: str = "background_job",
                           start_worker: bool = True) -> Dict[str, Any]:
    """Persist a pending run, then execute it outside the request thread."""
    assert_job_boundary(job_name)
    params_dict = dict(params or {})
    run_id = f"bgjob-{job_name}-{uuid.uuid4().hex}"
    params_dict["run_id"] = run_id
    manifest = _create_run_manifest(job_name, project, params_dict, run_id=run_id)
    store.init_db(project)
    with store._conn(project) as connection:
        _persist_run(connection, manifest)
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (None, actor, "background_job.queued", json.dumps({
                "run_id": run_id,
                "job_name": job_name,
                "status": "pending",
            }, sort_keys=True), time.time()),
        )

    if not start_worker:
        return manifest

    _start_worker(project, job_name, run_id, params_dict, actor)
    return manifest
