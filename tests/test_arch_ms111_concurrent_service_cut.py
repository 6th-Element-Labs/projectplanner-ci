#!/usr/bin/env python3
"""ARCH-MS-111: concurrent Tasks writes with Coord/Deliverables reads."""
from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from path_setup import ROOT  # noqa: E402,F401

tmp = Path(tempfile.mkdtemp(prefix="arch-ms111-concurrent-cut-"))
os.environ.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "dev-open",
})
(tmp / "projects").mkdir(parents=True)

import store  # noqa: E402
from switchboard.api.deliverables_port_adapters import RepositoryDeliverablesQueries  # noqa: E402

project = "ms111-stress"
store.init_project_registry()
store.create_project("MS111 stress", project_id=project, actor="test")
store.init_db(project)
store.create_deliverable({
    "id": "ms111-stress-deliverable", "title": "Concurrent cut proof",
    "status": "proposed", "end_state": "No SQLite lock failures",
    "acceptance_criteria": ["concurrent writes and reads remain coherent"],
}, actor="test", project=project)
queries = RepositoryDeliverablesQueries()


def task_write(index: int) -> str:
    result = store.create_task(
        {"workstream_id": "STRESS", "title": f"write-{index}"},
        actor="stress", project=project,
    )
    return str(result.get("task_id") or result.get("id") or "")


def coord_read(_: int) -> int:
    return int((store.board_rollups(project=project) or {}).get("total", 0))


def deliverables_read(_: int) -> int:
    return len(queries.list_deliverables(project))


errors: list[str] = []
results: list[object] = []
with ThreadPoolExecutor(max_workers=12) as pool:
    futures = []
    for index in range(60):
        futures.extend((pool.submit(task_write, index),
                        pool.submit(coord_read, index),
                        pool.submit(deliverables_read, index)))
    for future in as_completed(futures):
        try:
            results.append(future.result())
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")

assert not errors, errors
assert len([value for value in results if isinstance(value, str) and value]) == 60
assert all(value == 1 for value in results if isinstance(value, int) and value == 1)
assert len(queries.list_deliverables(project)) == 1
print("PASS concurrent Tasks writes + Coord/Deliverables reads have zero SQLite errors")
