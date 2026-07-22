"""Atomic per-principal admission for interactive AI work.

The ledger deliberately stores no prompt, bearer, cookie, or provider secret.
"""
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
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        return max(0, int(raw))
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
        return {
            "allowed": self.allowed,
            "admission_id": self.admission_id,
            "status": self.status,
            "reason_code": self.reason_code,
        }


class AdmissionDenied(PermissionError):
    def __init__(self, decision: AdmissionDecision):
        self.decision = decision
        super().__init__(decision.reason_code)


def _authorization_reason(project: str, authorization: Mapping[str, Any]) -> str:
    if kill_switch_enabled():
        return "global_kill_switch"
    principal_id = str(authorization.get("principal_id") or "")
    if (not principal_id and not authorization.get("dev_open")
            and not authorization.get("environment_operator")
            and not authorization.get("principal_kind")):
        # No principal identity was attached to the request. Authorization is the
        # surface middleware's job (ACCESS-26 use:llm scope); this governor owns
        # admission only. There is no identity to re-check or revoke here, so the
        # kill switch and the per-principal limits (keyed to the anonymous bucket)
        # are the applicable gates -- denying would re-authenticate at the wrong
        # layer and 403 requests the auth gate already admitted (BUG-60 contract).
        return "authorized"
    if authorization.get("dev_open"):
        return "authorized" if auth.auth_mode() == auth.DEV_OPEN else "authorization_revoked"
    if authorization.get("environment_operator"):
        env_name = {
            "env-auth-token": "PM_AUTH_TOKEN",
            "env-mcp-token": "PM_MCP_TOKEN",
        }.get(principal_id, "")
        return "authorized" if env_name and os.environ.get(env_name) else "authorization_revoked"
    if authorization.get("principal_kind") == "direct_session":
        runner_session_id = principal_id.removeprefix("direct-session/")
        with _conn(project) as c:
            row = c.execute(
                "SELECT revoked_at, expires_at, project_id FROM direct_session_tokens "
                "WHERE runner_session_id=? ORDER BY created_at DESC LIMIT 1",
                (runner_session_id,),
            ).fetchone()
        if (row and not row["revoked_at"] and float(row["expires_at"] or 0) > time.time()
                and row["project_id"] == project):
            return "authorized"
        return "authorization_revoked"
    principal = access_repo.get_principal_by_id(principal_id, project=project)
    if not principal:
        return "principal_not_found"
    try:
        auth.authorize_principal(principal, project, ("read",))
    except PermissionError:
        return "authorization_revoked"
    return "authorized"


def authorization_snapshot(principal: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only authorization fields safe to persist with a job.

    Never copies prompts, cookies, or credentials. In dev-open mode an empty
    principal is the local operator (DOGFOOD-21 contract).
    """
    if not principal and auth.auth_mode() == auth.DEV_OPEN:
        return {"principal_id": "dev/operator", "dev_open": True}
    return {
        "principal_id": str(principal.get("id") or ""),
        "principal_kind": str(principal.get("kind") or ""),
        "dev_open": bool(principal.get("dev_open")),
        "environment_operator": bool(principal.get("environment_operator")),
    }


def admit(*, project: str = DEFAULT_PROJECT, surface: str,
          authorization: Mapping[str, Any], question: str = "",
          provider_secret: str = "") -> AdmissionDecision:
    """Make one atomic decision without persisting sensitive request material.

    ``question`` is measured for the prompt-size gate and then discarded;
    ``provider_secret`` is accepted only so callers cannot accidentally route it
    anywhere else, and is deleted immediately (DOGFOOD-21 residue contract).
    """
    del provider_secret
    # Identity-less requests (authorized by the surface middleware, no attached
    # principal) share one explicit anonymous bucket so the per-principal limits
    # still apply to them collectively instead of denying at the wrong layer.
    principal_id = str(authorization.get("principal_id") or "") or "principal/anonymous"
    admission_id = f"aiadmission-{uuid.uuid4().hex}"
    now = time.time()
    auth_reason = _authorization_reason(project, authorization)
    config = limits()

    def persist() -> tuple[str, str]:
        with _conn(project) as c:
            active = c.execute(
                "SELECT COUNT(*) FROM ai_admission_events "
                "WHERE principal_id=? AND status=?",
                (principal_id, ACTIVE),
            ).fetchone()[0]
            queued = c.execute(
                "SELECT COUNT(*) FROM ai_admission_events "
                "WHERE principal_id=? AND status=?",
                (principal_id, QUEUED),
            ).fetchone()[0]
            hour = c.execute(
                "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? "
                "AND created_at>=? AND reason_code='admitted'",
                (principal_id, now - 3600),
            ).fetchone()[0]
            day = c.execute(
                "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? "
                "AND created_at>=? AND reason_code='admitted'",
                (principal_id, now - 86400),
            ).fetchone()[0]
            if auth_reason != "authorized":
                status, reason = "denied", auth_reason
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
            c.execute(
                "INSERT INTO ai_admission_events(admission_id, principal_id, project, surface, "
                "status, reason_code, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (admission_id, principal_id, project, surface, status, reason, now, now),
            )
            return status, reason

    status, reason = _write_through(project, persist)
    decision = AdmissionDecision(status in {ACTIVE, QUEUED}, admission_id, status, reason)
    if not decision.allowed:
        raise AdmissionDenied(decision)
    return decision


def bind_run(project: str, admission_id: str, run_id: str) -> None:
    with _conn(project) as c:
        c.execute("UPDATE ai_admission_events SET run_id=?, updated_at=? WHERE admission_id=?",
                  (run_id, time.time(), admission_id))


def authorize_execution(*, project: str, admission_id: str,
                        authorization: Mapping[str, Any]) -> AdmissionDecision:
    """Re-check auth and atomically promote a queued admission when capacity opens."""
    now = time.time()
    reason = _authorization_reason(project, authorization)

    def recheck() -> AdmissionDecision:
        with _conn(project) as c:
            row = c.execute(
            "SELECT principal_id, status, reason_code FROM ai_admission_events "
            "WHERE admission_id=?", (admission_id,),
            ).fetchone()
            if not row:
                return AdmissionDecision(False, admission_id, "denied", "admission_not_found")
            status = row["status"]
            if reason != "authorized":
                status = "denied"
                c.execute(
                    "UPDATE ai_admission_events SET status='denied', reason_code=?, updated_at=? "
                    "WHERE admission_id=?", (reason, now, admission_id),
                )
                return AdmissionDecision(False, admission_id, status, reason)
            if status == QUEUED:
                active = c.execute(
                    "SELECT COUNT(*) FROM ai_admission_events WHERE principal_id=? AND status=?",
                    (row["principal_id"], ACTIVE),
                ).fetchone()[0]
                if active >= limits()["active"]:
                    return AdmissionDecision(False, admission_id, QUEUED, "waiting_for_active_slot")
                status = ACTIVE
                c.execute(
                    "UPDATE ai_admission_events SET status=?, updated_at=? WHERE admission_id=?",
                    (ACTIVE, now, admission_id),
                )
            if status != ACTIVE:
                return AdmissionDecision(False, admission_id, status, row["reason_code"])
        return AdmissionDecision(True, admission_id, ACTIVE, "authorized")

    return _write_through(project, recheck)


def finish(project: str, admission_id: str, status: str) -> None:
    terminal = status if status in TERMINAL else "failed"
    with _conn(project) as c:
        c.execute("UPDATE ai_admission_events SET status=?, updated_at=? WHERE admission_id=?",
                  (terminal, time.time(), admission_id))


__all__ = ["AdmissionDecision", "AdmissionDenied", "admit", "authorization_snapshot",
           "authorize_execution", "bind_run", "finish", "kill_switch_enabled", "limits"]
