#!/usr/bin/env python3
"""Mission Bot: ordered facts → eight outputs. No classifier. No invented humans."""
from __future__ import annotations

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.domain.completion.state_machine import build_completion_snapshot
from switchboard.domain.mission_bot import (
    MissionOutput,
    build_dossier,
    reduce_mission,
)
from switchboard.domain.mission_bot.facts import agent_requires_human
from switchboard.application.mission_bot.shadow import shadow_mission
from switchboard.application.mission_bot.driver import _mission_instruction


HEAD = "a" * 40


def _agent_blocker(**extra):
    return {
        "route": "agent_requires_human",
        "reason": "missing_credentials",
        "source_tool": "agent_requires_human",
        "agent_id": "agent-mission-1",
        "actor": "agent-mission-1",
        "execution_id": "exec-1",
        "execution_generation": 1,
        **extra,
    }


def snapshot(
    *,
    draft: bool = False,
    merge_state: str = "CLEAN",
    ci: str = "SUCCESS",
    attribution: str = "product",
    review: str = "passed",
    review_head: str = HEAD,
    findings=(),
    runner=None,
    work_session=None,
    board_status: str = "In Review",
    pr_state: str = "OPEN",
    queue=None,
    check_url: str = "https://github.com/example/project/actions/runs/99",
    check_summary: str = "1 test failed",
    dependency_state=None,
    run_attempt: int = 1,
):
    task = {
        "task_id": "MISSION-1",
        "status": board_status,
        "git_state": {"head_sha": HEAD, "pr_number": 810},
    }
    if dependency_state is not None:
        task["dependency_state"] = dependency_state
    return build_completion_snapshot(
        task=task,
        github_pr={
            "number": 810, "state": pr_state, "draft": draft, "mergeable": True,
            "mergeStateStatus": merge_state, "head": {"sha": HEAD},
            "status_contexts": [{
                "context": "Switchboard CI / VM gate", "state": ci,
                "failure_attribution": attribution,
                "target_url": check_url,
                "description": check_summary,
                "run_attempt": run_attempt,
            }],
        },
        required_status_contexts=["Switchboard CI / VM gate"],
        review={"status": review, "head_sha": review_head, "number": 810},
        merge_gate={"findings": list(findings)},
        merge_queue=queue or {},
        runner=runner or {},
        work_session=work_session or {},
    )


def test_agent_requires_human_stops_without_reboot():
    snap = snapshot(ci="FAILURE")
    snap["work_session"] = {
        "status": "blocked",
        "hygiene": {"blocker": _agent_blocker()},
    }
    assert agent_requires_human(snap) is True
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.AGENT_REQUIRES_HUMAN.value
    assert cmd["reason_code"] == "missing_credentials"
    assert cmd["role"] is None


def test_legacy_human_route_without_provenance_does_not_stop():
    """Bare route=human is not an authenticated agent receipt."""
    snap = snapshot(ci="FAILURE")
    snap["work_session"] = {
        "status": "blocked",
        "hygiene": {"blocker": {"route": "human", "reason": "budget"}},
    }
    assert agent_requires_human(snap) is False
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.START_REMEDIATION.value


def test_merged_outranks_stale_human_blocker():
    snap = snapshot()
    snap["github_pr"]["state"] = "MERGED"
    snap["github_pr"]["merged"] = True
    snap["work_session"] = {
        "status": "blocked",
        "hygiene": {"blocker": _agent_blocker()},
    }
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.OBSERVE_MERGED.value


def test_queue_complete_is_not_canonical_merge():
    snap = snapshot()
    snap["merge_queue"] = {"state": "COMPLETE"}
    cmd = reduce_mission(snap)
    assert cmd["output"] != MissionOutput.OBSERVE_MERGED.value


def test_machine_ci_failure_boots_remediation_with_full_dossier():
    snap = snapshot(ci="FAILURE", attribution="authority")
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.START_REMEDIATION.value
    assert cmd["role"] == "remediation"
    dossier = cmd["dossier"]
    assert dossier["failing_check_url"].endswith("/actions/runs/99")
    assert dossier["failing_check_summary"] == "1 test failed"
    assert "Switchboard CI / VM gate" in dossier["failing_contexts"]
    assert dossier["acceptance_findings"]
    assert dossier["failing_checks"]
    assert dossier["github_pr"]["number"] == 810
    assert dossier["status_contexts"]
    # Findings present must not drop CI URL (P1 from #1015 review).
    assert dossier["acceptance_findings"][0].get("failing_check_url")


def test_dossier_and_prompt_are_not_truncated():
    findings = [
        {"code": f"finding_{i}", "blocking": True, "message": f"msg-{i}",
         "nested": {"detail": i}}
        for i in range(25)
    ]
    snap = snapshot(ci="FAILURE", findings=findings)
    cmd = reduce_mission(snap)
    dossier = cmd["dossier"]
    assert len(dossier["acceptance_findings"]) >= 25
    assert dossier["acceptance_findings"][0]["nested"]["detail"] == 0
    instruction = _mission_instruction(cmd)
    assert "DOSSIER_JSON_BEGIN" in instruction
    assert "finding_24" in instruction
    assert '"nested"' in instruction


def test_idempotency_changes_when_ci_evidence_changes():
    first = reduce_mission(snapshot(
        ci="FAILURE",
        check_url="https://github.com/example/project/actions/runs/99",
        run_attempt=1,
    ))
    second = reduce_mission(snapshot(
        ci="FAILURE",
        check_url="https://github.com/example/project/actions/runs/100",
        run_attempt=2,
    ))
    assert first["idem_key"] != second["idem_key"]
    assert first["evidence_identity"]["failing_check_urls"] != (
        second["evidence_identity"]["failing_check_urls"]
    )


def test_unmet_dependencies_wait_instead_of_start():
    snap = snapshot(
        board_status="Not Started",
        dependency_state={
            "satisfied": False,
            "blocked_by_count": 1,
            "blocking": [{"task_id": "DEP-1", "done": False}],
        },
    )
    # No PR → would otherwise START_IMPLEMENTATION.
    snap["github_pr"] = {}
    snap["pr_number"] = None
    snap["pr_identity"] = ""
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.WAIT.value
    assert cmd["reason_code"] == "unmet_dependencies"


def test_judgment_review_findings_still_remediate_not_human():
    snap = snapshot(review="changes_requested")
    snap["review"]["findings"] = [{"finding_class": "judgment", "code": "design"}]
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.START_REMEDIATION.value
    assert cmd["output"] != MissionOutput.AGENT_REQUIRES_HUMAN.value


def test_draft_marks_ready_when_ci_pending():
    cmd = reduce_mission(snapshot(draft=True, ci="IN_PROGRESS"))
    assert cmd["output"] == MissionOutput.MARK_READY.value


def test_green_arms_merge():
    cmd = reduce_mission(snapshot())
    assert cmd["output"] == MissionOutput.ARM_MERGE.value
    assert cmd["reason_code"] == "exact_head_gates_passed"


def test_live_runner_waits():
    cmd = reduce_mission(snapshot(
        runner={"live": True, "role": "remediation", "head_sha": HEAD},
        ci="FAILURE",
    ))
    assert cmd["output"] == MissionOutput.WAIT.value


def test_merged_observes_done():
    snap = snapshot()
    snap["github_pr"]["state"] = "MERGED"
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.OBSERVE_MERGED.value


def test_no_pr_starts_implementation():
    snap = build_completion_snapshot(
        task={"task_id": "MISSION-2", "status": "Not Started"},
        github_pr={},
        runner={},
    )
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.START_IMPLEMENTATION.value
    assert cmd["role"] == "implementation"


def test_shadow_is_read_only():
    result = shadow_mission(snapshot(ci="FAILURE"))
    assert result["mutates"] is False
    assert result["output"] == MissionOutput.START_REMEDIATION.value


def test_dossier_merges_findings_and_ci_identity():
    snap = snapshot(ci="FAILURE", findings=[{
        "code": "missing_executed_test_run", "blocking": True,
    }])
    dossier = build_dossier(
        snap, reason_code="missing_executed_test_run", mission="remediate",
    )
    assert dossier["failing_check_url"]
    assert dossier["acceptance_findings"][0]["code"] == "missing_executed_test_run"
    assert dossier["acceptance_findings"][0]["failing_check_url"]


def test_agent_requires_human_is_registered_claim_tool():
    from pathlib import Path
    claims_src = Path(SRC, "switchboard/mcp/tools/claims.py").read_text(
        encoding="utf-8",
    )
    auth_src = Path(SRC, "switchboard/mcp/authorization.py").read_text(
        encoding="utf-8",
    )
    assert '"agent_requires_human"' in claims_src
    assert '"agent_requires_human"' in auth_src
    # Must appear in CLAIM_TOOL_NAMES before register_claim_tools loops it.
    names_block = claims_src.split("CLAIM_TOOL_NAMES")[1].split(
        "def register_claim_tools"
    )[0]
    assert '"agent_requires_human"' in names_block


if __name__ == "__main__":
    _passed = _failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            _passed += 1
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            _failed += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\nMission Bot: {_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
