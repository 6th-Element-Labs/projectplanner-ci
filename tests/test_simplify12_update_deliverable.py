#!/usr/bin/env python3
"""SIMPLIFY-12: deliverables have one audited partial-update command."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="simplify12-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402
from switchboard.application.commands import update_deliverable as command  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += bool(condition)
    failed += not bool(condition)


try:
    store.init_db("switchboard")
    created = store.create_deliverable({
        "id": "simplify-12-proof", "title": "Before", "status": "proposed",
        "end_state": "Old contract", "why_it_matters": "Old purpose",
        "metadata": {"kept": False},
    }, actor="fixture", project="switchboard")
    ok(created.get("id") == "simplify-12-proof", "fixture deliverable created")

    updated = command.execute_mapping_result("simplify-12-proof", {
        "title": "After", "status": "in_review", "end_state": "Correct contract",
        "purpose": "Current purpose", "metadata": {"superseded_in_part": True},
    }, actor="codex/SIMPLIFY-12", project="switchboard")
    ok(updated.get("title") == "After" and updated.get("status") == "in_review",
       "shared command updates title and validated status")
    ok(updated.get("end_state") == "Correct contract"
       and updated.get("why_it_matters") == "Current purpose",
       "shared command updates contract fields")
    ok(updated.get("metadata", {}).get("superseded_in_part") is True,
       "shared command updates metadata")

    rejected = command.execute_mapping_result(
        "simplify-12-proof", {"status": "imaginary"},
        actor="codex/SIMPLIFY-12", project="switchboard")
    ok(rejected.get("error") == "invalid status", "invalid status fails closed")
    after_rejection = store.get_deliverable("simplify-12-proof", project="switchboard")
    ok(after_rejection.get("status") == "in_review", "rejected transition does not write")

    with store._conn("switchboard") as connection:
        event = connection.execute(
            "SELECT actor, payload FROM activity WHERE kind='deliverable.updated' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(event["payload"])
    ok(event["actor"] == "codex/SIMPLIFY-12", "activity records the actor")
    ok(set(payload["changes"]) == {"end_state", "metadata", "purpose", "status", "title"},
       "activity records every changed field")

    router_source = (ROOT / "src/switchboard/api/routers/deliverables.py").read_text()
    mcp_source = (ROOT / "src/switchboard/mcp/tools/deliverables.py").read_text()
    ok('@router.patch("/api/deliverables/{deliverable_id}")' in router_source
       and "update_deliverable_command.execute_mapping_result" in router_source,
       "REST PATCH route uses the shared command")
    ok('"update_deliverable"' in mcp_source
       and "update_deliverable_command.execute_mapping_result" in mcp_source,
       "MCP adapter registers and uses the shared command")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
