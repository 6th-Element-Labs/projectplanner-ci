"""File-lease MCP tools.

Transport adapter extracted in ARCH-MS-52. Authentication and MCP serialization
remain edge concerns; persistence stays behind store / application commands.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import store


@dataclass(frozen=True)
class LeaseToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: LeaseToolServices | None = None


def _services() -> LeaseToolServices:
    if _SERVICES is None:
        raise RuntimeError("lease MCP tools must be registered before use")
    return _SERVICES


def claim_files(agent_id: str, files: str, ctx: Context, project: str = "maxwell",
                task_id: str = "", ttl_minutes: int = 30) -> str:
    """Claim file paths before editing them (advisory soft lock — prevents parallel agents
    from clobbering each other). Call before writing; call release_files when done.

    agent_id: a stable string identifying this agent session (e.g. 'claude/ENGINE-11').
    files: comma or newline-separated list of paths (relative to repo root).
    task_id: the board task you're working on (optional but recommended).
    ttl_minutes: auto-expire after this many minutes if release_files is never called (default 30).

    On success: {lease_id, files, expires_at, ttl_minutes}
    On conflict: {conflict, task_id, files, retry_after_seconds} — use Bash(sleep N) before retrying."""
    services = _services()
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return services.dumps({"error": "no files given"})
    services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.claim_files(agent_id, file_list,
                                    task_id=task_id or None,
                                    ttl_minutes=max(1, ttl_minutes),
                                    project=project))



def release_files(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a file lease when you are done editing. Pass the lease_id returned by
    claim_files. Idempotent — releasing an already-released lease returns an error but does
    not corrupt state. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.release_files(lease_id, project=project))



def check_files(files: str, project: str = "maxwell") -> str:
    """Check whether any of the given file paths are held by an active lease.
    files: comma or newline-separated list of paths.
    Returns a list of {file, held_by, task_id, expires_at} for files that ARE held.
    Empty list means all files are free — safe to edit without claiming first (though
    calling claim_files is strongly preferred to avoid races).
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return services.dumps([])
    return services.dumps(store.check_files(file_list, project=project))



def list_active_leases(project: str = "maxwell") -> str:
    """All active file leases on the board — who holds what, and when it expires.
    Use to see which agents are currently active and which files they have claimed.
    Expired and released leases are not shown.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    return services.dumps(store.list_active_leases(project=project))


# ---- IXP-core runtime lifecycle -----------------------------------------




LEASE_TOOL_NAMES = ('claim_files', 'release_files', 'check_files', 'list_active_leases')


def register_lease_tools(mcp: Any, services: LeaseToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in LEASE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
