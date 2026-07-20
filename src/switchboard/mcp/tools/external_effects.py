"""External effects / CI / publication / merge_gate MCP tools.

Transport adapter extracted in ARCH-MS-67. Authentication and MCP serialization
remain edge concerns; shared commands own effect claim/lifecycle and merge_gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import external_ci_mirror
import store
from switchboard.application.commands import claim_external_effect as effect_command
from switchboard.application.commands import merge_gate as merge_gate_command
from switchboard.application.commands import verify_ci as verify_ci_command


@dataclass(frozen=True)
class ExternalEffectsToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: ExternalEffectsToolServices | None = None


def _services() -> ExternalEffectsToolServices:
    if _SERVICES is None:
        raise RuntimeError("external effects MCP tools must be registered before use")
    return _SERVICES


def claim_external_effect(effect_type: str, target: str, resource: str,
                          payload_json: str, ctx: Context,
                          project: str = "maxwell", task_id: str = "",
                          claim_id: str = "", agent_id: str = "",
                          idem_key: str = "",
                          idempotency_window_seconds: int = 0) -> str:
    """Atomically claim an external side effect before touching a provider.

    Replays return the existing effect. If the existing effect is not verified, callers
    must read back provider state before issuing it again.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(payload_json or "{}")
    except Exception:
        return services.dumps({"error": "payload_json must be a JSON object string"})
    return services.dumps(effect_command.claim_mapping_result(
        {
            "effect_type": effect_type,
            "target": target,
            "resource": resource,
            "payload": payload,
            "task_id": task_id or None,
            "claim_id": claim_id,
            "agent_id": agent_id,
            "idem_key": idem_key,
            "idempotency_window_seconds": idempotency_window_seconds,
            "project": project,
        },
        actor=auth.actor(principal), principal_id=principal["id"]))


def mark_external_effect_issued(effect_key: str, ctx: Context,
                                readback_json: str = "{}",
                                project: str = "maxwell") -> str:
    """Mark an already-claimed external side effect as issued to the provider."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return services.dumps({"error": "readback_json must be a JSON object string"})
    return services.dumps(effect_command.mark_issued_mapping_result(
        {"effect_key": effect_key, "readback": readback, "project": project},
        actor=auth.actor(principal)))


def verify_external_effect(effect_key: str, ctx: Context,
                           readback_json: str = "{}",
                           project: str = "maxwell") -> str:
    """Confirm an external side effect only after provider readback or explicit proof."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return services.dumps({"error": "readback_json must be a JSON object string"})
    return services.dumps(effect_command.verify_mapping_result(
        {"effect_key": effect_key, "readback": readback, "project": project},
        actor=auth.actor(principal)))


def fail_external_effect(effect_key: str, error: str, ctx: Context,
                         readback_json: str = "{}", dead_letter: bool = False,
                         project: str = "maxwell") -> str:
    """Record a failed or dead-lettered external side effect with visible error state."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return services.dumps({"error": "readback_json must be a JSON object string"})
    return services.dumps(effect_command.fail_mapping_result(
        {"effect_key": effect_key, "error": error, "readback": readback,
         "dead_letter": dead_letter, "project": project},
        actor=auth.actor(principal)))


def list_external_effects(project: str = "maxwell", effect_type: str = "",
                          status: str = "", task_id: str = "",
                          target: str = "") -> str:
    """List external side effects by type/status/task/target."""
    services = _services()
    return services.dumps(effect_command.list_mapping_result(
        effect_type=effect_type, status=status, task_id=task_id,
        target=target, project=project))


def list_external_ci_runs(project: str = "maxwell", task_id: str = "",
                          source_project: str = "", source_sha: str = "",
                          status: str = "") -> str:
    """List public CI mirror runs tracked by Switchboard."""
    services = _services()
    return services.dumps(store.list_external_ci_runs(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, status=status, project=project))


def get_external_ci_run(run_id: str, project: str = "maxwell") -> str:
    """Read one public CI mirror run by Switchboard run id."""
    services = _services()
    run = store.get_external_ci_run(run_id, project=project)
    return services.dumps(run) if run else services.dumps(
        {"error": "external_ci_run not found", "run_id": run_id})


def list_publication_evidence(project: str = "maxwell", task_id: str = "",
                              source_project: str = "", source_sha: str = "",
                              public_repo: str = "") -> str:
    """List public mirror publication evidence tracked by Switchboard."""
    services = _services()
    return services.dumps(store.list_publication_evidence(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, public_repo=public_repo, project=project))


def merge_gate(task_id: str, ctx: Context, project: str = "maxwell",
               agent_id: str = "", claim_id: str = "", work_session_id: str = "",
               pr_url: str = "", pr_number: int = 0, repo: str = "",
               target_branch: str = "", branch: str = "", head_sha: str = "",
               required_status_contexts: str = "", status_contexts_json: str = "{}",
               github_pr_json: str = "{}", evidence_json: str = "{}",
               require_work_session: bool = False) -> str:
    """Evaluate safe-merge readiness before an agent runs or requests PR merge.

    This records a merge.gate activity event and returns pass/blocked findings. It does
    not merge and cannot mark Done; GitHub webhook/reconcile provenance remains the Done
    authority."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        status_contexts = json.loads(status_contexts_json or "{}")
    except Exception:
        return services.dumps(
            {"error": "status_contexts_json must be a JSON object or array string"})
    try:
        github_pr = json.loads(github_pr_json or "{}")
    except Exception:
        return services.dumps({"error": "github_pr_json must be a JSON object string"})
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return services.dumps({"error": "evidence_json must be a JSON object string"})
    if not isinstance(evidence, dict):
        return services.dumps({"error": "evidence_json must be a JSON object string"})
    return services.dumps(merge_gate_command.execute_mapping_result(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "claim_id": claim_id,
            "work_session_id": work_session_id,
            "pr_url": pr_url,
            "pr_number": pr_number or None,
            "repo": repo,
            "target_branch": target_branch,
            "branch": branch,
            "head_sha": head_sha,
            "required_status_contexts": required_status_contexts,
            "status_contexts": status_contexts,
            "github_pr": github_pr,
            "evidence": evidence,
            "require_work_session": bool(require_work_session),
            "project": project,
        },
        actor=auth.actor(principal), principal_id=principal["id"]))


def record_publication_evidence(ctx: Context, source_sha: str, public_ref: str,
                                project: str = "maxwell", source_project: str = "",
                                source_repo: str = "", public_repo: str = "",
                                public_sha: str = "", public_tag: str = "",
                                script: str = "", guard_status: str = "unknown",
                                guard_json: str = "{}", artifact_url: str = "",
                                task_id: str = "", claim_id: str = "",
                                agent_id: str = "", publication_id: str = "",
                                published_at: float = 0.0) -> str:
    """Record public mirror publication evidence for a canonical source SHA."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        guard = json.loads(guard_json or "{}")
    except Exception:
        return services.dumps({"error": "guard_json must be a JSON object string"})
    if not isinstance(guard, dict):
        return services.dumps({"error": "guard_json must be a JSON object string"})
    payload = {
        "publication_id": publication_id,
        "source_project": source_project or project,
        "source_repo": source_repo,
        "source_sha": source_sha,
        "public_repo": public_repo,
        "public_ref": public_ref,
        "public_sha": public_sha,
        "public_tag": public_tag,
        "script": script,
        "guard_status": guard_status,
        "guard": guard,
        "artifact_url": artifact_url,
        "task_id": task_id,
        "claim_id": claim_id,
        "agent_id": agent_id,
        "principal_id": principal["id"],
    }
    if published_at:
        payload["published_at"] = published_at
    return services.dumps(store.create_publication_evidence(
        payload, actor=auth.actor(principal), project=project))


def request_external_ci_mirror_run(source_path: str, mirror_repo: str, workflow: str,
                                   ctx: Context, source_project: str = "",
                                   source_repo: str = "", source_branch: str = "",
                                   source_sha: str = "", mirror_branch: str = "",
                                   mirror_remote_url: str = "", task_id: str = "",
                                   claim_id: str = "", agent_id: str = "",
                                   status_context: str = "",
                                   workflow_inputs_json: str = "{}",
                                   poll_interval_seconds: float = 15.0,
                                   timeout_seconds: float = 1800.0,
                                   idem_key: str = "",
                                   project: str = "maxwell") -> str:
    """Mirror an exact private source SHA to a disposable public CI branch and poll Actions.

    Agents should request this from their private/source-of-truth checkout. They must not
    edit or develop in the public CI repo. Switchboard records sync/trigger/poll/test
    failures with distinct failure_class values on the external_ci_run.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        workflow_inputs = json.loads(workflow_inputs_json or "{}")
    except Exception:
        return services.dumps({"error": "workflow_inputs_json must be a JSON object string"})
    payload = {
        "source_project": source_project or project,
        "source_repo": source_repo,
        "source_branch": source_branch,
        "source_sha": source_sha,
        "mirror_repo": mirror_repo,
        "mirror_branch": mirror_branch,
        "mirror_remote_url": mirror_remote_url,
        "workflow": workflow,
        "status_context": status_context,
        "workflow_inputs": workflow_inputs,
        "task_id": task_id,
        "claim_id": claim_id,
        "agent_id": agent_id,
        "principal_id": principal["id"],
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": timeout_seconds,
        "idem_key": idem_key,
        "request": {
            "workflow_inputs": workflow_inputs,
            "poll_interval_seconds": poll_interval_seconds,
            "timeout_seconds": timeout_seconds,
        },
    }
    return services.dumps(external_ci_mirror.request_external_ci_mirror_run(
        payload, source_path, actor=auth.actor(principal), project=project))


def poll_external_ci_mirror_run(run_id: str, source_path: str, ctx: Context,
                                poll_interval_seconds: float = 15.0,
                                timeout_seconds: float = 1800.0,
                                project: str = "maxwell") -> str:
    """Resume polling one external CI mirror run after trigger or agent/session interruption."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(external_ci_mirror.poll_external_ci_mirror_run(
        run_id, source_path, actor=auth.actor(principal), project=project,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds))


def verify_ci(sha: str, ctx: Context, project: str = "maxwell",
              ensure: bool = False, source_path: str = "", task_id: str = "",
              pr_number: int = 0, repo: str = "",
              source_fetch_ref: str = "") -> str:
    """SIMPLIFY-8: verify(sha) -> {pending|green|red, url, contexts, stall?}.

    This is the only CI surface callers should use. Pass ensure=true to request a
    re-verify for exactly one SHA; mirror/branch plumbing stays inside the adapter.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(verify_ci_command.execute_mapping_result(
        {
            "sha": sha,
            "project": project,
            "ensure": bool(ensure),
            "source_path": source_path,
            "task_id": task_id,
            "pr_number": pr_number or 0,
            "repo": repo,
            "source_fetch_ref": source_fetch_ref,
        },
        actor=auth.actor(principal),
    ))


EXTERNAL_EFFECTS_TOOL_NAMES = (
    "claim_external_effect",
    "mark_external_effect_issued",
    "verify_external_effect",
    "fail_external_effect",
    "list_external_effects",
    "list_external_ci_runs",
    "get_external_ci_run",
    "list_publication_evidence",
    "merge_gate",
    "record_publication_evidence",
    "request_external_ci_mirror_run",
    "poll_external_ci_mirror_run",
    "verify_ci",
)


def register_external_effects_tools(
        mcp: Any, services: ExternalEffectsToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in EXTERNAL_EFFECTS_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
