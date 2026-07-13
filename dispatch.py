"""Dispatch a plan task to Claude Code cloud or Codex cloud through the wake substrate.

The UI dispatch controls and MCP tools call `dispatch()` here. Claude uses the official
``claude --cloud`` bridge (``vendor_cloud`` capability). Codex uses the ADAPTER-19
``cloud_execution`` envelope consumed by ``adapters/codex/cloud_adapter.py``.
"""
import os
import time

import store

_RUNTIME = "claude-code"
_CODEX_RUNTIME = "codex"
_CODEX_VENDOR = "openai-codex-cloud"
_CLOUD_CAPABILITY = "vendor_cloud"


def _host_is_work_capable(host):
    """True if a registered host advertises work capability (defensive across shapes)."""
    if not isinstance(host, dict):
        return False
    if host.get("stale"):
        return False
    for rt in host.get("runtimes") or []:
        if isinstance(rt, dict) and (rt.get("policy") or {}).get("allow_work"):
            return True
    for src in (host, host.get("policy") or {}, host.get("inventory") or {}):
        if isinstance(src, dict) and src.get("allow_work"):
            return True
    return False


def _host_is_cloud_capable(host, runtime=_RUNTIME, capability=_CLOUD_CAPABILITY):
    if not _host_is_work_capable(host):
        return False
    for rt in host.get("runtimes") or []:
        if (isinstance(rt, dict)
                and rt.get("runtime") == runtime
                and capability in set(rt.get("capabilities") or [])):
            return True
    return False


def _work_hosts(project, lane="", runtime=_RUNTIME, capability=_CLOUD_CAPABILITY):
    try:
        hosts = store.list_agent_hosts(runtime=runtime, lane=lane, project=project)
    except Exception:
        return []
    return [h for h in hosts if _host_is_cloud_capable(h, runtime=runtime, capability=capability)]


def status(project=store.DEFAULT_PROJECT):
    hosts = _work_hosts(project)
    return {"configured": True, "mode": "wake", "project": project,
            "work_hosts_online": len(hosts)}


def _normalize_runtime(runtime):
    value = str(runtime or _RUNTIME).strip().lower()
    if value in {_RUNTIME, "claude", "claude-code-local"}:
        return _RUNTIME
    if value in {_CODEX_RUNTIME, "codex-cloud", _CODEX_VENDOR}:
        return _CODEX_RUNTIME
    return ""


def _codex_cloud_policy(task_id, task, branch):
    endpoint = os.environ.get("PM_MCP_PUBLIC_URL", "https://plan.taikunai.com/mcp")
    return {
        "mode": "cloud_execution",
        "kind": "cloud_execution",
        "vendor_id": _CODEX_VENDOR,
        "cloud_execution": {
            "vendor_id": _CODEX_VENDOR,
            "branch": branch,
            "canonical_repo": "6th-Element-Labs/projectplanner",
            "dev_brief": (
                f"Read {task_id} from Switchboard and implement it end-to-end. "
                f"Task title: {task.get('title') or task_id}. "
                "Run the repository gate, push the required task branch, open a PR, and write "
                "branch/head/test/PR evidence back to the claim."
            ),
            "mcp_access": {
                "endpoint": endpoint,
                "token_ref": f"switchboard://scoped-token/{task_id}",
                "scopes": ["read:task", "write:claim", "write:evidence"],
                "expires_at": time.time() + 3600,
            },
        },
    }


def dispatch(task_id, actor="user", project=store.DEFAULT_PROJECT, runtime=_RUNTIME):
    """Enqueue a lane-scoped wake for `task_id` on `project`."""
    t = store.get_task(task_id, project=project)
    if not t:
        return {"dispatched": False, "error": "task not found",
                "task_id": task_id, "project": project}
    selected_runtime = _normalize_runtime(runtime)
    if not selected_runtime:
        return {"dispatched": False, "error": "unsupported runtime",
                "task_id": task_id, "project": project, "runtime": runtime}
    lane = t.get("_wsId") or ""
    if selected_runtime == _CODEX_RUNTIME:
        branch = f"codex/{task_id.lower()}"
        selector = {
            "runtime": _CODEX_RUNTIME,
            "lane": lane,
            "agent_id": f"codex/{task_id}",
            "capabilities": ["cloud_execution"],
            "branch": branch,
        }
        policy = _codex_cloud_policy(task_id, t, branch)
        hosts = _work_hosts(project, lane, _CODEX_RUNTIME, "cloud_execution")
        note = (
            f"Queued a Codex cloud dispatch (wake pending, lane {lane or '—'}). "
            f"An eligible bridge host will submit an OpenAI-hosted task on `{branch}`, bind its "
            "ChatGPT/Codex task URL as the runner session, and record unknown/zero subscription "
            "cost until provider usage is reconciled."
        )
    else:
        branch = f"claude/{task_id.lower()}-cloud"
        selector = {
            "runtime": _RUNTIME,
            "lane": lane,
            "agent_id": f"claude/{task_id}",
            "capabilities": [_CLOUD_CAPABILITY],
            "branch": branch,
        }
        policy = {"mode": "vendor_cloud", "provider": "anthropic", "continuity": "fresh_only"}
        hosts = _work_hosts(project, lane)
        note = (f"Queued an Anthropic-hosted Claude Code cloud session (lane {lane or '—'}). "
                f"A trigger-only host will launch the pushed `{branch}` branch, bind the "
                "app-visible session URL, and Claude will open a PR.")
    reason = f"Operator dispatched {task_id} — {t.get('title') or ''}".strip()
    w = store.request_wake(
        selector=selector, reason=reason, source=f"ui:{actor}",
        policy=policy, task_id=task_id, actor=actor,
        project=project, idem_key=f"ui-dispatch:{project}:{task_id}:{selected_runtime}")
    if w.get("error") or not w.get("wake_id"):
        return {"dispatched": False, "task_id": task_id, "project": project,
                "error": w.get("error") or w.get("reason") or "wake not created"}
    if not hosts:
        if selected_runtime == _CODEX_RUNTIME:
            note += " No Codex cloud bridge host is online for this lane yet, so it stays queued."
        else:
            note += (" No authenticated Claude cloud trigger host is online for this lane yet, so "
                     "it stays queued (deploy/switchboard-claude-cloud-host.service.example).")
    store.add_comment(task_id, "Switchboard (dispatch)", note, project=project)
    return {"dispatched": True, "task_id": task_id, "project": project,
            "wake_id": w["wake_id"], "wake_status": w.get("status"),
            "lane": lane, "runtime": selected_runtime,
            "vendor_id": _CODEX_VENDOR if selected_runtime == _CODEX_RUNTIME else None,
            "branch": branch, "execution_mode": policy.get("mode"),
            "work_hosts_online": len(hosts)}


def latest(task_id, project=store.DEFAULT_PROJECT):
    """The current dispatch state for a task, for the Dev-tab panel."""
    try:
        wakes = [w for w in store.list_wake_intents(project=project)
                 if w.get("task_id") == task_id]
    except Exception:
        wakes = []
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
        "runtime": (session or {}).get("runtime") or sel.get("runtime"),
        "provider_session_id": (session_metadata.get("provider_session_id")
                                or nested_result.get("provider_session_id")
                                or wake_result.get("provider_session_id")),
        "vendor_id": (session_metadata.get("vendor_id") or nested_result.get("vendor_id")
                      or wake_result.get("vendor_id")),
        "pr_url": pr_url,
        "lane": sel.get("lane") or t.get("_wsId"),
        "execution_mode": (wake or {}).get("policy", {}).get("mode"),
    }
