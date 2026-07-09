"""Dispatch a plan task to the fleet via the wake substrate — the coherent control plane.

This retires the old Maxwell/ActionEngine push-bridge (a self-hosted runner at a private
demo IP that never carried `project`, so any non-Maxwell task 404'd and nothing spun up).

The UI "Dispatch to Claude Code" button and the `dispatch_to_claude_code` MCP tool both call
`dispatch()` here. It enqueues a project-aware, lane-scoped **wake intent** (store.request_wake)
that a work-capable Agent Host (adapters/agent_host.py with PM_AGENT_HOST_ALLOW_WORK=1) claims
and runs in an isolated worktree, opening a PR on a `claude/<task>` branch — never main.

Servicing a wake needs a work-capable host online for the task's project+lane
(see deploy/switchboard-agent-host-work.service.example). The prod plan box is intentionally
message-only (PM_AGENT_HOST_ALLOW_WORK=0), so wakes queue until such a host is running — the
status/response surfaces that so the UI can say "queued, waiting for a work host" instead of
failing silently.
"""
import store

_RUNTIME = "claude-code"


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


def _work_hosts(project, lane=""):
    try:
        hosts = store.list_agent_hosts(runtime=_RUNTIME, lane=lane, project=project)
    except Exception:
        return []
    return [h for h in hosts if _host_is_work_capable(h)]


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
    selector = {"runtime": _RUNTIME, "lane": lane, "agent_id": f"claude/{task_id}"}
    reason = f"Operator dispatched {task_id} — {t.get('title') or ''}".strip()
    w = store.request_wake(
        selector=selector, reason=reason, source=f"ui:{actor}",
        policy={"mode": "claim_next"}, task_id=task_id, actor=actor,
        project=project, idem_key=f"ui-dispatch:{project}:{task_id}")
    if w.get("error") or not w.get("wake_id"):
        return {"dispatched": False, "task_id": task_id, "project": project,
                "error": w.get("error") or w.get("reason") or "wake not created"}
    hosts = _work_hosts(project, lane)
    note = (f"Queued a work session via the fleet (wake {w['wake_id']}, lane {lane or '—'}). "
            f"A work-capable agent host will claim {task_id} and open a PR on a "
            f"`claude/{task_id.lower()}` branch — it never merges to main.")
    if not hosts:
        note += (" No work-capable host is online for this lane yet, so it stays queued until "
                 "one is running (deploy/switchboard-agent-host-work.service.example).")
    store.add_comment(task_id, "Switchboard (dispatch)", note, project=project)
    return {"dispatched": True, "task_id": task_id, "project": project,
            "wake_id": w["wake_id"], "wake_status": w.get("status"),
            "lane": lane, "work_hosts_online": len(hosts)}


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
        "pr_url": pr_url,
        "lane": sel.get("lane") or t.get("_wsId"),
    }
