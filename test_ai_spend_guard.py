"""DOGFOOD-21: adversarial proof for shared-user AI spend containment.

This gate intentionally exercises the public admission/reservation seams instead of
calling a provider.  A denied request must be stopped before ``agent.run`` and must
leave both provider usage and persisted secret/prompt residue unchanged.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import scripts.switchboard_path  # noqa: F401,E402


TMP = Path(tempfile.mkdtemp(prefix="dogfood21-ai-spend-"))
os.environ.update({
    "PM_DB_PATH": str(TMP / "maxwell.db"),
    "PM_HELM_DB_PATH": str(TMP / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(TMP / "registry.db"),
    "PM_AUTH_MODE": "dev-open",
    "PM_AI_MAX_ACTIVE_PER_PRINCIPAL": "1",
    "PM_AI_MAX_QUEUED_PER_PRINCIPAL": "1",
    "PM_AI_MAX_PROMPTS_PER_HOUR": "100",
    "PM_AI_MAX_PROMPTS_PER_DAY": "100",
})

import store  # noqa: E402
from switchboard.storage.repositories import ai_admission  # noqa: E402


PROJECT = "switchboard"
PRINCIPAL = "user/shared-ai-dogfood"
AUTHORIZATION = {"principal_id": PRINCIPAL, "dev_open": True}
SECRET_MARKER = "sk-dogfood21-must-never-persist"
PROMPT_MARKER = "dogfood21-private-prompt-must-never-persist"

store.init_project_registry()
store.init_db(PROJECT)


def _counts() -> dict[str, int]:
    with store._conn(PROJECT) as connection:
        return {
            "usage": connection.execute("SELECT COUNT(*) FROM llm_spend").fetchone()[0],
            "reservations": connection.execute(
                "SELECT COUNT(*) FROM spend_reservations").fetchone()[0],
        }


def _deny(**kwargs):
    try:
        ai_admission.admit(
            project=PROJECT,
            surface=kwargs.pop("surface", "dogfood"),
            authorization=kwargs.pop("authorization", AUTHORIZATION),
            **kwargs,
        )
    except ai_admission.AdmissionDenied as exc:
        return exc.decision
    raise AssertionError("request unexpectedly admitted")


def _finish_all() -> None:
    with store._conn(PROJECT) as connection:
        ids = [row[0] for row in connection.execute(
            "SELECT admission_id FROM ai_admission_events "
            "WHERE principal_id=? AND status IN ('active','queued')", (PRINCIPAL,))]
    for admission_id in ids:
        ai_admission.finish(PROJECT, admission_id, "cancelled")


def test_rest_and_mcp_are_guarded_before_enqueue():
    rest = (ROOT / "src/switchboard/api/routers/plan_chat.py").read_text()
    mcp = (ROOT / "src/switchboard/mcp/tools/plan.py").read_text()
    jobs = (ROOT / "background_jobs.py").read_text()
    assert "ai_admission.admit(" in rest and 'surface="browser_chat"' in rest
    assert "ai_admission.admit(" in mcp and 'surface="mcp_ask_plan"' in mcp
    assert "authorize_execution(" in jobs
    assert rest.index("ai_admission.admit(") < rest.index("enqueue_background_job(")
    assert mcp.index("ai_admission.admit(") < mcp.index("enqueue_background_job(")


def test_concurrent_callers_are_atomically_bounded():
    _finish_all()

    def attempt(number: int) -> str:
        try:
            return ai_admission.admit(
                project=PROJECT, surface=f"concurrent-{number}",
                authorization=AUTHORIZATION).status
        except ai_admission.AdmissionDenied as exc:
            return exc.decision.reason_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(16)))
    assert outcomes.count("active") == 1, outcomes
    assert outcomes.count("queued") == 1, outcomes
    assert outcomes.count("queue_capacity") == 14, outcomes


def test_queue_resume_retry_revocation_and_kill_switch():
    _finish_all()
    active = ai_admission.admit(
        project=PROJECT, surface="browser_chat", authorization=AUTHORIZATION)
    queued = ai_admission.admit(
        project=PROJECT, surface="mcp_ask_plan", authorization=AUTHORIZATION)
    assert active.status == "active" and queued.status == "queued"
    waiting = ai_admission.authorize_execution(
        project=PROJECT, admission_id=queued.admission_id,
        authorization=AUTHORIZATION)
    assert not waiting.allowed and waiting.reason_code == "waiting_for_active_slot"
    ai_admission.finish(PROJECT, active.admission_id, "completed")
    resumed = ai_admission.authorize_execution(
        project=PROJECT, admission_id=queued.admission_id,
        authorization=AUTHORIZATION)
    retry = ai_admission.authorize_execution(
        project=PROJECT, admission_id=queued.admission_id,
        authorization=AUTHORIZATION)
    assert resumed.allowed and retry.allowed and resumed.admission_id == retry.admission_id
    ai_admission.finish(PROJECT, queued.admission_id, "completed")

    original = ai_admission._authorization_reason
    try:
        ai_admission._authorization_reason = lambda *_args, **_kwargs: "authorization_revoked"
        revoked = _deny(surface="queued_worker")
        assert revoked.reason_code == "authorization_revoked"
    finally:
        ai_admission._authorization_reason = original

    os.environ["PM_AI_KILL_SWITCH"] = "true"
    try:
        killed = _deny(surface="browser_chat")
        assert killed.reason_code == "global_kill_switch"
    finally:
        os.environ.pop("PM_AI_KILL_SWITCH", None)


def test_oversized_prompt_and_role_denial_do_not_reach_provider():
    before = _counts()
    oversized = _deny(question="x" * (ai_admission.max_prompt_chars() + 1))
    assert oversized.reason_code == "prompt_too_large"

    original = ai_admission._authorization_reason
    try:
        ai_admission._authorization_reason = lambda *_args, **_kwargs: "role_denied"
        denied = _deny(question="small")
        assert denied.reason_code == "role_denied"
    finally:
        ai_admission._authorization_reason = original
    assert _counts() == before


def test_tiny_allowance_denial_adds_exactly_zero_cost():
    """Production-shaped provider attempt: reserve worst case before any call."""
    _finish_all()
    before = _counts()
    store.set_spend_envelope(PRINCIPAL, "0.000001", "0.000001", project=PROJECT)
    denied = store.reserve_spend(
        PRINCIPAL, "dogfood21-provider-attempt", "0.000002",
        {"provider": "openai", "model": "gpt-production-shaped"}, project=PROJECT)
    after = _counts()
    assert denied["failure_class"] == "budget_exceeded"
    assert after["usage"] == before["usage"]
    assert after["reservations"] == before["reservations"]


def test_denials_leave_no_prompt_secret_or_bearer_residue():
    before = _counts()
    os.environ["PM_AI_KILL_SWITCH"] = "1"
    try:
        decision = _deny(
            surface="residue_scan", question=PROMPT_MARKER,
            provider_secret=SECRET_MARKER)
        assert decision.reason_code == "global_kill_switch"
    finally:
        os.environ.pop("PM_AI_KILL_SWITCH", None)
    assert _counts() == before
    database_bytes = (TMP / "switchboard.db").read_bytes()
    assert PROMPT_MARKER.encode() not in database_bytes
    assert SECRET_MARKER.encode() not in database_bytes
    rows = []
    with store._conn(PROJECT) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM ai_admission_events WHERE principal_id=?", (PRINCIPAL,))]
    serialized = json.dumps(rows, sort_keys=True)
    assert PROMPT_MARKER not in serialized and SECRET_MARKER not in serialized


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"DOGFOOD-21 AI spend guard: {len(tests)} proofs passed")
