"""Atomic, revocation-aware admission for interactive shared-user AI work."""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import auth
from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.storage.repositories import access as access_repo

ACTIVE = "active"
QUEUED = "queued"
TERMINAL = frozenset({"completed", "failed", "cancelled", "denied"})


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int((os.environ.get(name) or str(default)).strip()))
    except ValueError:
        return default


def limits() -> dict[str, int]:
    return {
        "active": _limit("PM_AI_MAX_ACTIVE_PER_PRINCIPAL", 1),
        "queued": _limit("PM_AI_MAX_QUEUED_PER_PRINCIPAL", 2),
        "hour": _limit("PM_AI_MAX_PROMPTS_PER_HOUR", 5),
        "day": _limit("PM_AI_MAX_PROMPTS_PER_DAY", 20),
    }


def max_prompt_chars() -> int:
    return _limit("PM_AI_MAX_PROMPT_CHARS", 32_000)


def kill_switch_enabled() -> bool:
    return (os.environ.get("PM_AI_KILL_SWITCH") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    admission_id: str
    status: str
    reason_code: str

    def as_dict(self) -> dict[str, Any]:
        return vars(self)


class AdmissionDenied(PermissionError):
    def __init__(self, decision: AdmissionDecision):
        self.decision = decision
        super().__init__(decision.reason_code)


def _authorization_reason(project: str, authorization: Mapping[str, Any]) -> str:
    if kill_switch_enabled():
        return "global_kill_switch"
    principal_id = str(authorization.get("principal_id") or "")
    if authorization.get("dev_open"):
        return "authorized" if auth.auth_mode() == auth.DEV_OPEN else "authorization_revoked"
    principal = access_repo.get_principal_by_id(principal_id, project=project)
    if not principal:
        return "principal_not_found"
    try:
        auth.authorize_principal(principal, project, ("read",))
    except PermissionError:
        return "authorization_revoked"
    return "authorized"


def authorization_snapshot(principal: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only identity state; never copy prompts, cookies, or credentials."""
    if not principal and auth.auth_mode() == auth.DEV_OPEN:
        return {"principal_id": "dev/operator", "dev_open": True}
    return {"principal_id": str(principal.get("id") or ""),
            "dev_open": bool(principal.get("dev_open"))}


def admit(*, project: str = DEFAULT_PROJECT, surface: str,
          authorization: Mapping[str, Any], question: str = "",
          provider_secret: str = "") -> AdmissionDecision:
    """Make one atomic decision without persisting sensitive request material."""
    del provider_secret
    principal_id = str(authorization.get("principal_id") or "")
    admission_id = f"aiadmission-{uuid.uuid4().hex}"
    now = time.time()
    auth_reason = _authorization_reason(project, authorization)
    config = limits()
    def decide():
      with _conn(project) as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? AND status=?",
            (principal_id, ACTIVE)).fetchone()[0]
        queued = connection.execute(
            "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? AND status=?",
            (principal_id, QUEUED)).fetchone()[0]
        hour = connection.execute(
            "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? "
            "AND created_at>=? AND reason_code='admitted'", (principal_id, now - 3600)
        ).fetchone()[0]
        day = connection.execute(
            "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? "
            "AND created_at>=? AND reason_code='admitted'", (principal_id, now - 86400)
        ).fetchone()[0]
        if auth_reason != "authorized":
            status, reason = "denied", auth_reason
        elif not principal_id:
            status, reason = "denied", "principal_missing"
        elif len(question) > max_prompt_chars():
            status, reason = "denied", "prompt_too_large"
        elif hour >= config["hour"]:
            status, reason = "denied", "hourly_prompt_limit"
        elif day >= config["day"]:
            status, reason = "denied", "daily_prompt_limit"
        elif active < config["active"]:
            status, reason = ACTIVE, "admitted"
        elif queued < config["queued"]:
            status, reason = QUEUED, "admitted"
        else:
            status, reason = "denied", "queue_capacity"
        connection.execute(
            "INSERT INTO ai_admission_events(admission_id,principal_id,project,surface,"
            "status,reason_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (admission_id, principal_id, project, surface, status, reason, now, now))
        return status, reason
    status, reason = _write_through(project, decide)
    decision = AdmissionDecision(status in {ACTIVE, QUEUED}, admission_id, status, reason)
    if not decision.allowed:
        raise AdmissionDenied(decision)
    return decision


def bind_run(project: str, admission_id: str, run_id: str) -> None:
    with _conn(project) as connection:
        connection.execute("UPDATE ai_admission_events SET run_id=?,updated_at=? "
                           "WHERE admission_id=?", (run_id, time.time(), admission_id))


def authorize_execution(*, project: str, admission_id: str,
                        authorization: Mapping[str, Any]) -> AdmissionDecision:
    now = time.time()
    reason = _authorization_reason(project, authorization)
    def authorize():
      with _conn(project) as connection:
        row = connection.execute(
            "SELECT principal_id,status,reason_code FROM ai_admission_events "
            "WHERE admission_id=?", (admission_id,)).fetchone()
        if not row:
            return AdmissionDecision(False, admission_id, "denied", "admission_not_found")
        status = row["status"]
        if reason != "authorized":
            connection.execute("UPDATE ai_admission_events SET status='denied',reason_code=?,"
                               "updated_at=? WHERE admission_id=?", (reason, now, admission_id))
            return AdmissionDecision(False, admission_id, "denied", reason)
        if status == QUEUED:
            active = connection.execute(
                "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? AND status=?",
                (row["principal_id"], ACTIVE)).fetchone()[0]
            if active >= limits()["active"]:
                return AdmissionDecision(False, admission_id, QUEUED,
                                         "waiting_for_active_slot")
            status = ACTIVE
            connection.execute("UPDATE ai_admission_events SET status=?,updated_at=? "
                               "WHERE admission_id=?", (ACTIVE, now, admission_id))
        if status != ACTIVE:
            return AdmissionDecision(False, admission_id, status, row["reason_code"])
        return AdmissionDecision(True, admission_id, ACTIVE, "authorized")
    return _write_through(project, authorize)


def finish(project: str, admission_id: str, status: str) -> None:
    terminal = status if status in TERMINAL else "failed"
    with _conn(project) as connection:
        connection.execute("UPDATE ai_admission_events SET status=?,updated_at=? "
                           "WHERE admission_id=?", (terminal, time.time(), admission_id))


__all__ = ["ACTIVE", "QUEUED", "AdmissionDecision", "AdmissionDenied", "admit",
           "authorization_snapshot", "authorize_execution", "bind_run", "finish",
           "kill_switch_enabled", "limits", "max_prompt_chars"]
