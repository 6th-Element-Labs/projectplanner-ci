#!/usr/bin/env python3
"""COORD-111: execution generations remain monotonic across fresh roles."""
from __future__ import annotations

import os
import shutil
import tempfile


TMP = tempfile.mkdtemp(prefix="coord111-role-generations-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = os.path.join(TMP, "projects")

from path_setup import ROOT as _ROOT  # noqa: E402,F401

import store  # noqa: E402
from switchboard.storage.repositories import coordination  # noqa: E402


PROJECT = "switchboard"
TASK_ID = "QA-ROLE-GENERATIONS"


def acquire(role: str) -> dict:
    with store._conn(PROJECT) as connection:
        lease = coordination._acquire_execution_lease_in(
            connection,
            task_id=TASK_ID,
            role=role,
            head_sha="",
            ttl_seconds=7200,
            agent_id="switchboard/execution",
            principal_id="coord111-test",
            now=100.0,
        )
        return dict(lease)


try:
    store.init_db(PROJECT)
    implementation = acquire("implementation")
    review = acquire("review_merge")
    remediation = acquire("remediation")

    assert [
        int(implementation["execution_generation"]),
        int(review["execution_generation"]),
        int(remediation["execution_generation"]),
    ] == [1, 2, 3]
    assert [
        implementation["execution_role"],
        review["execution_role"],
        remediation["execution_role"],
    ] == ["implementation", "review_merge", "remediation"]
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("COORD-111 global role generations: passed")
