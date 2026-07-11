#!/usr/bin/env python3
"""DELIVERABLES-13: intake gate for the deliverable create flow.

Proves that when PM_ENFORCE_DELIVERABLE_INTAKE is on, a deliverable MOVING INTO in_progress
must carry end_state + acceptance_criteria + a well-formed proof_requirements
(switchboard.deliverable_proof_requirements.v1), while legacy/off flows are unaffected.
Self-contained: runs against a throwaway DB. See docs/DELIVERABLE-CLOSURE-GATE.md.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverable-intake-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ.pop("PM_ENFORCE_DELIVERABLE_INTAKE", None)  # start from a known-off baseline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def enforce(on):
    if on:
        os.environ["PM_ENFORCE_DELIVERABLE_INTAKE"] = "1"
    else:
        os.environ.pop("PM_ENFORCE_DELIVERABLE_INTAKE", None)


PROJECT = "qa-intake"
VALID_PROOF = {
    "schema": "switchboard.deliverable_proof_requirements.v1",
    "gates": [
        {"id": "scope", "required": True},
        {"id": "harness:concurrent_load_gate", "required": True},
    ],
}


def create(**kw):
    kw.setdefault("project", PROJECT)
    project = kw.pop("project")
    return store.create_deliverable(kw, actor="test", project=project)


def is_error(res):
    return isinstance(res, dict) and bool(res.get("error"))


try:
    store.init_project_registry()
    store.create_project("Intake QA", project_id=PROJECT, actor="test")

    # ---- gate OFF (default): legacy behavior is fully preserved ----
    enforce(False)
    res = create(id="d-off", title="Off gate", status="in_progress")
    ok(not is_error(res) and res.get("status") == "in_progress",
       "gate OFF: in_progress deliverable with no intake fields is allowed (backward compat)")

    # ---- gate ON ----
    enforce(True)

    # new deliverable created directly as in_progress with nothing -> rejected
    res = create(id="d-bare", title="Bare", status="in_progress")
    ok(is_error(res) and res["error"] == "deliverable intake incomplete",
       "gate ON: new in_progress deliverable with no intake fields is rejected")
    ok(is_error(res) and any("end_state" in d for d in res.get("details", []))
       and any("acceptance_criteria" in d for d in res.get("details", []))
       and any("proof_requirements" in d for d in res.get("details", [])),
       "gate ON: rejection lists all three missing fields in details")
    ok(store.get_deliverable("d-bare", project=PROJECT) is None,
       "gate ON: rejected deliverable was NOT written")

    # proposed (not in_progress) with nothing -> allowed (gate only guards in_progress)
    res = create(id="d-proposed", title="Proposed", status="proposed")
    ok(not is_error(res) and res.get("status") == "proposed",
       "gate ON: status=proposed with no intake fields is allowed (gate only guards in_progress)")

    # a fully-formed intake -> accepted
    res = create(id="d-good", title="Good", status="in_progress",
                 end_state="Operators can verify & stamp closure.",
                 acceptance_criteria=["gate passes", "report persisted"],
                 proof_requirements=VALID_PROOF)
    ok(not is_error(res) and res.get("status") == "in_progress",
       "gate ON: complete intake (end_state + criteria + proof_requirements) is accepted")

    # transition proposed -> in_progress without fields -> rejected
    res = create(id="d-proposed", title="Proposed", status="in_progress")
    ok(is_error(res),
       "gate ON: transition proposed->in_progress without intake fields is rejected")
    ok(store.get_deliverable("d-proposed", project=PROJECT).get("status") == "proposed",
       "gate ON: rejected transition left the deliverable at proposed")

    # legacy deliverable that is ALREADY in_progress (written while gate was off) stays
    # editable: re-saving it in_progress is not re-validated
    res = create(id="d-off", title="Off gate (edited)", status="in_progress")
    ok(not is_error(res),
       "gate ON: re-saving an already-in_progress deliverable is not re-validated (legacy stays editable)")

    # ---- proof_requirements structural / gate-ref validation (gate ON) ----
    def reject_reason(proof):
        res = create(id="d-pr", title="PR", status="in_progress",
                     end_state="x", acceptance_criteria=["y"], proof_requirements=proof)
        return res.get("details", []) if is_error(res) else None

    ok(reject_reason({"gates": []}) is not None,
       "gate ON: empty gates list is rejected")
    ok(reject_reason({"gates": [{"id": "scope"}]}) is not None,
       "gate ON: gate missing 'required' is rejected")
    ok(reject_reason({"gates": [{"required": True}]}) is not None,
       "gate ON: gate missing 'id' is rejected")
    dups = reject_reason({"gates": [{"id": "scope", "required": True},
                                    {"id": "scope", "required": False}]})
    ok(dups is not None and any("duplicated" in d for d in dups),
       "gate ON: duplicate gate ids are rejected")
    badschema = reject_reason({"schema": "wrong.v9", "gates": [{"id": "scope", "required": True}]})
    ok(badschema is not None and any("schema" in d for d in badschema),
       "gate ON: wrong proof_requirements.schema is rejected")

    # acceptance_criteria present but effectively empty (whitespace) -> rejected
    res = create(id="d-blankac", title="Blank AC", status="in_progress",
                 end_state="x", acceptance_criteria=["   ", ""], proof_requirements=VALID_PROOF)
    ok(is_error(res) and any("acceptance_criteria" in d for d in res.get("details", [])),
       "gate ON: whitespace-only acceptance_criteria is rejected")

    # ---- MCP tool-arg style: fields arrive as JSON strings, must validate the same ----
    import json as _json
    res = create(id="d-strargs", title="String args", status="in_progress",
                 end_state="Ships end to end.",
                 acceptance_criteria=_json.dumps(["a", "b"]),
                 proof_requirements=_json.dumps(VALID_PROOF))
    ok(not is_error(res) and res.get("status") == "in_progress",
       "gate ON: JSON-string acceptance_criteria/proof_requirements (MCP args) validate + accept")

    res = create(id="d-strbad", title="String bad", status="in_progress",
                 end_state="", acceptance_criteria="[]", proof_requirements="{}")
    ok(is_error(res),
       "gate ON: JSON-string empty intake (MCP args) is rejected")

except Exception as exc:  # pragma: no cover - surface unexpected failures
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
