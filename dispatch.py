"""Dispatch a plan task through the wake substrate.

The UI dispatch controls and MCP tools call `dispatch()` here. Claude uses the official
``claude --cloud`` bridge (``vendor_cloud`` capability). The browser's Codex action targets
the authenticated operator's enrolled native Agent Host. The explicit ``codex-cloud`` runtime
retains the ADAPTER-19 envelope consumed by ``adapters/codex/cloud_adapter.py``.
"""
import hashlib
import json
import os
import re
import time

import store

_RUNTIME = "claude-code"
_CODEX_RUNTIME = "codex"
_CODEX_VENDOR = "openai-codex-cloud"
_CLOUD_CAPABILITY = "vendor_cloud"
_CO_FLEET_CAPABILITY = "co_fleet"
_BINDING_FIELD = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,240}$")
_BINDING_REQUIRED = (
    "tenant_id", "user_id", "provider", "provider_account_id",
    "credential_reference",
)


def _co_account_binding(task_id, project, account_binding):
    """Validate and normalize the non-secret BYOA account-affinity contract."""
    if not account_binding:
        return None
    if not isinstance(account_binding, dict):
        raise ValueError("account_binding must be an object")
    allowed = set(_BINDING_REQUIRED) | {"auth_lane"}
    unknown = set(account_binding) - allowed - {"project", "task_id"}
    if unknown:
        raise ValueError(f"unsupported account binding fields: {sorted(unknown)}")
    if account_binding.get("project") not in (None, "", project):
        raise ValueError("account binding project does not match dispatch project")
    if account_binding.get("task_id") not in (None, "", task_id):
        raise ValueError("account binding task does not match dispatch task")
    normalized = {}
    for key in allowed:
        value = str(account_binding.get(key) or "").strip()
        if key in _BINDING_REQUIRED and not value:
            raise ValueError(f"account binding missing {key}")
        if value and not _BINDING_FIELD.fullmatch(value):
            raise ValueError(f"unsafe account binding field {key}")
        if value:
            normalized[key] = value
    reference = normalized["credential_reference"]
    if not reference.startswith((
        "provider-cred-", "credential:", "vault:", "ssm:/", "secretsmanager:arn:",
    )):
        raise ValueError("credential_reference must be an opaque credential/vault/secret reference")
    normalized.update({
        "schema": "switchboard.co_account_binding.v1",
        "project": project,
        "task_id": task_id,
        # Execution identifiers do not exist at dispatch time. The selected host
        # binds them in order: wake reservation -> task claim/Work Session ->
        # credential lease -> credential-ready wake. They must never be guessed by
        # the dispatcher or supplied by an operator.
        "claim_id": None,
        "work_session_id": None,
        "host_id": None,
        "runner_session_id": None,
        "credential_lease_id": None,
        "credential_admission_phase": "preclaim",
    })
    affinity_source = {key: normalized.get(key) for key in (
        "tenant_id", "user_id", "project", "provider", "provider_account_id",
        "credential_reference", "auth_lane",
    )}
    normalized["account_affinity_id"] = hashlib.sha256(
        json.dumps(affinity_source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return normalized


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


def _personal_host_target(project, principal_id, lane=""):
    """Return the operator-owned enrolled Codex host and whether it is live now.

    Enrollment ownership is durable, so an offline host can still be the target of a queued
    wake. Live inventory is only used for the online/headroom signal shown by the UI.
    """
    owner = str(principal_id or "").strip()
    if not owner:
        return None, False
    try:
        enrollments = store.list_agent_host_enrollments(status="active", project=project)
        hosts = store.list_agent_hosts(
            runtime=_CODEX_RUNTIME, lane=lane, include_stale=True, project=project)
    except Exception:
        return None, False
    hosts_by_id = {str(host.get("host_id") or ""): host for host in hosts}
    candidates = []
    for enrollment in enrollments:
        if str(enrollment.get("owner_user_id") or "") != owner:
            continue
        if project not in set(enrollment.get("project_allowlist") or []):
            continue
        if "openai-codex" not in set(enrollment.get("provider_allowlist") or []):
            continue
        host_id = str(enrollment.get("host_id") or "").strip()
        if not host_id:
            continue
        host = hosts_by_id.get(host_id) or {}
        local_auth = dict((host.get("capacity") or {}).get("local_auth") or {})
        live = bool(
            host
            and not host.get("stale")
            and _host_is_work_capable(host)
            and local_auth.get("available") is True
            and local_auth.get("auth_mode") == "chatgpt_personal"
            and (host.get("available_sessions") is None
                 or int(host.get("available_sessions") or 0) > 0)
        )
        candidates.append((live, host_id))
    if not candidates:
        return None, False
    candidates.sort(key=lambda item: (not item[0], item[1]))
    live, host_id = candidates[0]
    return host_id, live


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


def _codex_cloud_requested(runtime):
    return str(runtime or "").strip().lower() in {"codex-cloud", _CODEX_VENDOR}


def _personal_dispatch_attempt(project, task_id, selector, base_idem_key):
    """Return the safe idempotency key (or live wake) for a browser retry.

    One fixed key correctly collapses double-clicks, but it also replays a failed
    wake forever. Reuse the latest matching wake while it is active or completed;
    after an explicitly terminal failure/cancellation/expiry, derive one new key
    from that wake. A second click then sees the new active wake and collapses onto
    it instead of creating parallel duplicate sessions.
    """
    try:
        wakes = store.list_wake_intents(
            task_id=task_id, runtime=_CODEX_RUNTIME, project=project)
    except Exception:
        return base_idem_key, None, None
    matching = [wake for wake in wakes if (
        str((wake.get("selector") or {}).get("host_id") or "")
        == str(selector.get("host_id") or "")
        and str((wake.get("selector") or {}).get("agent_id") or "")
        == str(selector.get("agent_id") or "")
    )]
    if not matching:
        return base_idem_key, None, None
    latest = matching[-1]
    status = str(latest.get("status") or "").lower()
    if status not in {"failed", "cancelled", "expired"}:
        return base_idem_key, latest, None
    prior_wake_id = str(latest.get("wake_id") or "")
    return f"{base_idem_key}:after:{prior_wake_id}", None, prior_wake_id


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


def dispatch(task_id, actor="user", project=store.DEFAULT_PROJECT, runtime=_RUNTIME,
             principal_id=""):
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
    if selected_runtime == _CODEX_RUNTIME and not _codex_cloud_requested(runtime):
        branch = f"codex/{task_id.lower()}"
        host_id, host_online = _personal_host_target(project, principal_id, lane)
        if not host_id:
            return {
                "dispatched": False,
                "error": "personal_agent_host_not_enrolled",
                "reason": "No active Codex Agent Host enrollment belongs to this user.",
                "task_id": task_id,
                "project": project,
                "runtime": selected_runtime,
            }
        selector = {
            "runtime": _CODEX_RUNTIME,
            "lane": lane,
            "agent_id": f"codex/{task_id}",
            "task_id": task_id,
            "host_id": host_id,
            "branch": branch,
        }
        links = store.list_task_deliverable_links(task_id, project=project)
        linked_deliverable = links[0] if links else {}
        deliverable = t.get("deliverable") or {}
        deliverable_id = str(
            t.get("deliverable_id")
            or (deliverable.get("id") if isinstance(deliverable, dict) else deliverable)
            or linked_deliverable.get("deliverable_id")
            or ""
        ).strip()
        prompt_scope = f" for deliverable {deliverable_id}" if deliverable_id else ""
        endpoint = str(
            os.environ.get("PM_MCP_PUBLIC_URL")
            or os.environ.get("PM_BASE", "https://plan.taikunai.com").rstrip("/") + "/mcp"
        ).strip()
        if not endpoint.endswith("/mcp"):
            endpoint = endpoint.rstrip("/") + "/mcp"
        policy = {
            "mode": "direct_task",
            "execution_mode": "direct_personal_cli",
            "continuity": "switchboard_mcp_boot",
            "require_runner_bind": False,
            "allow_on_demand": False,
            "assignment": {
                "schema": "switchboard.direct_cli_assignment.v1",
                "project": project,
                "task_id": task_id,
                "deliverable_id": deliverable_id,
                "host_id": host_id,
                "prompt": (
                    f"Do {task_id}{prompt_scope} in project {project} via Switchboard."
                ),
                "repository": {
                    "slug": "6th-Element-Labs/projectplanner",
                    "default_branch": "master",
                    "branch": branch,
                    "canonical_sha": "",
                },
                "mcp": {
                    "endpoint": endpoint,
                    "auth_source": "enrolled_agent_host_token",
                },
            },
            "placement": {
                "canonical_repo": "6th-Element-Labs/projectplanner",
                "session_policy": "code_strict",
                "isolation": "task_worktree",
            },
        }
        hosts = [host_id] if host_online else []
        note = (
            f"Assigned `{task_id}` directly to `{host_id}`. The enrolled Mac will open an "
            f"isolated `{branch}-direct-<session>` workspace, start its native Codex CLI with the Switchboard "
            "MCP connection preloaded, and publish the same PTY to Watch."
        )
    elif selected_runtime == _CODEX_RUNTIME:
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
    personal_dispatch = (
        selected_runtime == _CODEX_RUNTIME and not _codex_cloud_requested(runtime))
    idem_key = (
        f"ui-personal-dispatch:v3:{project}:{task_id}:{principal_id}:{selector.get('host_id')}"
        if personal_dispatch
        else f"ui-dispatch:{project}:{task_id}:{str(runtime or selected_runtime).lower()}"
    )
    existing_wake = None
    if personal_dispatch:
        idem_key, existing_wake, retry_after = _personal_dispatch_attempt(
            project, task_id, selector, idem_key)
        if retry_after:
            # The side-effect ledger hashes the payload independently from the
            # API idempotency key. Name the terminal predecessor in the policy so
            # a deliberate retry is a new auditable effect, not a replay of void.
            policy = {**policy, "dispatch_attempt_after": retry_after}
    w = existing_wake or store.request_wake(
        selector=selector, reason=reason, source=f"ui:{actor}",
        policy=policy, task_id=task_id, actor=actor,
        principal_id=principal_id, project=project, idem_key=idem_key)
    if w.get("error") or not w.get("wake_id"):
        return {"dispatched": False, "task_id": task_id, "project": project,
                "error": w.get("error") or w.get("reason") or "wake not created"}
    if not hosts:
        if selected_runtime == _CODEX_RUNTIME and not _codex_cloud_requested(runtime):
            note += f" `{selector.get('host_id')}` is offline or full, so the exact wake stays queued."
        elif selected_runtime == _CODEX_RUNTIME:
            note += " No Codex cloud bridge host is online for this lane yet, so it stays queued."
        else:
            note += (" No authenticated Claude cloud trigger host is online for this lane yet, so "
                     "it stays queued (deploy/switchboard-claude-cloud-host.service.example).")
    if existing_wake is None:
        store.add_comment(task_id, "Switchboard (dispatch)", note, project=project)
    return {"dispatched": True, "task_id": task_id, "project": project,
            "wake_id": w["wake_id"], "wake_status": w.get("status"),
            "assignment_id": w["wake_id"],
            "lane": lane, "runtime": selected_runtime,
            "vendor_id": (_CODEX_VENDOR if selected_runtime == _CODEX_RUNTIME
                          and _codex_cloud_requested(runtime) else None),
            "host_id": selector.get("host_id"),
            "branch": branch, "execution_mode": policy.get("mode"),
            "work_hosts_online": len(hosts)}


def dispatch_to_co_fleet(task_id, actor="user", project=store.DEFAULT_PROJECT,
                         runtime=_RUNTIME, capabilities=None,
                         runtime_config_ref="", allow_on_demand=False,
                         account_binding=None, placement=None):
    """Queue a task for the elastic CO worker fleet.

    ``runtime_config_ref`` is an SSM parameter or Secrets Manager *reference*.
    Credential values are deliberately not accepted by this API and never enter a
    wake payload or EC2 user data.
    """
    task = store.get_task(task_id, project=project)
    if not task:
        return {"dispatched": False, "error": "task not found",
                "task_id": task_id, "project": project}
    selected_runtime = _normalize_runtime(runtime)
    if not selected_runtime:
        return {"dispatched": False, "error": "unsupported runtime",
                "task_id": task_id, "project": project, "runtime": runtime}
    config_ref = (runtime_config_ref or os.environ.get("PM_CO_RUNTIME_CONFIG_REF") or "").strip()
    if not (config_ref.startswith("ssm:/") or config_ref.startswith("secretsmanager:arn:")):
        return {"dispatched": False, "error": "runtime_config_ref required",
                "reason": "use ssm:/path or secretsmanager:arn:...; raw credentials are forbidden",
                "task_id": task_id, "project": project}
    try:
        binding = _co_account_binding(task_id, project, account_binding)
    except ValueError as exc:
        return {"dispatched": False, "error": "invalid_account_binding",
                "reason": str(exc), "task_id": task_id, "project": project}
    lane = task.get("_wsId") or ""
    required = [_CO_FLEET_CAPABILITY]
    for capability in capabilities or []:
        value = str(capability or "").strip()
        if value and value not in required:
            required.append(value)
    selector = {
        "runtime": selected_runtime,
        "lane": lane,
        "agent_id": f"{selected_runtime}/{task_id}",
        "capabilities": required,
        "task_id": task_id,
    }
    policy = {
        "mode": "co_fleet",
        "continuity": "fresh_switchboard_state",
        "runtime_config_ref": config_ref,
        "allow_on_demand": bool(allow_on_demand),
        "registration_timeout_s": 180,
        "account_binding_required": binding is not None,
        "scheduler": {
            "mode": "hybrid",
            "prefer_persistent": True,
            "allow_persistent": True,
            "allow_ephemeral": True,
            "burst_enabled": True,
            "max_host_loss_reschedules": 3,
        },
        "placement": {
            "canonical_repo": "6th-Element-Labs/projectplanner",
            "session_policy": str(task.get("policy_profile") or "code_strict"),
            "isolation": "task_worktree",
            **dict(placement or {}),
        },
    }
    if binding is not None:
        policy["account_binding"] = binding
    wake = store.request_wake(
        selector=selector,
        reason=f"CO Fleet dispatch {task_id} — {task.get('title') or ''}".strip(),
        source=f"co-fleet:{actor}", policy=policy, task_id=task_id,
        actor=actor, project=project,
        idem_key=f"co-fleet-dispatch:{project}:{task_id}:{selected_runtime}",
    )
    if wake.get("error") or not wake.get("wake_id"):
        return {"dispatched": False, "task_id": task_id, "project": project,
                "error": wake.get("error") or wake.get("reason") or "wake not created"}
    if wake.get("status") == "failed":
        return {
            "dispatched": False, "task_id": task_id, "project": project,
            "wake_id": wake.get("wake_id"), "wake_status": "failed",
            "error": "hybrid_placement_denied",
            "reason": (wake.get("placement") or {}).get("reason_code")
            or (wake.get("result") or {}).get("reason"),
        }
    store.add_comment(
        task_id, "Switchboard (CO Fleet)",
        f"Queued elastic {selected_runtime} worker wake {wake['wake_id']} for lane "
        f"{lane or '—'} with capabilities {', '.join(required)}. Runtime credentials "
        "remain behind opaque references. Any BYOA account binding is preserved "
        "on the durable wake and omitted from host metadata and activity text.",
        project=project,
    )
    return {"dispatched": True, "task_id": task_id, "project": project,
            "wake_id": wake["wake_id"], "wake_status": wake.get("status"),
            "lane": lane, "runtime": selected_runtime, "capabilities": required,
            "execution_mode": "co_fleet", "allow_on_demand": bool(allow_on_demand),
            "account_affinity_id": (binding or {}).get("account_affinity_id")}


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
