"""Commands for browser PTY relay ticket minting and descriptors (ADAPTER-22)."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import DEFAULT_PROJECT
from switchboard.application import runner_pty_relay as relay
from switchboard.domain import runner_pty as domain
from switchboard.storage.repositories import runner as runner_repo


def _session_binding(session: Mapping[str, Any], project: str) -> dict[str, str]:
    bind = runner_repo.runner_bind_tuple(session)
    meta = session.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    return domain.merge_binding({
        "tenant_id": str(
            session.get("tenant_id")
            or meta.get("tenant_id")
            or ""
        ),
        "user_id": str(session.get("user_id") or meta.get("user_id") or ""),
        "project_id": str(session.get("project_id") or project or DEFAULT_PROJECT),
        "task_id": bind.get("task_id") or "",
        "claim_id": bind.get("claim_id") or "",
        "work_session_id": bind.get("work_session_id") or "",
        "runner_session_id": str(session.get("runner_session_id") or ""),
        "host_id": bind.get("host_id") or "",
        "wake_id": bind.get("wake_id") or "",
        "execution_connection_id": str(
            session.get("execution_connection_id")
            or meta.get("execution_connection_id")
            or ""
        ),
        "source_sha": str(session.get("source_sha") or meta.get("source_sha") or ""),
        "permission_profile": str(
            session.get("permission_profile")
            or meta.get("permission_profile")
            or "operator_watch"
        ),
    })


def mint_ticket_for_session(
    *,
    runner_session_id: str,
    project: str,
    scopes: Any,
    ttl_seconds: int = domain.DEFAULT_TICKET_TTL_SECONDS,
    binding_overlay: Mapping[str, Any] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Load runner session, merge COORD-34 bind fields, mint a capability ticket."""
    project_id = project or DEFAULT_PROJECT
    sid = str(runner_session_id or "").strip()
    if not sid:
        return {"error": "runner_session_id_required", "error_code": "invalid_input"}
    session = runner_repo.get_runner_session(sid, project=project_id)
    if not session:
        return {"error": "runner_session_not_found", "error_code": "not_found"}
    missing = runner_repo.missing_runner_bind_fields(session)
    if missing:
        return runner_repo.runner_bind_incomplete(missing, task_id=session.get("task_id") or "")
    binding = _session_binding(session, project_id)
    if binding_overlay:
        binding = domain.merge_binding(binding, binding_overlay)
    # Ticket mint requires the full ADAPTER-22 bind tuple. Fill safe defaults for
    # optional identity fields when the session row does not yet carry them.
    binding.setdefault("tenant_id", binding.get("tenant_id") or "tenant/default")
    binding.setdefault("user_id", binding.get("user_id") or (actor or "operator"))
    binding.setdefault(
        "execution_connection_id",
        binding.get("execution_connection_id") or "execconn/unspecified",
    )
    binding.setdefault("source_sha", binding.get("source_sha") or "unknown")
    binding.setdefault(
        "permission_profile",
        binding.get("permission_profile") or "operator_watch",
    )
    still_missing = domain.missing_ticket_bind_fields(binding)
    if still_missing:
        return {
            "error": "incomplete_bind",
            "error_code": "runner_bind_incomplete",
            "missing": still_missing,
        }
    try:
        ticket, payload = relay.mint_capability_ticket(
            binding, scopes, ttl_seconds=ttl_seconds)
    except ValueError as exc:
        return {"error": str(exc), "error_code": "invalid_ticket_request"}
    public_base = relay.public_base_from_env()
    descriptor: dict[str, Any] = {
        "minted": True,
        "runner_session_id": sid,
        "scopes": payload.get("scopes") or [],
        "jti": payload.get("jti"),
        "expires_at": payload.get("exp"),
        "ticket": ticket,
        "transport": domain.TRANSPORT_SWITCHBOARD_PTY_RELAY,
        "browser_safe": bool(public_base) and not relay.is_loopback_url(public_base),
        "binding": {k: binding[k] for k in domain.TICKET_BIND_FIELDS},
    }
    if descriptor["browser_safe"]:
        descriptor["relay_url"] = relay.public_relay_url(public_base, sid, ticket)
        descriptor["relay_path"] = domain.RELAY_PATH_TEMPLATE.format(
            runner_session_id=sid)
    return descriptor


def open_relay_descriptor(
    *,
    runner_session_id: str,
    project: str,
    ticket: str = "",
) -> dict[str, Any]:
    """Return public relay info without ever exposing a loopback URL."""
    project_id = project or DEFAULT_PROJECT
    sid = str(runner_session_id or "").strip()
    if not sid:
        return {"error": "runner_session_id_required", "error_code": "invalid_input"}
    session = runner_repo.get_runner_session(sid, project=project_id)
    if not session:
        return {"error": "runner_session_not_found", "error_code": "not_found"}
    public_base = relay.public_base_from_env()
    path = domain.RELAY_PATH_TEMPLATE.format(runner_session_id=sid)
    result: dict[str, Any] = {
        "runner_session_id": sid,
        "relay_path": path,
        "transport": domain.TRANSPORT_SWITCHBOARD_PTY_RELAY,
        "browser_safe": True,
        "relay_required": True,
    }
    if public_base and not relay.is_loopback_url(public_base) and ticket:
        result["relay_url"] = relay.public_relay_url(public_base, sid, ticket)
    # Never include stream_url from session metadata if it is loopback.
    meta = session.get("metadata") or {}
    if isinstance(meta, dict):
        sanitized = relay.sanitize_browser_stream_metadata(
            meta, relay_url=str(result.get("relay_url") or ""))
        if sanitized.get("relay_url"):
            result["relay_url"] = sanitized["relay_url"]
    return result


def revoke_ticket(
    *,
    jti: str = "",
    ticket: str = "",
    project: str = "",
    hub: relay.RelayHub | None = None,
) -> dict[str, Any]:
    project_id = str(project or DEFAULT_PROJECT)
    if ticket:
        ok, reason = relay.revoke_capability_ticket(
            ticket, project=project_id, hub=hub)
        result: dict[str, Any] = {"revoked": bool(ok)}
        if reason:
            result["reason"] = reason
        return result
    if jti:
        ok = relay.revoke_ticket_jti(jti, project=project_id, hub=hub)
        return {"revoked": bool(ok), "jti": jti}
    return {"revoked": False, "error": "jti_or_ticket_required"}
