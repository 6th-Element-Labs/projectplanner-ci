#!/usr/bin/env python3
"""ACCESS-22: protected system projects use the unified registry lifecycle."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="access22-protected-registry-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

import db.connection as db_connection  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    for project_id in ("maxwell", "helm", "switchboard"):
        store.init_db(project_id)

    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.row_factory = sqlite3.Row
        rows = {row["id"]: dict(row) for row in c.execute(
            "SELECT * FROM projects WHERE is_system=1 ORDER BY id"
        ).fetchall()}
        migrations = {row[0] for row in c.execute(
            "SELECT name FROM registry_migrations"
        ).fetchall()}

    ok(set(rows) == {"maxwell", "helm", "switchboard"},
       "configured system homes are materialized as registry rows")
    ok(all(row["is_protected"] == 1 and row["lifecycle_status"] == "active"
           for row in rows.values()),
       "backfilled system rows are active and protected")
    ok(rows["maxwell"]["db_path"] == os.environ["PM_DB_PATH"]
       and rows["helm"]["seed_path"] == store.HELM_SEED_PATH
       and rows["switchboard"]["db_path"] == os.environ["PM_SWITCHBOARD_DB_PATH"],
       "registry rows preserve configured database and seed paths")
    ok("access22_protected_system_project_records" in migrations,
       "protected-system backfill is recorded in the registry migration ledger")

    route_map = store._project_map()
    ok(set(route_map) == set(rows)
       and route_map["switchboard"]["lifecycle_status"] == "active",
       "one registry projection routes configured and lifecycle state")

    topology = store.get_project_repo_topology("switchboard")
    ok(topology["roles"]["canonical"]["repo"] == "6th-Element-Labs/projectplanner",
       "canonical repository provenance survives registry migration")

    updated = store.update_project_metadata({
        "project_id": "switchboard",
        "label": "Switchboard Control Plane",
        "pretitle": "Updated protected metadata",
        "updated_by": "access22-test",
    }, actor="access22-test")
    ok(updated.get("label") == "Switchboard Control Plane"
       and updated.get("updated_by") == "access22-test"
       and updated.get("is_protected") is True,
       "protected records use the normal editable audited metadata path")

    store.init_project_registry()
    after_reconcile = store.get_project_record("switchboard")
    ok(after_reconcile.get("label") == "Switchboard Control Plane"
       and after_reconcile.get("db_path") == os.environ["PM_SWITCHBOARD_DB_PATH"],
       "bootstrap reconciliation preserves edits while enforcing configured paths")

    blocked = store.access_repository.transition_project_lifecycle(
        "switchboard", "archived", actor="access22-test", reason="must fail")
    ok(blocked.get("error") == "protected project cannot be archived",
       "protected flag fails closed on archive without an id comparison")

    store.BUILTIN_PROJECTS["neutral-system"] = {
        "db": str(Path(TMP) / "neutral-system.db"),
        "seed": None,
        "label": "Neutral System Home",
        "pretitle": "Configuration-driven fixture",
    }
    store.init_project_registry()
    db_connection.bust_project_cache()
    neutral = store.get_project_record("neutral-system")
    neutral_blocked = store.access_repository.transition_project_lifecycle(
        "neutral-system", "archived", actor="access22-test", reason="must fail")
    ok(neutral.get("is_protected") is True and neutral.get("is_system") is True
       and neutral_blocked.get("error") == "protected project cannot be archived",
       "new configured system homes need no customer-specific lifecycle code")
    del store.BUILTIN_PROJECTS["neutral-system"]

    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.execute("UPDATE projects SET is_protected=0 WHERE id=?", ("switchboard",))
    db_connection.bust_project_cache()
    archived = store.access_repository.transition_project_lifecycle(
        "switchboard", "archived", actor="governed-migration",
        reason="explicit protection-removal fixture")
    ok(archived.get("transitioned") is True
       and archived.get("project", {}).get("lifecycle_status") == "archived",
       "explicit protection removal enables the generic lifecycle transition")

    store.init_project_registry()
    governed_result = store.get_project_record("switchboard")
    ok(governed_result.get("is_protected") is False
       and governed_result.get("lifecycle_status") == "archived",
       "bootstrap preserves a separately governed protection-removal migration")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
