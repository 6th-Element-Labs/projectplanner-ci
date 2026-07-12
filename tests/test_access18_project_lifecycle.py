#!/usr/bin/env python3
"""ACCESS-18: project lifecycle domain contract and additive registry migration."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="access18-lifecycle-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

import db.connection as db_connection  # noqa: E402
import store  # noqa: E402
from switchboard.contracts import (  # noqa: E402
    PROJECT_RECORD_SCHEMA,
    PROJECT_UPDATE_COMMAND_SCHEMA,
    ProjectRecord,
    ProjectUpdateCommand,
    get_schema,
)
from switchboard.domain.projects.lifecycle import validate_lifecycle_transition  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _legacy_registry_without_lifecycle() -> None:
    """Simulate a pre-ACCESS-18 registry file."""
    path = os.environ["PM_PROJECT_REGISTRY_DB_PATH"]
    if os.path.exists(path):
        os.remove(path)
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                pretitle TEXT,
                db_path TEXT NOT NULL,
                seed_path TEXT,
                created_at REAL NOT NULL,
                created_by TEXT
            );
            CREATE TABLE project_access (
                project_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                owner_user_id TEXT,
                purpose TEXT,
                boundary TEXT,
                created_at REAL NOT NULL,
                created_by TEXT,
                updated_at REAL NOT NULL,
                visibility TEXT
            );
            """
        )
        now = time.time()
        c.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
            ("legacyproj", "Legacy", "pretitle", str(Path(TMP) / "legacyproj.db"), None, now, "seed"),
        )
        c.execute(
            "INSERT INTO project_access VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacyproj", "org-legacy", "user-1", "purpose", "boundary", now, "seed", now, "org"),
        )


try:
    store.init_project_registry()
    store.init_db("switchboard")

    # --- contract registration -------------------------------------------------
    ok(get_schema(PROJECT_RECORD_SCHEMA) is not None,
       "registry records switchboard.project.v2")
    ok(get_schema(PROJECT_UPDATE_COMMAND_SCHEMA) is not None,
       "registry records switchboard.project.update_command.v2")

    record = ProjectRecord.from_mapping({
        "id": "demo",
        "label": "Demo",
        "lifecycle_status": "active",
    })
    ok(record.schema == PROJECT_RECORD_SCHEMA and record.is_active(),
       "ProjectRecord validates and exposes lifecycle helpers")

    # --- lifecycle guards ------------------------------------------------------
    ok(validate_lifecycle_transition("active", "archived") is None,
       "active -> archived is allowed")
    ok(validate_lifecycle_transition("archived", "active") is None,
       "archived -> active is allowed")
    blocked = validate_lifecycle_transition("active", "archived", is_protected=True)
    ok(blocked and "protected" in blocked["error"],
       "protected projects cannot be archived")
    invalid = validate_lifecycle_transition("archived", "bogus")
    ok(invalid and "invalid lifecycle_status" in invalid["error"],
       "invalid lifecycle_status fails closed")

    # --- fresh registry active discovery ---------------------------------------
    created = store.create_project("Atlas QA", project_id="atlas-qa", actor="test",
                                   purpose="qa purpose", boundary="qa boundary",
                                   visibility="private")
    ok(created.get("created") is True, "create_project still creates dynamic projects")

    before_ids = {p["id"] for p in store.projects()}
    ok("atlas-qa" in before_ids, "new active project appears in discovery list")

    rec = store.get_project_record("atlas-qa")
    ok(rec.get("lifecycle_status") == "active" and rec.get("purpose") == "qa purpose",
       "fresh registry record carries active lifecycle defaults")

    # --- metadata round-trip ---------------------------------------------------
    updated = store.update_project_metadata({
        "project_id": "atlas-qa",
        "label": "Atlas QA Updated",
        "pretitle": "6EL",
        "purpose": "updated purpose",
        "boundary": "updated boundary",
        "visibility": "org",
        "updated_by": "tester",
    }, actor="tester")
    ok(updated.get("label") == "Atlas QA Updated" and updated.get("pretitle") == "6EL",
       "editable metadata round-trips through repository")
    ok(updated.get("purpose") == "updated purpose" and updated.get("visibility") == "org",
       "access metadata round-trips through repository")

    # --- archive hides from discovery but remains routable ---------------------
    archived = store.update_project_metadata({
        "project_id": "atlas-qa",
        "lifecycle_status": "archived",
        "archive_reason": "qa archive",
    }, actor="tester")
    ok(archived.get("lifecycle_status") == "archived" and archived.get("archive_reason") == "qa archive",
       "archive metadata round-trips")
    ok("atlas-qa" not in {p["id"] for p in store.projects()},
       "archived project drops out of active discovery")
    ok(store.has_project("atlas-qa"),
       "archived project remains routable via has_project")

    full = store.list_registry_projects(include_archived=True)
    ok(any(p["id"] == "atlas-qa" and p["lifecycle_status"] == "archived" for p in full),
       "full registry read retains archived project")

    # --- protected built-ins ---------------------------------------------------
    builtin = store.get_project_record("switchboard")
    ok(builtin.get("is_protected") is True and builtin.get("is_system") is True,
       "built-in projects surface as protected/system records")
    protected_archive = store.update_project_metadata({
        "project_id": "switchboard",
        "lifecycle_status": "archived",
    }, actor="tester")
    ok(protected_archive.get("error") and "protected" in protected_archive["error"],
       "protected built-in archive attempt fails closed")

    immutable_builtin = store.update_project_metadata({
        "project_id": "helm",
        "label": "Renamed",
    }, actor="tester")
    ok(immutable_builtin.get("error") and "immutable" in immutable_builtin["error"],
       "built-in routing metadata remains immutable")

    # --- upgraded legacy registry compatibility --------------------------------
    _legacy_registry_without_lifecycle()
    db_connection.bust_project_cache()
    store.init_project_registry()
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as legacy_conn:
        migrated_names = {
            row[0] for row in legacy_conn.execute("SELECT name FROM registry_migrations").fetchall()
        }
    ok("access18_projects_lifecycle_status" in migrated_names,
       "legacy registry upgrades through ledgered migrations")

    legacy = store.get_project_record("legacyproj")
    ok(legacy.get("lifecycle_status") == "active" and legacy.get("label") == "Legacy",
       "upgraded legacy registry preserves rows with active defaults")
    upgraded_discovery = {p["id"] for p in store.projects()}
    ok("legacyproj" in upgraded_discovery,
       "upgraded legacy project remains in active discovery")

    # --- cache invalidation after lifecycle write ------------------------------
    db_connection.bust_project_cache()
    store.create_project("Cache Bust", project_id="cache-bust", actor="test")
    loads = {"n": 0}
    orig = db_connection._load_dynamic_projects
    db_connection._load_dynamic_projects = lambda: (loads.__setitem__("n", loads["n"] + 1) or orig())
    try:
        db_connection.bust_project_cache()
        store._project_map()
        loads["n"] = 0
        for _ in range(20):
            store._project_map()
        ok(loads["n"] == 0, "hot project map reads stay cached")
        store.update_project_metadata({
            "project_id": "cache-bust",
            "label": "Cache Bust Renamed",
        }, actor="test")
        ok(store.get_project_record("cache-bust")["label"] == "Cache Bust Renamed",
           "metadata update visible immediately after cache bust")
    finally:
        db_connection._load_dynamic_projects = orig
        db_connection.bust_project_cache()

    # --- repository protocol surface -------------------------------------------
    repo = store.access_repository
    ok(hasattr(repo, "get_project_record") and hasattr(repo, "update_project_metadata"),
       "AccessStoreRepository exposes lifecycle repository contract")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
