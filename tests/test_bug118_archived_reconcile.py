#!/usr/bin/env python3
"""BUG-118: archived projects cannot abort the global reconcile-alert batch."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

import jobs


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


tmp = tempfile.mkdtemp(prefix="bug118-reconcile-")
env_names = (
    "PM_RECON_ALERT_PROJECTS",
    "PM_RECON_ALERT_LOCK_PATH",
    "PM_RECON_INCREMENTAL",
)
old_env = {name: os.environ.get(name) for name in env_names}
patched_names = (
    "project_ids",
    "project_lifecycle_status",
    "init_db",
    "seed_if_empty",
    "run_reconcile_alerts",
)
originals = {name: getattr(jobs.store, name) for name in patched_names}
calls: list[tuple[str, str]] = []


try:
    os.environ["PM_RECON_ALERT_PROJECTS"] = "all"
    os.environ["PM_RECON_ALERT_LOCK_PATH"] = str(Path(tmp) / "reconcile.lock")
    os.environ["PM_RECON_INCREMENTAL"] = "1"

    jobs.store.project_ids = lambda: ["archived-one", "archive-race", "active-one"]
    jobs.store.project_lifecycle_status = (
        lambda project: "archived" if project == "archived-one" else "active"
    )
    jobs.store.init_db = lambda project: calls.append(("init", project))
    jobs.store.seed_if_empty = lambda project: calls.append(("seed", project))

    def run_reconcile_alerts(*, project: str, **_kwargs) -> dict:
        if project == "archived-one":
            raise AssertionError("archived project reached the write-capable reconcile path")
        if project == "archive-race":
            raise jobs.store.ProjectLifecycleWriteBlocked(
                project, "insert", "archived")
        calls.append(("reconcile", project))
        return {
            "project": project,
            "ok": True,
            "finding_count": 0,
            "alert_sent": False,
            "deduped": False,
            "message_id": None,
        }

    jobs.store.run_reconcile_alerts = run_reconcile_alerts

    result = jobs.reconcile_alerts()
    by_project = {item["project"]: item for item in result["results"]}
    skipped = by_project["archived-one"]

    ok(result["projects"] == ["archived-one", "archive-race", "active-one"],
       "the aggregate preserves configured project order")
    ok(skipped["ok"] is True and skipped["skipped"] is True
       and skipped["skip_reason"] == "project_archived"
       and skipped["lifecycle_status"] == "archived",
       "an archived project returns an explicit non-fatal skipped result")
    raced = by_project["archive-race"]
    ok(raced["skipped"] is True and raced["skip_reason"] == "project_archived"
       and raced["write_block"]["error"] == "project_archived",
       "a project archived during reconcile preserves the typed write denial and continues")
    ok(calls == [
        ("init", "archive-race"),
        ("seed", "archive-race"),
        ("init", "active-one"),
        ("seed", "active-one"),
        ("reconcile", "active-one"),
    ], "the archived project performs no writes and the later active project still reconciles")
    ok(result["findings"] == 0 and result["sent"] == 0
       and result["deduped"] == 0,
       "a skipped archived project does not distort aggregate alert counters")
finally:
    for name, value in originals.items():
        setattr(jobs.store, name, value)
    for name, value in old_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    shutil.rmtree(tmp, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
