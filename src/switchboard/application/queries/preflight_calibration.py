"""Read-side queries for SESSION-15 preflight prediction calibration."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from switchboard.storage.repositories import preflight_runs as repo


def get_run(run_id: str, *, project: str) -> Optional[Dict[str, Any]]:
    return repo.get_preflight_run(run_id, project=project)


def list_runs(*, project: str, task_id: str = "", head_sha: str = "",
              work_session_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    return repo.list_preflight_runs(
        task_id=task_id, head_sha=head_sha, work_session_id=work_session_id,
        limit=limit, project=project)


def calibration(*, project: str, code: str = "", since: float = 0.0,
                min_outcomes: int = 3) -> Dict[str, Any]:
    return repo.preflight_calibration(
        code=code, since=since, min_outcomes=min_outcomes, project=project)
