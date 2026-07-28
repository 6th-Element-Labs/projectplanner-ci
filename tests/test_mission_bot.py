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
        # Server-stamped resolve_write_actor binding — not a bare actor string.
        "binding": "registered_agent",
        "provenance_stamp": "switchboard.resolve_write_actor.v1",
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


def test_legacy_blocker_tool_cannot_stop_even_with_copied_agent_stamp():
    """Only the canonical explicit MCP receipt is attention authority."""
    snap = snapshot(ci="FAILURE")
    blocker = _agent_blocker(source_tool="record_human_blocker")
    snap["work_session"] = {
        "status": "blocked",
        "hygiene": {"blocker": blocker},
    }
    assert agent_requires_human(snap) is False
    assert reduce_mission(snap)["output"] == MissionOutput.START_REMEDIATION.value


def test_forged_actor_string_without_server_binding_does_not_stop():
    """Tool name + arbitrary actor string must not mint a human stop."""
    snap = snapshot(ci="FAILURE")
    snap["work_session"] = {
        "status": "blocked",
        "hygiene": {
            "blocker": {
                "route": "agent_requires_human",
                "reason": "forged",
                "source_tool": "agent_requires_human",
                "actor": "not-an-agent",
                "agent_id": "not-an-agent",
            },
        },
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
    assert cmd["task_id"] == "MISSION-1"
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


def test_canonical_task_id_reaches_start_task_unchanged():
    """A dispatch must never normalize the canonical board task identifier."""
    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command

    plans = []
    ports = MissionPorts(
        start_task=lambda plan: plans.append(dict(plan)) or {"action": "started"},
        mark_ready=lambda _plan: {"returncode": 0},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_kwargs: {},
        persist_agent_requires_human=lambda **_kwargs: {},
        observe_merged=lambda **_kwargs: {},
    )
    cmd = reduce_mission(snapshot(ci="FAILURE"))
    cmd.pop("idem_key", None)
    cmd.pop("idempotency_key", None)
    result = execute_mission_command(
        cmd,
        ports=ports,
        project="switchboard",
        actor="test",
    )
    assert result["receipt"]["verified"] is True
    assert len(plans) == 1
    assert plans[0]["task_id"] == "MISSION-1"


def test_all_start_commands_preserve_canonical_task_id():
    remediation = reduce_mission(snapshot(ci="FAILURE"))
    review = reduce_mission(snapshot(review="missing"))
    implementation_snap = build_completion_snapshot(
        task={"task_id": "COORD-98", "status": "Not Started"},
        github_pr={},
        runner={},
    )
    implementation = reduce_mission(implementation_snap)

    assert remediation["output"] == MissionOutput.START_REMEDIATION.value
    assert review["output"] == MissionOutput.START_REVIEW.value
    assert implementation["output"] == MissionOutput.START_IMPLEMENTATION.value
    assert {
        remediation["task_id"],
        review["task_id"],
        implementation["task_id"],
    } == {"MISSION-1", "COORD-98"}


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
    assert "factory failures are your mission to diagnose and repair" in instruction
    assert "concrete question that requires an operator answer" in instruction


def test_prompt_redacts_execution_environment_secrets():
    snap = snapshot(ci="FAILURE")
    snap["work_session"] = {
        "status": "active",
        "env": {"GH_TOKEN": "ghs_secret", "api_key": "sk-test"},
        "session_token": "wst-secret",
    }
    snap["runner"] = {
        "live": False,
        "env": {"GITHUB_TOKEN": "ghs_other"},
        "relay_ticket": "ticket-secret",
    }
    snap["task"] = {
        **snap.get("task", {}),
        "credentials": {"token": "should-not-leak"},
    }
    cmd = reduce_mission(snap)
    # In-memory dossier keeps full evidence surfaces for operators/identity.
    assert cmd["dossier"]["work_session"]["env"]["GH_TOKEN"] == "ghs_secret"
    instruction = _mission_instruction(cmd)
    assert "ghs_secret" not in instruction
    assert "sk-test" not in instruction
    assert "wst-secret" not in instruction
    assert "ticket-secret" not in instruction
    assert "should-not-leak" not in instruction
    assert "<redacted>" in instruction
    assert "finding_" in instruction or "required_exact_head_ci_failed" in instruction


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


def test_idempotency_changes_when_review_or_finding_details_change():
    base = snapshot(review="changes_requested")
    base["review"]["findings"] = [{
        "code": "design", "message": "needs redesign", "blocking": True,
    }]
    first = reduce_mission(base)
    changed = snapshot(review="changes_requested")
    changed["review"]["findings"] = [{
        "code": "design", "message": "needs different redesign", "blocking": True,
    }]
    second = reduce_mission(changed)
    assert first["idem_key"] != second["idem_key"]
    assert first["evidence_identity"]["review_digest"] != (
        second["evidence_identity"]["review_digest"]
    )
    assert first["evidence_identity"]["findings_digest"] != (
        second["evidence_identity"]["findings_digest"]
    )


def test_idempotency_ignores_elapsed_review_clock():
    """An unchanged red head is one mission even while its wait clock advances."""
    first_snap = snapshot(ci="FAILURE", findings=[{
        "code": "review_stalled_no_verdict",
        "message": "No review verdict in 30 min.",
        "blocking": True,
        "head_sha": HEAD,
        "waited_seconds": 1807.7,
        "review_gate": {
            "code": "review_required",
            "head_sha": HEAD,
            "waited_seconds": 1807.7,
        },
    }])
    second_snap = snapshot(ci="FAILURE", findings=[{
        "code": "review_stalled_no_verdict",
        "message": "No review verdict in 31 min.",
        "blocking": True,
        "head_sha": HEAD,
        "waited_seconds": 1868.9,
        "review_gate": {
            "code": "review_required",
            "head_sha": HEAD,
            "waited_seconds": 1868.9,
        },
    }])
    first = reduce_mission(first_snap)
    second = reduce_mission(second_snap)
    assert first["idem_key"] == second["idem_key"]
    # The dossier remains complete for the remediation agent.
    assert first["dossier"]["acceptance_findings"][0]["waited_seconds"] == 1807.7
    assert second["dossier"]["acceptance_findings"][0]["waited_seconds"] == 1868.9


def test_ledger_identity_uses_stable_mission_key_not_full_dossier():
    """Volatile dossier detail cannot mint a second external-effect key."""
    from unittest.mock import patch

    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command

    claimed_payloads = []

    def claim(*_args, **kwargs):
        claimed_payloads.append(dict(_args[3]))
        return {
            "claimed": True,
            "effect_key": "effect-one",
            "effect": {"effect_key": "effect-one", "status": "claimed"},
        }

    ports = MissionPorts(
        start_task=lambda _plan: {"action": "started"},
        mark_ready=lambda _plan: {"returncode": 0},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_k: {},
        persist_agent_requires_human=lambda **_k: {},
        observe_merged=lambda **_k: {},
    )
    base = {
        "output": MissionOutput.START_REMEDIATION.value,
        "task_id": "MISSION-1",
        "head_sha": HEAD,
        "role": "remediation",
        "reason_code": "required_exact_head_ci_failed",
        "idem_key": "mission:MISSION-1:stable",
        "evidence_identity": {"finding_codes": ["required_ci_failed"]},
    }
    with (
        patch(
            "switchboard.storage.repositories.external_effects.claim_external_effect",
            side_effect=claim,
        ),
        patch(
            "switchboard.storage.repositories.external_effects.verify_external_effect",
            return_value={},
        ),
    ):
        execute_mission_command(
            {**base, "dossier": {"waited_seconds": 1807.7}},
            ports=ports, project="switchboard", actor="test",
        )
        execute_mission_command(
            {**base, "dossier": {"waited_seconds": 1868.9}},
            ports=ports, project="switchboard", actor="test",
        )
    assert claimed_payloads[0] == claimed_payloads[1]
    assert "dossier" not in claimed_payloads[0]


def test_raised_start_task_failure_closes_claimed_effect():
    """A port exception is a failed receipt, never an immortal claimed row."""
    from unittest.mock import patch

    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command

    failed = []
    ports = MissionPorts(
        start_task=lambda _plan: (_ for _ in ()).throw(
            RuntimeError("runner admission exploded")
        ),
        mark_ready=lambda _plan: {"returncode": 0},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_k: {},
        persist_agent_requires_human=lambda **_k: {},
        observe_merged=lambda **_k: {},
    )
    with (
        patch(
            "switchboard.storage.repositories.external_effects.claim_external_effect",
            return_value={
                "claimed": True,
                "effect_key": "effect-failed",
                "effect": {
                    "effect_key": "effect-failed",
                    "status": "claimed",
                },
            },
        ),
        patch(
            "switchboard.storage.repositories.external_effects.fail_external_effect",
            side_effect=lambda *args, **kwargs: failed.append((args, kwargs)),
        ),
    ):
        try:
            execute_mission_command(
                {
                    "output": MissionOutput.START_REMEDIATION.value,
                    "task_id": "MISSION-1",
                    "head_sha": HEAD,
                    "role": "remediation",
                    "reason_code": "required_exact_head_ci_failed",
                    "idem_key": "mission:MISSION-1:failure",
                    "dossier": {},
                },
                ports=ports,
                project="switchboard",
                actor="test",
            )
        except RuntimeError as exc:
            assert "runner admission exploded" in str(exc)
        else:
            raise AssertionError("start_task exception was swallowed")
    assert len(failed) == 1
    assert failed[0][0][0] == "effect-failed"
    assert "RuntimeError: runner admission exploded" in failed[0][0][1]


def test_failed_boot_receipt_is_reclaimed_for_next_agent_boot():
    """A factory boot failure retries through one CAS; it never becomes WAIT."""
    from unittest.mock import patch

    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command

    starts = []
    ports = MissionPorts(
        start_task=lambda plan: starts.append(dict(plan)) or {"action": "started"},
        mark_ready=lambda _plan: {"returncode": 0},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_k: {},
        persist_agent_requires_human=lambda **_k: {},
        observe_merged=lambda **_k: {},
    )
    failed_row = {
        "effect_key": "effect-retry",
        "status": "failed",
        "retry_count": 1,
        "last_error": "runner admission exploded",
    }
    with (
        patch(
            "switchboard.storage.repositories.external_effects.claim_external_effect",
            return_value={
                "claimed": False,
                "effect_key": "effect-retry",
                "effect": failed_row,
            },
        ),
        patch(
            "switchboard.storage.repositories.external_effects.retry_external_effect",
            return_value={
                "claimed": True,
                "effect_key": "effect-retry",
                "effect": failed_row,
            },
        ) as retry,
        patch(
            "switchboard.storage.repositories.external_effects.verify_external_effect",
            return_value={},
        ),
    ):
        result = execute_mission_command(
            {
                "output": MissionOutput.START_REMEDIATION.value,
                "task_id": "MISSION-1",
                "head_sha": HEAD,
                "role": "remediation",
                "reason_code": "required_exact_head_ci_failed",
                "idem_key": "mission:MISSION-1:retry",
                "dossier": {},
            },
            ports=ports,
            project="switchboard",
            actor="test",
        )
    retry.assert_called_once_with(
        "effect-retry",
        expected_retry_count=1,
        actor="test",
        project="switchboard",
    )
    assert len(starts) == 1
    assert result["receipt"]["verified"] is True


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



def test_passed_review_from_replaced_pr_does_not_arm_merge():
    """Same-head review on a replaced PR URL must not arm merge."""
    from switchboard.domain.mission_bot.facts import review_passed
    snap = snapshot()
    snap["pr_url"] = "https://github.com/example/project/pull/810"
    snap["github_pr"]["url"] = snap["pr_url"]
    snap["review"]["pr_url"] = "https://github.com/example/project/pull/999"
    assert review_passed(snap) is False
    cmd = reduce_mission(snap)
    assert cmd["output"] == MissionOutput.START_REVIEW.value


def test_passed_review_without_head_sha_does_not_arm_merge():
    """Exact-head review is required — a headless pass must not arm merge."""
    from switchboard.domain.mission_bot.facts import review_passed

    snap = snapshot(review_head="")
    assert review_passed(snap) is False
    cmd = reduce_mission(snap)
    assert cmd["output"] != MissionOutput.ARM_MERGE.value
    assert cmd["output"] == MissionOutput.START_REVIEW.value


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


def test_failed_effect_receipt_is_not_verified():
    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command

    ports = MissionPorts(
        start_task=lambda _plan: {"error": "boom"},
        mark_ready=lambda _plan: {"returncode": 1, "stderr": "gh failed"},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_k: {},
        persist_agent_requires_human=lambda **_k: {},
        observe_merged=lambda **_k: {"reconcile": {"error": "pr_state_unavailable"}},
    )
    failed_ready = execute_mission_command(
        {
            "output": MissionOutput.MARK_READY.value,
            "task_id": "MISSION-1",
            "pr_number": 1,
            "head_sha": HEAD,
        },
        ports=ports,
        project="switchboard",
        actor="test",
    )
    assert failed_ready["receipt"]["verified"] is False
    assert failed_ready["receipt"]["pending"] is False
    assert failed_ready["receipt"]["error"]

    failed_merge = execute_mission_command(
        {
            "output": MissionOutput.OBSERVE_MERGED.value,
            "task_id": "MISSION-1",
            "head_sha": HEAD,
        },
        ports=ports,
        project="switchboard",
        actor="test",
    )
    assert failed_merge["receipt"]["verified"] is False
    assert "pr_state_unavailable" in str(failed_merge["receipt"]["error"])


def test_pending_effect_receipt_is_not_verified():
    """In-flight / transitioning boots must stay pending — never verified."""
    from switchboard.domain.mission_bot import MissionPorts, execute_mission_command
    from switchboard.domain.mission_bot import adapter as mission_adapter

    ports = MissionPorts(
        start_task=lambda _plan: {"action": "transitioning"},
        mark_ready=lambda _plan: {"returncode": 0},
        arm_merge=lambda _plan: {"returncode": 0},
        persist_wait=lambda **_k: {},
        persist_agent_requires_human=lambda **_k: {},
        observe_merged=lambda **_k: {},
    )
    pending_start = execute_mission_command(
        {
            "output": MissionOutput.START_REMEDIATION.value,
            "task_id": "MISSION-1",
            "head_sha": HEAD,
            "role": "remediation",
            "reason_code": "required_exact_head_ci_failed",
            "dossier": {},
        },
        ports=ports,
        project="switchboard",
        actor="test",
    )
    assert pending_start["receipt"]["pending"] is True
    assert pending_start["receipt"]["verified"] is False

    # Ledger in-flight replay must also stay pending / unverified.
    original_replay = mission_adapter._ledger_replay

    def fake_replay(**_kwargs):
        return {
            "claimed": False,
            "ledger": {"effect_key": "effect-pending-1", "status": "issued"},
            "result": {"action": "awaiting_readback"},
            "idempotent_replay": True,
            "verified": False,
            "pending": True,
        }

    mission_adapter._ledger_replay = fake_replay
    try:
        in_flight = execute_mission_command(
            {
                "output": MissionOutput.START_REMEDIATION.value,
                "task_id": "MISSION-1",
                "head_sha": HEAD,
                "idem_key": "mission:pending:1",
                "dossier": {},
            },
            ports=ports,
            project="switchboard",
            actor="test",
        )
    finally:
        mission_adapter._ledger_replay = original_replay
    assert in_flight["receipt"]["pending"] is True
    assert in_flight["receipt"]["verified"] is False
    assert in_flight["receipt"]["idempotent_replay"] is True


def test_observe_merged_reconciles_when_live_store_injected():
    """Production coordinators pass store_mod=self.store; reconcile must still run."""
    from unittest.mock import patch

    from switchboard.application.mission_bot.driver import production_mission_ports

    calls = []
    decisions = []

    class LiveStore:
        def get_task(self, *_a, **_k):
            return {"task_id": "MISSION-1"}

    def fake_execute(task_id, **kwargs):
        calls.append(task_id)
        return {"merged": True, "task_id": task_id}

    def capture_ensure(**kwargs):
        decisions.append(dict(kwargs.get("decision") or {}))
        return {"run_id": "run-1"}

    with (
        patch(
            "switchboard.application.commands.reconcile_task_merge.execute",
            side_effect=fake_execute,
        ),
        patch(
            "switchboard.application.completion_driver.ensure_completion_run",
            side_effect=capture_ensure,
        ),
    ):
        ports = production_mission_ports(
            project="switchboard",
            actor="test",
            agent_id="agent/test",
            store_mod=LiveStore(),
        )
        result = ports.observe_merged(
            command={
                "task_id": "MISSION-1",
                "head_sha": HEAD,
                "reason_code": "canonical_pr_merged",
            },
            snapshot={"task_id": "MISSION-1", "head_sha": HEAD},
            run={},
            project="switchboard",
            actor="test",
        )

    assert calls == ["MISSION-1"]
    assert result["reconcile"].get("merged") is True
    assert decisions[-1]["board_projection"] == "Done"


def test_failed_reconcile_does_not_persist_done_projection():
    from unittest.mock import patch

    from switchboard.application.mission_bot.driver import production_mission_ports

    decisions = []

    class LiveStore:
        def get_task(self, *_a, **_k):
            return {"task_id": "MISSION-1"}

    def capture_ensure(**kwargs):
        decisions.append(dict(kwargs.get("decision") or {}))
        return {"run_id": "run-1"}

    with (
        patch(
            "switchboard.application.commands.reconcile_task_merge.execute",
            return_value={"error": "pr_state_unavailable"},
        ),
        patch(
            "switchboard.application.completion_driver.ensure_completion_run",
            side_effect=capture_ensure,
        ),
    ):
        ports = production_mission_ports(
            project="switchboard",
            actor="test",
            agent_id="agent/test",
            store_mod=LiveStore(),
        )
        result = ports.observe_merged(
            command={
                "task_id": "MISSION-1",
                "head_sha": HEAD,
                "reason_code": "canonical_pr_merged",
            },
            snapshot={
                "task_id": "MISSION-1",
                "head_sha": HEAD,
                "board_status": "In Review",
            },
            run={},
            project="switchboard",
            actor="test",
        )

    assert result["action"] == "reconcile_failed"
    assert result["error"] == "pr_state_unavailable"
    assert decisions[-1]["board_projection"] != "Done"
    assert decisions[-1]["board_projection"] == "In Review"
    assert decisions[-1]["reason_code"] == "reconcile_failed"


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
