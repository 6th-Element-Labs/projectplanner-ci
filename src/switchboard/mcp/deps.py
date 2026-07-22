"""Shared MCP edge helpers: JSON serialization and Switchboard bearer-principal
write/read gating (ARCH-MS-70).

Extracted from ``mcp_server_impl`` so tool modules under ``switchboard.mcp.tools``
can depend on these directly instead of importing the composition root. The
composition root still owns FastMCP wiring / observability instrumentation; it
registers a write observer here via ``register_write_observer`` so the
HARDEN-63 write-latency histogram keeps working without this module owning
that singleton.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import auth
import store
from switchboard.mcp import authorization
from switchboard.security import redact_provider_secrets

_mark_write: Optional[Callable[[], None]] = None


def register_write_observer(observer: Optional[Callable[[], None]]) -> None:
    """Let the composition root wire in a write-latency observer (HARDEN-63)."""
    global _mark_write
    _mark_write = observer


def _dumps(obj) -> str:
    """json.dumps with sort_keys=True — deterministic serialization for prompt-cache hits.
    Stable key order means identical responses share a cache hit across agent sessions."""
    return json.dumps(redact_provider_secrets(obj), sort_keys=True)


def _require_write(ctx, project: str = "maxwell", scopes=("write:tasks",)):
    """Gate writes through the shared Switchboard bearer-principal path."""
    try:
        try:
            principal = authorization.require_current_access(project, tuple(scopes))
        except authorization.ProjectContextUnavailable:
            principal = auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                          scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))
    # HARDEN-63: this call took the write path — feed the write-latency histogram.
    if _mark_write is not None:
        _mark_write()
    return principal


def _require_read(ctx, project: str = "maxwell", scopes=("read",)):
    """Gate sensitive reads through the selected project's bearer scopes."""
    try:
        try:
            return authorization.require_current_access(project, tuple(scopes))
        except authorization.ProjectContextUnavailable:
            return auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                     scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))


def _resolve_write_actor(principal, project: str = "maxwell", task_id: str = "",
                         agent_id: str = "", system_actor: str = "",
                         system_reason: str = ""):
    binding = store.resolve_write_actor(
        auth.actor(principal),
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        system_actor=system_actor,
        system_reason=system_reason,
        principal_id=principal.get("id") or "",
    )
    if not binding.get("ok"):
        return binding
    return binding


def _write_binding_comment(task_id: str, binding, project: str = "maxwell") -> None:
    if not task_id or not isinstance(binding, dict):
        return
    if binding.get("binding") in ("principal", None):
        return
    store.append_activity(
        "principal.write_bound",
        "switchboard/identity",
        store.write_binding_activity_payload(binding),
        task_id=task_id,
        project=project,
    )


# Public aliases (module-level helpers above are named with a leading
# underscore to match the exact identifiers moved out of mcp_server_impl).
dumps = _dumps
require_write = _require_write
require_read = _require_read
resolve_write_actor = _resolve_write_actor
write_binding_comment = _write_binding_comment
