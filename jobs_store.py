"""jobs_store.py — background jobs / DBOS eval (leaf store)."""

from typing import Any, Dict

from constants import DEFAULT_PROJECT

__all__ = [
    "list_background_jobs",
    "enqueue_background_job",
    "ensure_background_job_running",
    "run_background_job",
    "get_background_job_run",
    "list_background_job_runs",
]


def list_background_jobs() -> Dict[str, Any]:
    """Catalog of resumable background jobs and DBOS eligibility boundaries."""
    import background_jobs

    return background_jobs.list_background_jobs()


def enqueue_background_job(
    project: str = DEFAULT_PROJECT,
    job_name: str = "",
    params: Any = None,
    actor: str = "background_job",
    start_worker: bool = True,
) -> Dict[str, Any]:
    """Persist and start a background job without holding the caller open."""
    import background_jobs

    if not (job_name or "").strip():
        return {"error": "job_name required", "project": project}
    return background_jobs.enqueue_background_job(
        project,
        job_name.strip(),
        params=params if isinstance(params, dict) else {},
        actor=actor,
        start_worker=start_worker,
    )


def ensure_background_job_running(
    project: str = DEFAULT_PROJECT,
    run_id: str = "",
    actor: str = "background_job/resume",
) -> Dict[str, Any]:
    """Resume a persisted non-terminal job when its client reconnects."""
    import background_jobs

    if not (run_id or "").strip():
        return {"error": "run_id required", "project": project}
    return background_jobs.ensure_background_job_running(
        project, run_id.strip(), actor=actor
    )


def run_background_job(
    project: str = DEFAULT_PROJECT,
    job_name: str = "",
    run_id: str = "",
    resume: bool = True,
    params: Any = None,
    actor: str = "background_job",
) -> Dict[str, Any]:
    """Run or resume a checkpointed background job for one project scope."""
    import background_jobs

    if not (job_name or "").strip():
        return {"error": "job_name required", "project": project}
    from switchboard.security import redact_provider_secrets
    return background_jobs.run_background_job(
        project,
        job_name.strip(),
        run_id=run_id,
        resume=resume,
        params=redact_provider_secrets(params if isinstance(params, dict) else {}),
        actor=actor,
    )


def get_background_job_run(
    project: str = DEFAULT_PROJECT, run_id: str = ""
) -> Dict[str, Any]:
    """Load one persisted background job run manifest."""
    import background_jobs

    if not (run_id or "").strip():
        return {"error": "run_id required", "project": project}
    return background_jobs.load_run(project, run_id.strip())


def list_background_job_runs(
    project: str = DEFAULT_PROJECT, *, job_name: str = "", limit: int = 20
) -> Dict[str, Any]:
    """List recent background job runs for a project."""
    import background_jobs

    return background_jobs.list_job_runs(project, job_name=job_name, limit=limit)
