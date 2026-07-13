#!/usr/bin/env python3
"""COORD-3 — structured coordinator decision log / explainable planner."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="coord3-decisions-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decisions_store  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")

    # Schema columns exist after additive migrations.
    with store._conn("switchboard") as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(decisions)").fetchall()}
    for col in ("decision_key", "decision_kind", "deliverable_id", "coordinator_agent_id",
                "inputs_json", "policy_rule", "chosen_action_json",
                "skipped_alternatives_json", "result_json"):
        ok(col in cols, f"decisions.{col} present")

    first = store.record_coordinator_decision(
        author="agent/coordinator",
        title="Skip blocked merge",
        inputs_snapshot={"candidates": [{"action": "merge", "pr": 12},
                                        {"action": "escalate", "reason": "red_ci"}]},
        policy_rule="coord.merge.fail_closed_red_ci",
        chosen_action={"action": "escalate", "reason": "ci_red"},
        skipped_alternatives=[{"action": "merge", "reason": "checks_red"}],
        result={"status": "human_required"},
        project="switchboard",
        task_id="COORD-7",
        deliverable_id="deliverable-autopilot",
        coordinator_agent_id="agent/coordinator",
        decision_kind="human_escalation",
        stable_key="tick-autopilot-1",
    )
    ok(not first.get("error"), "record_coordinator_decision succeeds")
    ok(str(first.get("decision_id", "")).startswith("coorddec-"),
       "stable decision_id uses coorddec- prefix")
    ok(first.get("created") is True, "first write creates a row")
    ok(first.get("schema") == "switchboard.coordinator_decision.v1",
       "schema stamped on coordinator records")
    ok(first.get("policy_rule") == "coord.merge.fail_closed_red_ci",
       "policy_rule persisted")
    ok(first.get("chosen_action", {}).get("action") == "escalate",
       "chosen_action decoded")
    ok(first.get("skipped_alternatives", [])[0].get("action") == "merge",
       "skipped_alternatives decoded")
    ok(first.get("inputs_snapshot", {}).get("candidates"),
       "inputs_snapshot decoded")
    ok(first.get("result", {}).get("status") == "human_required",
       "result decoded")

    replay = store.record_coordinator_decision(
        author="agent/coordinator",
        title="Skip blocked merge (retry)",
        inputs_snapshot={"candidates": [{"action": "merge"}]},  # different inputs
        policy_rule="coord.merge.fail_closed_red_ci",
        chosen_action={"action": "escalate", "reason": "ci_red"},
        skipped_alternatives=[{"action": "merge", "reason": "checks_red"}],
        result={"status": "human_required", "retried": True},
        project="switchboard",
        task_id="COORD-7",
        deliverable_id="deliverable-autopilot",
        coordinator_agent_id="agent/coordinator",
        decision_kind="human_escalation",
        stable_key="tick-autopilot-1",
    )
    ok(replay.get("idempotent") is True and replay.get("created") is False,
       "stable_key replay is idempotent")
    ok(replay.get("decision_id") == first.get("decision_id"),
       "idempotent replay returns the same decision_id")
    ok(replay.get("id") == first.get("id"),
       "idempotent replay returns the same integer id")

    listed = store.list_coordinator_decisions(
        deliverable_id="deliverable-autopilot", project="switchboard")
    ok(len(listed) == 1, "list_coordinator_decisions returns one keyed row")
    ok(listed[0].get("decision_id") == first.get("decision_id"),
       "list trail exposes decision_id")

    by_key = store.get_decision(first["decision_id"], project="switchboard")
    ok(by_key and by_key.get("id") == first.get("id"),
       "get_decision accepts stable decision_id")

    # Legacy ADR-lite path still works and projects empty structured fields.
    legacy = store.record_decision(
        task_id="ARCH-1", author="human", title="Keep SQLite",
        context="Need durable store", decision="Use SQLite WAL",
        rationale="Simple ops", project="switchboard",
    )
    ok(legacy.get("created") is True and legacy.get("decision_id", "").startswith("decision-"),
       "legacy record_decision still appends")
    legacy_list = store.list_decisions(project="switchboard")
    ok(len(legacy_list) >= 2, "list_decisions includes legacy + coordinator rows")
    coord_only = store.list_coordinator_decisions(project="switchboard")
    ok(all(r.get("decision_key") or r.get("decision_id", "").startswith("coorddec-")
           for r in coord_only),
       "list_coordinator_decisions excludes unkeyed legacy rows")

    # Deterministic id without stable_key collapses identical snapshots.
    a = decisions_store.coordinator_decision_id(
        project="switchboard", decision_kind="skip",
        inputs_snapshot={"x": 1}, policy_rule="r", chosen_action={"action": "skip"})
    b = decisions_store.coordinator_decision_id(
        project="switchboard", decision_kind="skip",
        inputs_snapshot={"x": 1}, policy_rule="r", chosen_action={"action": "skip"})
    c = decisions_store.coordinator_decision_id(
        project="switchboard", decision_kind="skip",
        inputs_snapshot={"x": 2}, policy_rule="r", chosen_action={"action": "skip"})
    ok(a == b and a != c, "snapshot-based decision ids are deterministic and sensitive")

    bad = store.record_coordinator_decision(
        author="agent/coordinator",
        title="bad",
        inputs_snapshot={},
        policy_rule="",
        chosen_action={"action": "x"},
        skipped_alternatives=[],
        result={},
        project="switchboard",
    )
    ok(bad.get("error") == "policy_rule_required", "missing policy_rule fails closed")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
