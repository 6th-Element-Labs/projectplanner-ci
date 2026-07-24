#!/usr/bin/env python3
"""REPO-6: freeze switchboard.repo_constitution.v1 + python_modular_monolith fixture."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from path_setup import ROOT

from switchboard.contracts import (
    REPO_CONSTITUTION_SCHEMA,
    RepoConstitution,
    list_schemas,
)
from switchboard.contracts.schema_export import registered_v1_schemas

FIXTURE = ROOT / "fixtures" / "repo_constitution.python_modular_monolith.v1.json"
ADR = ROOT / "docs" / "decisions" / "0019-repo-constitution.md"
SCHEMA_FILE = ROOT / "schemas" / "switchboard.repo_constitution.v1.json"

passed = failed = 0


def ok(cond: bool, msg: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"FAIL  {msg}")


def test_schema_registered() -> None:
    ok(REPO_CONSTITUTION_SCHEMA == "switchboard.repo_constitution.v1",
       "schema id constant")
    ok(REPO_CONSTITUTION_SCHEMA in list_schemas(),
       "schema registered in-process")
    ok(REPO_CONSTITUTION_SCHEMA in registered_v1_schemas(),
       "schema exported in v1 registry")


def test_fixture_validates() -> None:
    ok(FIXTURE.is_file(), "reference fixture exists")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = RepoConstitution.model_validate(payload)
    ok(model.profile_id == "python_modular_monolith", "profile_id")
    ok(model.project_id == "switchboard", "project_id")
    ok(model.product_root == "src/switchboard/", "product_root")
    ok(model.test_root == "tests/", "test_root")
    ok(model.docs_front_door == "docs/INDEX.md", "docs_front_door")
    ok(model.agent_front_door == "AGENTS.md", "agent_front_door")
    ok(model.shim_policy == "timed", "shim_policy timed")
    ok(model.enforcement_mode == "warn", "enforcement_mode warn for P0")
    ok("src/switchboard/" in model.product_root, "aligned to ADR-0007 product root")
    dumped = model.model_dump(by_alias=True)
    ok(dumped.get("schema") == REPO_CONSTITUTION_SCHEMA, "wire alias schema")


def test_rejects_bad_shim_policy() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["shim_policy"] = "forever"
    try:
        RepoConstitution.model_validate(payload)
    except Exception:
        ok(True, "invalid shim_policy rejected")
    else:
        ok(False, "invalid shim_policy rejected")


def test_adr_and_schema_file() -> None:
    ok(ADR.is_file(), "ADR-0019 exists")
    text = ADR.read_text(encoding="utf-8")
    ok("repo_topology" in text and "repo_constitution" in text,
       "ADR separates constitution from topology")
    ok(SCHEMA_FILE.is_file(), "generated schema file checked in")


if __name__ == "__main__":
    test_schema_registered()
    test_fixture_validates()
    test_rejects_bad_shim_policy()
    test_adr_and_schema_file()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
