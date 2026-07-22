#!/usr/bin/env python3
"""ENFORCE-12: shared, atomic and revocation-aware AI admission."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="enforce12-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_AI_MAX_ACTIVE_PER_PRINCIPAL"] = "1"
os.environ["PM_AI_MAX_QUEUED_PER_PRINCIPAL"] = "2"
os.environ["PM_AI_MAX_PROMPTS_PER_HOUR"] = "5"
os.environ["PM_AI_MAX_PROMPTS_PER_DAY"] = "20"
os.environ.pop("PM_AI_KILL_SWITCH", None)

import scripts.switchboard_path  # noqa: E402,F401
import store  # noqa: E402
from switchboard.storage.repositories import ai_admission  # noqa: E402
from switchboard.storage.repositories import access as access_repo  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db("switchboard")
    authz = {"principal_id": "dev/operator", "dev_open": True}
    decisions = []
    denials = []
    errors = []
    lock = threading.Lock()

    def attempt():
        try:
            result = ai_admission.admit(
                project="switchboard", surface="test", authorization=authz)
            with lock:
                decisions.append(result)
        except ai_admission.AdmissionDenied as exc:
            with lock:
                denials.append(exc.decision)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ok(not errors, f"concurrent admission has no SQLite errors ({errors})")
    summary = [(d.status, d.reason_code) for d in decisions + denials]
    ok(sum(d.status == "active" for d in decisions) == 1,
       f"atomic governor admits one active job ({summary})")
    ok(sum(d.status == "queued" for d in decisions) == 2,
       "atomic governor admits two queued jobs")
    ok([d.reason_code for d in denials] == ["queue_capacity"],
       "fourth concurrent prompt is denied with an auditable reason code")

    active = next(d for d in decisions if d.status == "active")
    queued = next(d for d in decisions if d.status == "queued")
    waiting = ai_admission.authorize_execution(
        project="switchboard", admission_id=queued.admission_id, authorization=authz)
    ok(not waiting.allowed and waiting.reason_code == "waiting_for_active_slot",
       "queued work cannot start while the active slot is occupied")
    ai_admission.finish("switchboard", active.admission_id, "completed")
    promoted = ai_admission.authorize_execution(
        project="switchboard", admission_id=queued.admission_id, authorization=authz)
    ok(promoted.allowed and promoted.status == "active",
       "queued work is atomically promoted after the slot is released")

    os.environ["PM_AI_KILL_SWITCH"] = "true"
    killed = ai_admission.authorize_execution(
        project="switchboard", admission_id=promoted.admission_id, authorization=authz)
    ok(not killed.allowed and killed.reason_code == "global_kill_switch",
       "global kill switch denies already-admitted work immediately")
    os.environ.pop("PM_AI_KILL_SWITCH")

    os.environ["PM_AI_MAX_ACTIVE_PER_PRINCIPAL"] = "10"
    for _ in range(5):
        ai_admission.admit(
            project="switchboard", surface="test",
            authorization={"principal_id": "dev/hourly", "dev_open": True})
    try:
        ai_admission.admit(
            project="switchboard", surface="test",
            authorization={"principal_id": "dev/hourly", "dev_open": True})
        hourly_reason = ""
    except ai_admission.AdmissionDenied as exc:
        hourly_reason = exc.decision.reason_code
    ok(hourly_reason == "hourly_prompt_limit",
       "sixth prompt in an hour is denied by the configurable default")

    os.environ["PM_AI_MAX_PROMPTS_PER_HOUR"] = "100"
    os.environ["PM_AI_MAX_PROMPTS_PER_DAY"] = "2"
    for _ in range(2):
        ai_admission.admit(
            project="switchboard", surface="test",
            authorization={"principal_id": "dev/daily", "dev_open": True})
    try:
        ai_admission.admit(
            project="switchboard", surface="test",
            authorization={"principal_id": "dev/daily", "dev_open": True})
        daily_reason = ""
    except ai_admission.AdmissionDenied as exc:
        daily_reason = exc.decision.reason_code
    ok(daily_reason == "daily_prompt_limit",
       "daily prompt limit is enforced independently")

    os.environ["PM_AUTH_MODE"] = "required"
    os.environ["PM_AI_MAX_PROMPTS_PER_HOUR"] = "5"
    os.environ["PM_AI_MAX_PROMPTS_PER_DAY"] = "20"
    principal = access_repo.create_principal(
        "agent", "Admission test", "not-persisted-in-admission", ["read"],
        principal_id="agent/admission-test", project="switchboard")
    required_authz = {"principal_id": principal["id"], "dev_open": False}
    # Free capacity without changing the hourly ledger.
    for decision in decisions:
        ai_admission.finish("switchboard", decision.admission_id, "completed")
    admitted = ai_admission.admit(
        project="switchboard", surface="test", authorization=required_authz)
    access_repo.revoke_principal(principal["id"], project="switchboard")
    revoked = ai_admission.authorize_execution(
        project="switchboard", admission_id=admitted.admission_id,
        authorization=required_authz)
    ok(not revoked.allowed and revoked.reason_code == "authorization_revoked",
       "principal revocation is re-checked before execution")

    with store._conn("switchboard") as connection:
        columns = {row["name"] for row in connection.execute(
            "PRAGMA table_info(ai_admission_events)").fetchall()}
    ok("prompt" not in columns and "token" not in columns and "secret" not in columns,
       "admission audit schema stores no prompts, tokens, or secrets")

    # BUG-60 layering contract: a request with NO attached principal identity was
    # already authorized by the surface middleware (ACCESS-26 owns use:llm); the
    # governor must admit it on identity grounds and gate it only via the kill
    # switch and the anonymous per-principal limits -- never re-authenticate.
    anonymous = ai_admission.admit(
        project="switchboard", surface="browser_chat",
        authorization=ai_admission.authorization_snapshot({}))
    ok(anonymous.allowed,
       "an identity-less (middleware-authorized) request is admitted, not 403'd")
    ai_admission.finish("switchboard", anonymous.admission_id, "completed")

    os.environ["PM_AI_KILL_SWITCH"] = "1"
    try:
        try:
            ai_admission.admit(
                project="switchboard", surface="browser_chat",
                authorization=ai_admission.authorization_snapshot({}))
            killed = None
        except ai_admission.AdmissionDenied as exc:
            killed = exc.decision.reason_code
    finally:
        os.environ.pop("PM_AI_KILL_SWITCH", None)
    ok(killed == "global_kill_switch",
       "the kill switch still stops identity-less requests")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
