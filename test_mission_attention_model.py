#!/usr/bin/env python3
"""Attention model for the mission "Next best actions" queue.

Every generated action must carry OWNERSHIP + an attention/impact model so the UI can stop
presenting agent housekeeping, coordinator automation, and human decisions as one to-do list:
  owner_type · attention (human decision) · automatic (control plane handles it) · delivery_impact.

The headline regression guard is the "old blocked session on shipped work" case (the HARDEN-36
report the operator saw): an unsafe Work Session on an In Review / Done task is coordinator
cleanup with delivery_impact == 'none' — it must never read as attention-worthy.

Pure-function test (no DB / server). Run: `python test_mission_attention_model.py`.
"""
import os
import sys
from scripts.frontend_test_source import read_frontend_source

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import store  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  attention-model test needs optional dependency: {exc.name}")
    sys.exit(0)

_FAILURES = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _FAILURES.append(msg)


def _link(task_id, detail, project="switchboard", blocks=False):
    # Attention-model fixtures are explicit automatic candidates. COORD-8 now
    # excludes nonblocking links unless the link policy opts them in.
    return {"project_id": project, "task_id": task_id,
            "blocks_deliverable": bool(blocks),
            "metadata": {"dispatch_eligible": True},
            "role": "contributes",
            "task_detail": {**detail, "task_id": task_id}}


def _find(actions, action):
    return next((a for a in actions if a.get("action") == action), None)


REQUIRED_KEYS = {"action", "owner_type", "label", "reason", "attention", "automatic", "delivery_impact"}


def test_every_action_is_labelled():
    print("\n[1] every action carries the full attention model")
    links = [
        _link("T-READY", {"status": "Not Started", "dependency_state": {"ready": True}, "active_claims": []}),
        _link("T-REVIEW", {"status": "In Review"}),
    ]
    actions = store._mission_next_actions({}, links, None)
    check(bool(actions), "actions generated")
    for a in actions:
        check(REQUIRED_KEYS <= set(a), f"{a.get('action')} has owner/attention/impact/label")
        check(a["owner_type"] in ("agent", "coordinator", "reviewer", "project_owner"),
              f"{a['action']} owner_type is a known role ({a['owner_type']})")


def test_human_decisions_flagged():
    print("\n[2] human decisions → attention=True, owner=project_owner, automatic=False")
    actions = store._mission_next_actions({}, [], {"status": "proposed", "id": "prop-1"})
    ab = _find(actions, "approve_breakdown")
    check(ab and ab["attention"] and ab["owner_type"] == "project_owner" and not ab["automatic"],
          "approve_breakdown is a human decision")

    gated = [_link("T-GATE", {"status": "In Progress", "active_claims": [{"agent_id": "x"}],
                              "human_gate": {"blocked": True, "reason": "spend approval"}},
                   blocks=True)]
    ra = _find(store._mission_next_actions({}, gated, None), "request_human_approval")
    check(ra and ra["attention"] and ra["delivery_impact"] == "blocking",
          "request_human_approval is attention + blocking")


def test_agent_and_coordinator_automatic():
    print("\n[3] agent/coordinator work → automatic=True, attention=False")
    links = [
        _link("T-READY", {"status": "Not Started", "dependency_state": {"ready": True}, "active_claims": []}),
        _link("T-REVIEW", {"status": "In Review"}),
    ]
    actions = store._mission_next_actions({}, links, None)
    claim = _find(actions, "claim_task")
    verify = _find(actions, "verify_merge_provenance")
    check(claim and claim["owner_type"] == "agent" and claim["automatic"] and not claim["attention"],
          "claim_task is automatic agent work, not your attention")
    check(verify and verify["owner_type"] == "coordinator" and verify["automatic"],
          "verify_merge_provenance is automatic coordinator work")


def test_delivery_impact_of_ready_task():
    print("\n[4] a ready task's impact reflects whether it blocks others")
    plain = _find(store._mission_next_actions({}, [
        _link("T-A", {"status": "Not Started", "dependency_state": {"ready": True}, "active_claims": []})],
        None), "claim_task")
    check(plain["delivery_impact"] == "none", "ready, non-blocking task → impact none")
    blk = _find(store._mission_next_actions({}, [
        _link("T-B", {"status": "Not Started", "is_blocking": True,
                      "dependency_state": {"ready": True}, "active_claims": []})],
        None), "claim_task")
    check(blk["delivery_impact"] == "blocking", "ready task that blocks others → impact blocking")


def test_harden36_stale_session_is_no_impact():
    print("\n[5] REGRESSION: unsafe session on shipped work → cleanup, no impact")
    # In Review task with an unsafe historical session (the operator's HARDEN-36 sighting).
    review = [_link("HARDEN-36", {"status": "In Review",
                                  "session_health": {"status": "unsafe", "recommended_repair": "archive workspace"}})]
    actions = store._mission_next_actions({}, review, None)
    repair = _find(actions, "repair_work_session")
    check(repair is not None, "repair_work_session still surfaced (auditable, not hidden)")
    check(repair["owner_type"] == "coordinator" and repair["automatic"] and not repair["attention"],
          "it's automatic coordinator cleanup, not your attention")
    check(repair["delivery_impact"] == "none",
          "unsafe session on an In Review task has NO delivery impact")

    # Contrast: unsafe session on live, not-yet-shipped work does carry risk.
    live = [_link("T-LIVE", {"status": "In Progress", "active_claims": [{"agent_id": "x"}],
                             "session_health": {"status": "unsafe"}})]
    live_repair = _find(store._mission_next_actions({}, live, None), "repair_work_session")
    check(live_repair["delivery_impact"] == "at_risk",
          "unsafe session on live work → at_risk (contrast holds)")


def test_repair_task_link_needs_a_person():
    print("\n[6] a broken link is coordinator work that isn't automatic")
    bad = [_link("T-BAD", {"error": "linked task not found"}, blocks=True)]
    rl = _find(store._mission_next_actions({}, bad, None), "repair_task_link")
    check(rl and rl["owner_type"] == "coordinator" and not rl["automatic"] and rl["delivery_impact"] == "at_risk",
          "repair_task_link: coordinator, not automatic, at_risk")


def test_empty_deliverable_proposes_breakdown():
    print("\n[7] an empty deliverable proposes a breakdown (coordinator, automatic)")
    pb = _find(store._mission_next_actions({"milestones": []}, [], None), "propose_breakdown")
    check(pb and pb["owner_type"] == "coordinator" and pb["automatic"],
          "propose_breakdown is coordinator automation")


def test_ui_wiring():
    print("\n[8] frontend consumes the model (app.js)")
    js = read_frontend_source(os.path.dirname(os.path.abspath(__file__)))
    for needle in ("_missionActionsHtml", "_sessionImpact", "Decisions needed from you",
                   "being handled automatically", "delivery unaffected"):
        check(needle in js, f"app.js has {needle!r}")
    check("Next best actions" not in js, "the ambiguous 'Next best actions' heading is gone")


def main():
    test_every_action_is_labelled()
    test_human_decisions_flagged()
    test_agent_and_coordinator_automatic()
    test_delivery_impact_of_ready_task()
    test_harden36_stale_session_is_no_impact()
    test_repair_task_link_needs_a_person()
    test_empty_deliverable_proposes_breakdown()
    test_ui_wiring()
    print()
    print("mission attention model: " + ("all checks passed" if not _FAILURES
                                          else f"{len(_FAILURES)} check(s) FAILED"))
    sys.exit(1 if _FAILURES else 0)


if __name__ == "__main__":
    main()
