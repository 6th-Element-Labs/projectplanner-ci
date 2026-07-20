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
from switchboard.domain.coordination.runtime_profile import runtime_profile_requirement

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


def _env_truthy(name):
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _expected_runtime_profile(runtime, *, session_policy="code_strict"):
    return runtime_profile_requirement(
        runtime,
        session_policy=session_policy,
        require_runner_watch=_env_truthy("PM_COORD_REQUIRE_RUNNER_WATCH"),
        agent_host_version=os.environ.get("PM_EXPECTED_AGENT_HOST_VERSION", ""),
        expected_profile_hash=os.environ.get("PM_EXPECTED_AGENT_HOST_PROFILE_HASH", ""),
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


def _personal_dispatch_attempt(project, task_id, selector, base_idem_key,
                               continuation_of=""):
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
    matching = []
    for wake in wakes:
        wake_assignment = (wake.get("policy") or {}).get("assignment") or {}
        wake_continuation = str(
            (wake_assignment.get("continuation") or {}).get(
                "previous_runner_session_id") or "")
        if (
            str((wake.get("selector") or {}).get("host_id") or "")
            == str(selector.get("host_id") or "")
            and str((wake.get("selector") or {}).get("agent_id") or "")
            == str(selector.get("agent_id") or "")
            and wake_continuation == str(continuation_of or "")
        ):
            matching.append(wake)
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
             principal_id="", continuation=None, *, role="implementation",
             source_sha="", instruction="", findings=None):
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
        task_agent_state = t.get("agent_state") if isinstance(t.get("agent_state"), dict) else {}
        recovery_handoff = task_agent_state.get("switchboard/recovery_handoff")
        recovery_handoff = (dict(recovery_handoff)
                            if isinstance(recovery_handoff, dict) else {})
        recovery_attempt = int(recovery_handoff.get("attempt") or 0)
        role = str(role or "implementation").strip().lower()
        continuation_head = str(
            ((continuation or {}).get("handoff") or {}).get("head_sha") or ""
        ).lower()
        current_head = str(
            (t.get("git_state") or {}).get("head_sha") or continuation_head
        ).lower()
        if continuation and role == "implementation":
            role = "review_merge"
            source_sha = source_sha or current_head
        if role not in {"implementation", "review_merge", "remediation"}:
            return {"dispatched": False, "error": "unsupported_lifecycle_role",
                    "task_id": task_id, "project": project, "role": role}
        source_sha = str(source_sha or "").strip().lower()
        if source_sha and not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            return {"dispatched": False, "error": "invalid_source_sha",
                    "task_id": task_id, "project": project, "role": role}
        if role in {"review_merge", "remediation"} and not source_sha:
            return {"dispatched": False, "error": "source_sha_required",
                    "task_id": task_id, "project": project, "role": role}
        if source_sha and source_sha != current_head:
            return {"dispatched": False, "error": "source_sha_mismatch",
                    "task_id": task_id, "project": project, "role": role,
                    "expected_head_sha": current_head or None,
                    "source_sha": source_sha}
        recovery_branch = str(recovery_handoff.get("branch") or "").strip()
        branch = (recovery_branch if recovery_branch.startswith("codex/")
                  else f"codex/{task_id.lower()}")
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
        continuation = dict(continuation or {})
        review_continuation = bool(continuation)
        if review_continuation:
            prior_runner = str(
                continuation.get("previous_runner_session_id") or "").strip()
            if not prior_runner:
                return {"dispatched": False, "error": "continuation_runner_required",
                        "task_id": task_id, "project": project}
            provider_session = str(
                continuation.get("provider_session_id") or "").strip()
            continuation = {
                "schema": "switchboard.review_runner_continuation.v1",
                "mode": "resume_conversation" if provider_session else "replacement_handoff",
                "previous_runner_session_id": prior_runner,
                "provider_session_id": provider_session or None,
                "handoff": dict(continuation.get("handoff") or {}),
            }
        if not instruction:
            if role == "review_merge":
                instruction = f"Review {task_id} via Switchboard and merge if green."
            elif role == "remediation":
                instruction = f"Remediate review findings for {task_id} via Switchboard."
            else:
                instruction = (
                    f"Do {task_id} in {deliverable_id} via Switchboard"
                    if deliverable_id else f"Do {task_id} via Switchboard"
                )
        if recovery_handoff:
            instruction += "\nRecovery handoff: " + json.dumps(
                recovery_handoff, sort_keys=True, separators=(",", ":"))
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
                "agent_id": selector["agent_id"],
                "role": role,
                "instruction": instruction,
                "prompt": (
                    f"Do {task_id}{prompt_scope} in project {project} via Switchboard."
                    if role == "implementation" else
                    f"Review and merge {task_id} if green{prompt_scope} in project "
                    f"{project} via Switchboard."
                    if review_continuation else instruction
                ),
                "source_sha": source_sha,
                "user_id": principal_id,
                "account_id": principal_id,
                "findings": list(findings or []),
                "recovery_handoff": recovery_handoff or None,
                "repository": {
                    "slug": "6th-Element-Labs/projectplanner",
                    "default_branch": "master",
                    "branch": branch,
                    "canonical_sha": "",
                    "source_sha": source_sha,
                },
                "mcp": {
                    "endpoint": endpoint,
                    "auth_source": "enrolled_agent_host_token",
                },
                **({"continuation": continuation} if review_continuation else {}),
            },
            "placement": {
                "canonical_repo": "6th-Element-Labs/projectplanner",
                "session_policy": "code_strict",
                "isolation": "task_worktree",
            },
            "lifecycle": {
                "role": role,
                "source_sha": source_sha or None,
                "findings": list(findings or []),
                "recovery_attempt": recovery_attempt or None,
            },
        }
        hosts = [host_id] if host_online else []
        note = (
            (f"Started a replacement review runner for `{task_id}` on `{host_id}` while "
             f"preserving `{continuation.get('previous_runner_session_id')}` as history. "
             if review_continuation else
             f"Assigned `{task_id}` directly to `{host_id}`. ")
            + "The enrolled Mac will open an "
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
    continuation_of = str(
        ((policy.get("assignment") or {}).get("continuation") or {}).get(
            "previous_runner_session_id") or "")
    idem_key = (
        (f"ui-resume-review:v1:{project}:{task_id}:{principal_id}:"
         f"{selector.get('host_id')}:{continuation_of}")
        if personal_dispatch and continuation_of else
        (f"ui-personal-lifecycle:v1:{project}:{task_id}:{principal_id}:"
         f"{selector.get('host_id')}:{role}:{source_sha or 'base'}:"
         f"recovery-{recovery_attempt or 0}")
        if personal_dispatch
        else f"ui-dispatch:{project}:{task_id}:{str(runtime or selected_runtime).lower()}"
    )
    existing_wake = None
    if personal_dispatch:
        idem_key, existing_wake, retry_after = _personal_dispatch_attempt(
            project, task_id, selector, idem_key, continuation_of=continuation_of)
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


def _review_handoff(task, stale_session):
    """Return the small, non-secret context needed by a replacement reviewer."""
    git_state = dict(task.get("git_state") or {})
    metadata = dict(stale_session.get("metadata") or {})
    snapshot = dict(stale_session.get("last_snapshot") or {})
    environment = dict(stale_session.get("environment") or {})
    return {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "workflow_status": task.get("status"),
        "branch": git_state.get("branch") or metadata.get("branch") or snapshot.get("branch"),
        "head_sha": git_state.get("head_sha") or metadata.get("head_sha") or snapshot.get("head_sha"),
        "pr_url": git_state.get("pr_url") or metadata.get("pr_url") or snapshot.get("pr_url"),
        "checks": git_state.get("checks") or metadata.get("checks") or snapshot.get("checks"),
        "previous_runner_status": stale_session.get("status"),
        "previous_failure_reason": environment.get("failure_reason"),
        "previous_log_tail": environment.get("log_tail"),
    }


def resume_review(task_id, actor="user", project=store.DEFAULT_PROJECT,
                  principal_id=""):
    """Replace one dead In Review runner without changing task workflow state."""
    task = store.get_task(task_id, project=project)
    if not task:
        return {"resumed": False, "error": "task not found", "task_id": task_id}
    if str(task.get("status") or "") != "In Review":
        return {"resumed": False, "error": "task_not_in_review", "task_id": task_id,
                "status": task.get("status")}
    watch = store.resolve_runner_watch(task_id, include_stale=True, project=project)
    if watch.get("watchable"):
        return {"resumed": False, "error": "review_runner_already_live",
                "task_id": task_id,
                "runner_session_id": watch.get("runner_session_id")}
    sessions = list(watch.get("sessions") or [])
    terminal_statuses = {
        "completed", "failed", "cancelled", "expired", "lost", "killed", "exited",
    }
    live_unwatchable = [session for session in sessions if (
        session.get("stale") is not True
        and str(session.get("status") or "").strip().lower()
        in {"starting", "ready", "running"}
    )]
    if live_unwatchable:
        return {"resumed": False, "error": "review_runner_bind_incomplete",
                "task_id": task_id,
                "runner_session_id": live_unwatchable[0].get("runner_session_id")}
    dead_sessions = [session for session in sessions if (
        session.get("stale") is True
        or str(session.get("status") or "").strip().lower() in terminal_statuses
    )]
    if not dead_sessions:
        return {"resumed": False, "error": "stale_review_runner_not_found",
                "task_id": task_id}
    stale_session = dead_sessions[0]
    metadata = dict(stale_session.get("metadata") or {})
    provider_session_id = str(
        metadata.get("codex_conversation_id")
        or metadata.get("provider_session_id")
        or metadata.get("conversation_id")
        or metadata.get("thread_id")
        or ""
    ).strip()
    previous_runner_id = str(stale_session.get("runner_session_id") or "").strip()
    result = dispatch(
        task_id, actor=actor, project=project, runtime="codex",
        principal_id=principal_id,
        continuation={
            "previous_runner_session_id": previous_runner_id,
            "provider_session_id": provider_session_id,
            "handoff": _review_handoff(task, stale_session),
        },
    )
    if not result.get("dispatched"):
        return {"resumed": False, **result}
    current = store.get_task(task_id, project=project)
    return {
        "resumed": True,
        **result,
        "continuation_mode": (
            "resume_conversation" if provider_session_id else "replacement_handoff"),
        "previous_runner_session_id": previous_runner_id,
        "workflow_status": (current or {}).get("status"),
    }


START_TASK_SCHEMA = "switchboard.task_session_start.v1"


def _aws_canary_qualified(project):
    """AWS overflow is unavailable until its explicit acceptance task is Done."""
    canary = store.get_task("DOGFOOD-20", project=project) or {}
    return str(canary.get("status") or "").strip().lower() == "done"


def start_task(task_id, actor="user", project=store.DEFAULT_PROJECT,
               principal_id="", role="implementation", source_sha="",
               instruction="", findings=None):
    """COORD-44 core: the ONE way any surface starts or resumes a task session.

    UI, MCP, and the coordinator all call this instead of assembling their own
    wakes — three divergent launch paths (UI watch-resolve, autopilot co_fleet,
    hand-run server scripts) were the root defect behind BUG-91's residue.

    Contract, in priority order:
      1. A live watchable runner exists            -> attach (never duplicate).
      2. A dispatch for this task is already
         pending/claimed                           -> starting (idempotent).
      3. Otherwise                                 -> start on the caller's
         enrolled watch-capable personal host (Mac-first while the AWS fleet
         is unqualified).
      4. Failure                                   -> one truthful reason plus
         the dispatcher's latest verdict; never a zombie panel.

    The server owns runner identity end to end; callers never pass a runner id.
    """
    task_id = str(task_id or "").strip().upper()
    if not task_id:
        return {"schema": START_TASK_SCHEMA, "action": "refused",
                "started": False, "attached": False, "error": "task_id required"}
    task = store.get_task(task_id, project=project)
    if not task:
        return {"schema": START_TASK_SCHEMA, "action": "refused",
                "started": False, "attached": False,
                "error": "task not found", "task_id": task_id, "project": project}

    # 1) The TaskSession projection is the sole execution-state authority.
    from switchboard.application.queries import task_session as task_session_query
    task_session = task_session_query.execute_for(task_id, project=project)
    session = (task_session or {}).get("active_runner")
    if session:
        return {
            "schema": START_TASK_SCHEMA, "action": "attach",
            "started": False, "attached": True, "watchable": True,
            "task_id": task_id, "project": project,
            "runner_session_id": session.get("runner_session_id"),
            "host_id": session.get("host_id"),
        }

    # 2) Idempotency: an in-flight dispatch means the click already worked.
    # A second click (or a second surface) must not race a duplicate session.
    attempt = (task_session or {}).get("active_attempt") or {}
    if ((task_session or {}).get("lifecycle_phase") == "starting"
            and str(attempt.get("status") or "") in {"pending", "claimed"}):
        return {
            "schema": START_TASK_SCHEMA, "action": "starting",
            "started": False, "attached": False, "watchable": False,
            "task_id": task_id, "project": project,
            "wake_id": attempt.get("wake_id"),
            "wake_status": attempt.get("status"),
            "host_id": attempt.get("host_id") or "",
            "message": "A session for this task is already starting.",
        }

    lifecycle_role = {
        "review": "review_merge",
        "reviewer": "review_merge",
        "review_merge": "review_merge",
        "remediation": "remediation",
        "implementation": "implementation",
    }.get(str(role or "implementation").strip().lower())
    if lifecycle_role is None:
        return {"schema": START_TASK_SCHEMA, "action": "refused",
                "started": False, "attached": False,
                "error": "unsupported_lifecycle_role", "task_id": task_id,
                "project": project, "role": role}

    # The command owns placement. Resolve the project owner for autonomous
    # lifecycle ticks, prefer that owner's live Mac, and only overflow to AWS
    # after DOGFOOD-20 has terminal Done provenance.
    if not principal_id:
        principal_id = str((store.project_access(project) or {}).get("owner_user_id") or "")
    lane = task.get("_wsId") or ""
    personal_host_id, personal_host_live = _personal_host_target(
        project, principal_id, lane)
    aws_qualified = _aws_canary_qualified(project)
    use_aws_overflow = bool(aws_qualified and not personal_host_live)

    if use_aws_overflow:
        runtime_config_ref = str(
            os.environ.get("PM_COORDINATOR_AUTOPILOT_RUNTIME_CONFIG_REF")
            or os.environ.get("PM_CO_RUNTIME_CONFIG_REF") or ""
        ).strip()
        result = dispatch_to_co_fleet(
            task_id, actor=actor, project=project, runtime=_CODEX_RUNTIME,
            runtime_config_ref=runtime_config_ref,
            allow_on_demand=_env_truthy("PM_COORDINATOR_AUTOPILOT_ALLOW_ON_DEMAND"),
            role=lifecycle_role, source_sha=source_sha,
            instruction=instruction, findings=findings,
        )
    else:
        result = dispatch(
            task_id, actor=actor, project=project,
            runtime=_CODEX_RUNTIME, principal_id=principal_id,
            role=lifecycle_role, source_sha=source_sha,
            instruction=instruction, findings=findings,
        )
    if result.get("dispatched"):
        return {
            "schema": START_TASK_SCHEMA, "action": "started",
            "started": True, "attached": False, "watchable": False,
            "task_id": task_id, "project": project,
            "wake_id": result.get("wake_id"),
            "host_id": result.get("host_id"),
            "branch": result.get("branch"),
            "execution_mode": result.get("execution_mode"),
            "work_hosts_online": result.get("work_hosts_online"),
            "placement": "aws_overflow" if use_aws_overflow else "mac_preferred",
            "role": lifecycle_role,
        }

    # 4) One truthful failure, with the dispatcher's own latest verdict so the
    # operator sees "capacity exhausted ..." / "not enrolled", never a stale gate.
    from switchboard.storage.repositories.runner import latest_dispatch_outcome
    return {
        "schema": START_TASK_SCHEMA, "action": "refused",
        "started": False, "attached": False, "watchable": False,
        "task_id": task_id, "project": project,
        "error": result.get("error") or "start_failed",
        "reason": result.get("reason") or "",
        "placement": "aws_overflow" if use_aws_overflow else "mac_preferred",
        "aws_canary_qualified": aws_qualified,
        "preferred_host_id": personal_host_id,
        "dispatch": latest_dispatch_outcome(task_id, project=project),
    }


def dispatch_to_co_fleet(task_id, actor="user", project=store.DEFAULT_PROJECT,
                         runtime=_RUNTIME, capabilities=None,
                         runtime_config_ref="", allow_on_demand=False,
                         account_binding=None, placement=None,
                         role="implementation", source_sha="", instruction="",
                         findings=None):
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
            # The coordinator owns the expected identity.  Callers may add
            # resource constraints, but cannot silently bypass the finishing
            # profile that prevents wrong-module / missing-gh placements.
            "runtime_profile": _expected_runtime_profile(
                selected_runtime,
                session_policy=str(task.get("policy_profile") or "code_strict"),
            ),
        },
        "lifecycle": {
            "role": str(role or "implementation").strip().lower(),
            "source_sha": str(source_sha or "").strip().lower() or None,
            "instruction": str(instruction or "").strip() or None,
            "findings": list(findings or []),
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
        refusal_details = (wake.get("placement") or {}).get("refusal_details") or []
        named_refusals = [
            reason for detail in refusal_details for reason in detail.get("reasons") or []
            if reason
        ]
        return {
            "dispatched": False, "task_id": task_id, "project": project,
            "wake_id": wake.get("wake_id"), "wake_status": "failed",
            "error": "hybrid_placement_denied",
            "reason": (named_refusals[0] if named_refusals else None)
            or (wake.get("placement") or {}).get("reason_code")
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
    """Compatibility view over the authoritative TaskSession projection."""
    from switchboard.application.queries import task_session as task_session_query
    projection = task_session_query.execute_for(task_id, project=project)
    return latest_from_task_session(projection or {})


def latest_from_task_session(projection):
    attempt = projection.get("active_attempt") or {}
    session = projection.get("active_runner") or {}
    pr = projection.get("pr_head") or {}
    outcome = projection.get("last_dispatch_outcome") or {}
    phase = projection.get("lifecycle_phase")
    status_v = {"running": "running", "starting": "claiming",
                "start_failed_retry": "failed", "review": "pr",
                "merged": "pr"}.get(phase, "none")
    if phase == "starting" and str(attempt.get("status") or "") in {
            "pending", "requested", ""}:
        status_v = "queued"
    metadata = session.get("metadata") or {}
    result = attempt.get("result") or {}
    return {
        "status": status_v,
        "wake_id": attempt.get("wake_id"), "wake_status": attempt.get("status"),
        "agent_id": session.get("agent_id") or attempt.get("agent_id"),
        "session_id": session.get("runner_session_id"),
        "session_url": metadata.get("session_url") or result.get("session_url"),
        "runtime": session.get("runtime") or attempt.get("runtime"),
        "provider_session_id": (metadata.get("provider_session_id")
                                or result.get("provider_session_id")),
        "vendor_id": metadata.get("vendor_id") or result.get("vendor_id"),
        "pr_url": pr.get("pr_url"),
        "lane": (projection.get("task") or {}).get("_wsId"),
        "execution_mode": attempt.get("execution_mode"),
        "failure_reason": outcome.get("reason"),
        "task_session": projection,
    }
