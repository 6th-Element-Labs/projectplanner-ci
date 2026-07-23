"""Read-only dispatch capacity projection.

Task starts are owned by ``switchboard.application.commands.task_execution``.
This module remains only for the board and closure capacity readout; it cannot
create, resume, select, or control an execution.
"""

import store


def _host_is_work_capable(host):
    if not isinstance(host, dict) or host.get("stale"):
        return False
    for runtime in host.get("runtimes") or []:
        if isinstance(runtime, dict) and (runtime.get("policy") or {}).get("allow_work"):
            return True
    for source in (host, host.get("policy") or {}, host.get("inventory") or {}):
        if isinstance(source, dict) and source.get("allow_work"):
            return True
    return False


def _online_work_host_count(project):
    try:
        hosts = store.list_agent_hosts(project=project)
    except Exception:
        return 0
    return sum(1 for host in hosts if _host_is_work_capable(host))


def status(project=store.DEFAULT_PROJECT):
    """Return read-only host capacity without selecting an execution target."""
    return {
        "configured": True,
        "mode": "task_execution",
        "project": project,
        "work_hosts_online": _online_work_host_count(project),
    }
