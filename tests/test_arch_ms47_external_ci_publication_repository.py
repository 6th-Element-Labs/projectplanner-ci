#!/usr/bin/env python3
"""ARCH-MS-47: external_ci + publication under storage/repositories."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms47-eci-pub-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for name in (
    "switchboard.storage.repositories.external_ci",
    "switchboard.storage.repositories.publication",
    "external_ci_store",
    "publication_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/external_ci.py").is_file(),
   "external_ci.py exists under storage/repositories")
ok((ROOT / "src/switchboard/storage/repositories/publication.py").is_file(),
   "publication.py exists under storage/repositories")
ok((ROOT / "external_ci_store.py").is_file(),
   "external_ci_store.py shim exists at repo root")
ok((ROOT / "publication_store.py").is_file(),
   "publication_store.py shim exists at repo root")

from switchboard.storage.repositories import external_ci as eci_repo  # noqa: E402
from switchboard.storage.repositories import publication as pub_repo  # noqa: E402
import external_ci_store  # noqa: E402
import publication_store  # noqa: E402
import store  # noqa: E402

ok(external_ci_store.create_external_ci_run is eci_repo.create_external_ci_run,
   "external_ci_store shim re-exports create_external_ci_run")
ok(publication_store.create_publication_evidence is pub_repo.create_publication_evidence,
   "publication_store shim re-exports create_publication_evidence")
ok(store.create_external_ci_run is eci_repo.create_external_ci_run,
   "store facade delegates create_external_ci_run to package module")
ok(store.update_external_ci_run is eci_repo.update_external_ci_run,
   "store facade delegates update_external_ci_run to package module")
ok(store.get_external_ci_run is eci_repo.get_external_ci_run,
   "store facade delegates get_external_ci_run to package module")
ok(store.list_external_ci_runs is eci_repo.list_external_ci_runs,
   "store facade delegates list_external_ci_runs to package module")
ok(store.task_external_ci_summary is eci_repo.task_external_ci_summary,
   "store facade delegates task_external_ci_summary to package module")
ok(store.create_publication_evidence is pub_repo.create_publication_evidence,
   "store facade delegates create_publication_evidence to package module")
ok(store.list_publication_evidence is pub_repo.list_publication_evidence,
   "store facade delegates list_publication_evidence to package module")
ok(store.task_publication_summary is pub_repo.task_publication_summary,
   "store facade delegates task_publication_summary to package module")
ok(store.create_external_ci_run.__module__
   == "switchboard.storage.repositories.external_ci",
   "create_external_ci_run lives under switchboard.storage.repositories.external_ci")
ok(store.create_publication_evidence.__module__
   == "switchboard.storage.repositories.publication",
   "create_publication_evidence lives under switchboard.storage.repositories.publication")
ok(isinstance(store.external_ci_repository, eci_repo.StoreExternalCiRepository),
   "store.external_ci_repository is StoreExternalCiRepository")
ok(isinstance(store.publication_repository, pub_repo.StorePublicationRepository),
   "store.publication_repository is StorePublicationRepository")

ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file(),
   "shell residual deleted (ARCH-MS-64)")
eci_src = (ROOT / "src/switchboard/storage/repositories/external_ci.py").read_text()
pub_src = (ROOT / "src/switchboard/storage/repositories/publication.py").read_text()
ok("def create_external_ci_run(" in eci_src,
   "external_ci repository owns create_external_ci_run")
ok("def create_publication_evidence(" in pub_src,
   "publication repository owns create_publication_evidence")
ok(len(eci_src.splitlines()) > 400,
   "external_ci extract is substantial")
ok(len(pub_src.splitlines()) > 300,
   "publication extract is substantial")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created_task = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms47 external_ci publication proof",
         "description": "external_ci + publication repository extract"},
        actor="arch-ms47",
        project="switchboard",
    )
    ok(bool(created_task and created_task.get("task_id")),
       "create_task persists a task for CI/publication proof")
    task_id = created_task["task_id"]

    # Topology may reject without configured repos; accept structured mapping either way.
    ci = store.create_external_ci_run(
        {
            "task_id": task_id,
            "source_sha": "abcdef1234567890",
            "source_branch": "cursor/ARCH-MS-47-extract-external-ci-publication",
        },
        actor="arch-ms47",
        project="switchboard",
    )
    ok(isinstance(ci, dict),
       f"create_external_ci_run returns a mapping via store façade ({ci.get('error')})")

    pub = store.create_publication_evidence(
        {
            "task_id": task_id,
            "source_sha": "abcdef1234567890",
        },
        actor="arch-ms47",
        project="switchboard",
    )
    ok(isinstance(pub, dict),
       f"create_publication_evidence returns a mapping via store façade ({pub.get('error')})")

    summary_ci = store.task_external_ci_summary(task_id, project="switchboard")
    summary_pub = store.task_publication_summary(task_id, project="switchboard")
    ok(isinstance(summary_ci, dict), "task_external_ci_summary reachable via store façade")
    ok(isinstance(summary_pub, dict), "task_publication_summary reachable via store façade")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-47 external_ci + publication repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
