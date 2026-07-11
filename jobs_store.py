"""jobs_store.py — background jobs / DBOS eval (leaf store). Extracted verbatim from store.py (ARCH-5)."""
import json
import time
import os
import sqlite3
import hashlib
import uuid
import copy
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.core import *     # noqa: F401,F403
from db.schema import *   # noqa: F401,F403
from db.connection import *  # noqa: F401,F403

__all__ = [
    "list_background_jobs",
    "run_background_job",
    "get_background_job_run",
    "list_background_job_runs",
]


def list_background_jobs() -> Dict[str, Any]:
    """Catalog of resumable background jobs and DBOS eligibility boundaries."""
    import background_jobs
    return background_jobs.list_background_jobs()


def run_background_job(project: str = DEFAULT_PROJECT, job_name: str = "",
                       run_id: str = "", resume: bool = True,
                       params: Any = None, actor: str = "background_job") -> Dict[str, Any]:
    """Run or resume a checkpointed background job for one project scope."""
    import background_jobs
    if not (job_name or "").strip():
        return {"error": "job_name required", "project": project}
    return background_jobs.run_background_job(
        project,
        job_name.strip(),
        run_id=run_id,
        resume=resume,
        params=params if isinstance(params, dict) else {},
        actor=actor,
    )


def get_background_job_run(project: str = DEFAULT_PROJECT, run_id: str = "") -> Dict[str, Any]:
    """Load one persisted background job run manifest."""
    import background_jobs
    if not (run_id or "").strip():
        return {"error": "run_id required", "project": project}
    return background_jobs.load_run(project, run_id.strip())


def list_background_job_runs(project: str = DEFAULT_PROJECT, *,
                             job_name: str = "", limit: int = 20) -> Dict[str, Any]:
    """List recent background job runs for a project."""
    import background_jobs
    return background_jobs.list_job_runs(project, job_name=job_name, limit=limit)
