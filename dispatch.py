"""Dispatch a plan task to Claude Code cloud through the wake substrate.

This retires the old Maxwell/ActionEngine push-bridge (a self-hosted runner at a private
demo IP that never carried `project`, so any non-Maxwell task 404'd and nothing spun up).

The UI "Dispatch to Claude Code" button and the `dispatch_to_claude_code` MCP tool both call
`dispatch()` here. It enqueues a project-aware, lane-scoped **wake intent** that only a
trigger-only host advertising the ``vendor_cloud`` capability can claim. That host invokes the
official ``claude --cloud`` CLI bridge; coding runs in Anthropic's hosted VM, not on the host.

Servicing a wake needs an authenticated Claude cloud trigger host online for the task's
project+lane (see deploy/switchboard-claude-cloud-host.service.example). The production plan box
remains coordination-only. Wakes queue until the trigger host is available, and provider auth or
receipt failures stay visibly failed rather than falling back to self-hosted compute.
"""
import store

_RUNTIME = "claude-code"
_CLOUD_CAPABILITY = "vendor_cloud"


def _host_is_work_capable(host):
    """True if a registered host advertises work capability (defensive across shapes)."""
    if not isinstance(host, dict):
        return False
    if host.get("stale"):
        return False
    # allow_work is advertised per-runtime under runtimes[].policy — that's the shape
    # register_host actually persists (it keeps runtimes_json but drops the top-level
    # inventory.policy). This is the real signal; the top-level checks below are a
    # defensive fallback for other shapes.
    for rt in host.get("runtimes") or []:
        if isinstance(rt, dict) and (rt.get("policy") or {}).get("allow_work"):
            return True
    for src in (host, host.get("policy") or {}, host.get("inventory") or {}):
        if isinstance(src, dict) and src.get("allow_work"):
            return True
    return False


def _host_is_cloud_capable(host):
    if not _host_is_work_capable(host):
        return False
    for runtime in host.get("runtimes") or []:
        if (isinstance(runtime, dict)
                and runtime.get("runtime") == _RUNTIME
                and _CLOUD_CAPABILITY in set(runtime.get("capabilities") or [])):
            return True
    return False


def _work_hosts(project, lane=""):
    try:
        hosts = store.list_agent_hosts(runtime=_RUNTIME, lane=lane, project=project)
    except Exception:
        return []
    return [h for h in hosts if _host_is_cloud_capable(h)]


def status(project=store.DEFAULT_PROJECT):
    """Is dispatch wired? Always yes now (the wake substrate is built-in). Also report whether a
    work-capable host is online for this project, so the UI can warn before a wake sits queued."""
    hosts = _work_hosts(project)
    return {"configured": True, "mode": "wake", "project": project,
            "work_hosts_online": len(hosts)}


def dispatch(task_id, actor="user", project=store.DEFAULT_PROJECT):
    """Enqueue a lane-scoped claim_next wake for `task_id` on `project`."""
    t = store.get_task(task_id, project=project)
    if not t:
        return {"dispatched": False, "error": "task not found",
                "task_id": task_id, "project": project}
    lane = t.get("_wsId") or ""
    branch = f"claude/{task_id.lower()}-cloud"
    selector = {
        "runtime": _RUNTIME,
        "lane": lane,
        "agent_id": f"claude/{task_id}",
        "capabilities": [_CLOUD_CAPABILITY],
        "branch": branch,
    }
    reason = f"Operator dispatched {task_id} — {t.get('title') or ''}".strip()
    w = store.request_wake(
        selector=selector, reason=reason, source=f"ui:{actor}",
        policy={"mode": "vendor_cloud", "provider": "anthropic",
                "continuity": "fresh_only"}, task_id=task_id, actor=actor,
        project=project, idem_key=f"ui-dispatch:{project}:{task_id}")
    if w.get("error") or not w.get("wake_id"):
        return {"dispatched": False, "task_id": task_id, "project": project,
                "error": w.get("error") or w.get("reason") or "wake not created"}
    hosts = _work_hosts(project, lane)
    note = (f"Queued an Anthropic-hosted Claude Code cloud session (wake {w['wake_id']}, "
            f"lane {lane or '—'}). A trigger-only host will launch the pushed `{branch}` branch, "
            "bind the app-visible session URL, and Claude will open a PR — it never merges or "
            "pushes to main/master.")
    if not hosts:
        note += (" No authenticated Claude cloud trigger host is online for this lane yet, so "
                 "it stays queued (deploy/switchboard-claude-cloud-host.service.example).")
    store.add_comment(task_id, "Switchboard (dispatch)", note, project=project)
    return {"dispatched": True, "task_id": task_id, "project": project,
            "wake_id": w["wake_id"], "wake_status": w.get("status"),
            "lane": lane, "branch": branch, "execution_mode": "vendor_cloud",
            "work_hosts_online": len(hosts)}


def latest(task_id, project=store.DEFAULT_PROJECT):
    """The current dispatch state for a task, for the Dev-tab panel:
    status in {none, queued, claiming, running, pr}, plus wake/session/PR detail."""
    try:
        wakes = [w for w in store.list_wake_intents(project=project)
                 if w.get("task_id") == task_id]
    except Exception:
        wakes = []
    # wake rows timestamp with `requested_at` (there is no `created_at`), and
    # list_wake_intents returns them oldest-first — so pick the newest explicitly.
    wake = max(wakes, key=lambda w: w.get("requested_at") or 0, default=None)
    try:
        sessions = store.list_runner_sessions(task_id=task_id, project=project)
    except Exception:
        sessions = []
    session = sessions[0] if sessions else None
    t = store.get_task(task_id, project=project) or {}
    git = t.get("git_state") or {}
    pr_url = git.get("pr_url")
    sel = (wake or {}).get("selector") or {}
    session_metadata = (session or {}).get("metadata") or {}
    wake_result = (wake or {}).get("result") or {}
    nested_result = session_metadata.get("wake_result") or {}
    session_url = (session_metadata.get("session_url") or nested_result.get("session_url")
                   or wake_result.get("session_url"))

    if pr_url:
        status_v = "pr"
    elif session and not session.get("stale"):
        status_v = "running"
    elif wake and wake.get("status") == "claimed":
        status_v = "claiming"
    elif wake and wake.get("status") in ("pending", "requested", "", None):
        status_v = "queued"
    elif wake:
        status_v = wake.get("status") or "queued"
    else:
        status_v = "none"

    return {
        "status": status_v,
        "wake_id": (wake or {}).get("wake_id"),
        "wake_status": (wake or {}).get("status"),
        "agent_id": (session or {}).get("agent_id") or sel.get("agent_id"),
        "session_id": (session or {}).get("runner_session_id"),
        "session_url": session_url,
        "provider_session_id": (session_metadata.get("provider_session_id")
                                or nested_result.get("provider_session_id")
                                or wake_result.get("provider_session_id")),
        "vendor_id": (session_metadata.get("vendor_id") or nested_result.get("vendor_id")
                      or wake_result.get("vendor_id")),
        "pr_url": pr_url,
        "lane": sel.get("lane") or t.get("_wsId"),
        "execution_mode": (wake or {}).get("policy", {}).get("mode"),
    }
