"""Authoritative, project-scoped execution readiness (UI-63).

This is the admission read model shared by REST, MCP, Settings, and Start.  It
composes existing authorities; it never copies credential material or invents
capacity when a required signal is absent.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from constants import DEFAULT_PROJECT
from switchboard.storage.repositories.access import has_project
from switchboard.storage.repositories.coordination import list_agent_hosts
from switchboard.storage.repositories.project_execution_policy import (
    get_project_execution_policy,
)
from switchboard.storage.repositories.projects import (
    get_project_repo_topology,
)
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    default_provider_credential_repository,
)
from switchboard.storage.repositories.scm_connections import (
    SCMConnectionError,
    default_scm_connection_repository,
)

SCHEMA = "switchboard.project_execution_readiness.v1"
REQUIRED_SCM_OPERATIONS = frozenset({"clone", "fetch", "push", "create_pr"})


def _blocker(code: str, category: str, message: str, repair: str,
             **details: Any) -> Dict[str, Any]:
    result = {
        "code": code,
        "category": category,
        "blocking": True,
        "message": message,
        "repair": repair,
    }
    if details:
        result["details"] = details
    return result


def _state(name: str, passed: bool, message: str, blockers: Iterable[Dict[str, Any]],
           **details: Any) -> Dict[str, Any]:
    blocker_list = list(blockers)
    result = {
        "name": name,
        "status": "ready" if passed else "blocked",
        "passed": passed,
        "message": message,
        "blockers": blocker_list,
    }
    result.update(details)
    return result


def _host_placement(host: Dict[str, Any]) -> Dict[str, Any]:
    capacity = host.get("capacity") if isinstance(host.get("capacity"), dict) else {}
    placement = capacity.get("placement")
    return dict(placement) if isinstance(placement, dict) else {}


def _runtime_available(host: Dict[str, Any], allowed: set[str]) -> bool:
    for runtime in host.get("runtimes") or []:
        name = str((runtime or {}).get("runtime") or "")
        local_auth = (runtime or {}).get("local_auth")
        if name in allowed and (
                not isinstance(local_auth, dict) or local_auth.get("available") is True):
            return True
    return False


def get_project_execution_readiness(
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    project = str(project or DEFAULT_PROJECT).strip()
    if not has_project(project):
        return {
            "schema": SCHEMA,
            "project": project,
            "status": "blocked",
            "passed": False,
            "reason_code": "unknown_project",
            "message": f"unknown project: {project}",
            "blockers": [_blocker(
                "unknown_project", "configuration",
                f"Project {project} is not registered.",
                "Create or select a registered project before starting work.")],
            "states": {},
        }

    topology = dict(get_project_repo_topology(project) or {})
    policy = dict(get_project_execution_policy(project) or {})
    policy_gate = dict(policy.get("readiness") or {})
    canonical = str(
        (((topology.get("roles") or {}).get("canonical") or {}).get("repo")) or "")
    allowed_runtimes = {
        str(item) for item in ((policy.get("runtimes") or {}).get("allowed") or [])
        if str(item)
    }

    configuration_blockers: List[Dict[str, Any]] = []
    if topology.get("valid") is not True:
        configuration_blockers.append(_blocker(
            "repo_topology_not_ready", "configuration",
            "Canonical repository topology is missing or invalid.",
            "Configure a canonical owner/repository and default branch in GitHub & repositories.",
            missing=list(topology.get("missing") or []),
            invalid=list(topology.get("invalid") or [])))
    if policy_gate.get("passed") is not True:
        configuration_blockers.append(_blocker(
            str(policy_gate.get("reason_code") or "project_execution_policy_not_ready"),
            "configuration",
            str(policy_gate.get("message") or "Project execution policy is not ready."),
            "Configure runtimes, placement, provider selectors, SCM, and activate the execution policy.",
            missing=list(policy_gate.get("missing") or []),
            invalid=list(policy_gate.get("invalid") or [])))
    configuration = _state(
        "configuration", not configuration_blockers,
        "Repository topology and execution policy are ready."
        if not configuration_blockers else "Project execution configuration needs repair.",
        configuration_blockers,
        topology_valid=topology.get("valid") is True,
        policy_revision=((policy.get("lifecycle") or {}).get("revision") or 0),
        canonical_repository=canonical)

    provider_blockers: List[Dict[str, Any]] = []
    selectors = list((policy.get("providers") or {}).get("selectors") or [])
    try:
        connections = default_provider_credential_repository.list_metadata(
            project=project, admin=True)
    except CredentialVaultError as exc:
        connections = []
        provider_blockers.append(_blocker(
            exc.code, "provider", exc.message,
            "Repair project access/tenant binding, then verify the provider connection."))
    by_reference = {
        str(item.get("credential_reference") or ""): item for item in connections
    }
    for selector in selectors:
        reference = str((selector or {}).get("connection_reference") or "")
        provider = str((selector or {}).get("provider") or "")
        connection = by_reference.get(reference)
        if not connection:
            provider_blockers.append(_blocker(
                "provider_connection_missing", "provider",
                f"Provider connection {reference or '(unset)'} is unavailable.",
                "Connect and verify the selected AI provider in Personal AI accounts.",
                provider=provider, connection_reference=reference))
        elif (connection.get("lifecycle_state") != "active"
              or connection.get("refresh_state") not in {"ready", "valid", "active"}):
            provider_blockers.append(_blocker(
                "provider_connection_not_ready", "provider",
                f"Provider connection {reference} is not ready.",
                "Verify or reconnect the provider connection before Start.",
                provider=provider, connection_reference=reference,
                lifecycle_state=connection.get("lifecycle_state"),
                refresh_state=connection.get("refresh_state")))
    provider = _state(
        "provider", bool(selectors) and not provider_blockers,
        "All selected provider connections are active."
        if selectors and not provider_blockers
        else "A selected provider connection needs repair.",
        provider_blockers or ([] if selectors else [_blocker(
            "provider_selector_missing", "provider",
            "No provider selector is configured.",
            "Select at least one verified provider connection in the execution policy.")]),
        selector_count=len(selectors))

    scm_blockers: List[Dict[str, Any]] = []
    scm_policy = dict(policy.get("scm") or {})
    scm_reference = str(scm_policy.get("connection_reference") or "")
    try:
        scm_connection = (
            default_scm_connection_repository.get(scm_reference)
            if scm_reference else None)
    except SCMConnectionError:
        scm_connection = None
    if not scm_connection:
        scm_blockers.append(_blocker(
            "scm_connection_missing", "scm",
            f"SCM connection {scm_reference or '(unset)'} is unavailable.",
            "Create an SCM installation connection and select it in the execution policy."))
    else:
        scopes = set(scm_connection.get("operation_scopes") or [])
        repo_allowlist = {
            str(item).lower() for item in scm_connection.get("repository_allowlist") or []}
        project_allowlist = set(scm_connection.get("project_allowlist") or [])
        missing_operations = sorted(REQUIRED_SCM_OPERATIONS - scopes)
        if (scm_connection.get("lifecycle_state") != "active"
                or project not in project_allowlist
                or canonical.lower() not in repo_allowlist
                or missing_operations):
            scm_blockers.append(_blocker(
                "scm_connection_not_authorized", "scm",
                "The selected SCM connection cannot complete the repository lifecycle.",
                "Authorize this project, canonical repository, and clone/fetch/push/create_pr operations.",
                connection_reference=scm_reference,
                lifecycle_state=scm_connection.get("lifecycle_state"),
                missing_operations=missing_operations))
    scm = _state(
        "scm", not scm_blockers,
        "SCM authorization covers the canonical repository lifecycle."
        if not scm_blockers else "SCM authorization needs repair.",
        scm_blockers, connection_reference=scm_reference)

    placement_policy = dict(policy.get("placement") or {})
    allowed_classes = set(placement_policy.get("host_classes") or [])
    allowed_zones = set(placement_policy.get("trust_zones") or [])
    hosts = list_agent_hosts(include_stale=False, project=project)
    eligible_persistent: List[str] = []
    eligible_ephemeral: List[str] = []
    for host in hosts:
        placement = _host_placement(host)
        host_class = str(placement.get("host_class") or "")
        zones = set(placement.get("trust_zones") or [])
        projects = set(placement.get("projects") or [])
        repositories = {str(item).lower() for item in placement.get("repositories") or []}
        eligible = (
            _runtime_available(host, allowed_runtimes)
            and (host.get("available_sessions") is None
                 or int(host.get("available_sessions") or 0) > 0)
            and placement.get("wakeable") is not False
            and str(placement.get("drain_state") or "accepting") == "accepting"
            and (not projects or project in projects)
            and (not canonical or not repositories or canonical.lower() in repositories)
            and (not allowed_zones or not zones or bool(allowed_zones & zones))
        )
        if eligible and host_class == "persistent":
            eligible_persistent.append(str(host.get("host_id") or ""))
        if eligible and host_class == "ephemeral":
            eligible_ephemeral.append(str(host.get("host_id") or ""))

    persistent_required = "persistent" in allowed_classes
    persistent_blockers = []
    if persistent_required and not eligible_persistent:
        persistent_blockers.append(_blocker(
            "persistent_capacity_unavailable", "persistent",
            "No eligible persistent Agent Host currently has capacity.",
            "Enroll or repair an online persistent host matching runtime, trust zone, repository, and isolation policy."))
    persistent = _state(
        "persistent", not persistent_required or bool(eligible_persistent),
        ("Persistent capacity is not selected by policy." if not persistent_required
         else "Eligible persistent capacity is online." if eligible_persistent
         else "Persistent capacity is unavailable."),
        persistent_blockers, required=persistent_required,
        eligible_host_ids=eligible_persistent)
    if not persistent_required:
        persistent["status"] = "not_required"

    burst = dict(placement_policy.get("burst") or {})
    ephemeral_required = "ephemeral" in allowed_classes
    burst_configured = (
        burst.get("enabled") is True
        and int(burst.get("max_concurrent_ephemeral") or 0) > 0)
    ephemeral_blockers = []
    if ephemeral_required and not burst_configured and not eligible_ephemeral:
        ephemeral_blockers.append(_blocker(
            "ephemeral_capacity_unavailable", "ephemeral",
            "Ephemeral execution is selected but no burst policy or eligible host is available.",
            "Enable bounded burst capacity or register an eligible ephemeral host."))
    ephemeral = _state(
        "ephemeral",
        not ephemeral_required or burst_configured or bool(eligible_ephemeral),
        ("Ephemeral capacity is not selected by policy." if not ephemeral_required
         else "Ephemeral execution capability is available."
         if burst_configured or eligible_ephemeral else "Ephemeral capacity is unavailable."),
        ephemeral_blockers, required=ephemeral_required,
        burst_enabled=burst.get("enabled") is True,
        max_concurrent=int(burst.get("max_concurrent_ephemeral") or 0),
        eligible_host_ids=eligible_ephemeral)
    if not ephemeral_required:
        ephemeral["status"] = "not_required"

    capacity_passed = (
        (persistent_required and persistent["passed"])
        or (ephemeral_required and ephemeral["passed"]))
    capacity_blockers = []
    if not (persistent_required or ephemeral_required):
        capacity_blockers.append(_blocker(
            "execution_capacity_class_missing", "capacity",
            "No persistent or ephemeral host class is selected.",
            "Select at least one execution host class in the project policy."))
    elif not capacity_passed:
        capacity_blockers.extend(persistent_blockers + ephemeral_blockers)

    autopilot_policy = dict(policy.get("autopilot") or {})
    autopilot_enabled = autopilot_policy.get("enabled") is True
    autopilot_blockers = []
    if autopilot_enabled and not autopilot_policy.get("profile_id"):
        autopilot_blockers.append(_blocker(
            "autopilot_profile_missing", "autopilot",
            "Autopilot is enabled without a policy profile.",
            "Select an Autopilot profile or disable Autopilot."))
    autopilot = _state(
        "autopilot", not autopilot_blockers,
        ("Autopilot is disabled." if not autopilot_enabled
         else "Autopilot policy is ready." if not autopilot_blockers
         else "Autopilot policy needs repair."),
        autopilot_blockers, enabled=autopilot_enabled,
        profile_id=str(autopilot_policy.get("profile_id") or ""))
    if not autopilot_enabled:
        autopilot["status"] = "disabled"

    states = {
        "configuration": configuration,
        "provider": provider,
        "scm": scm,
        "persistent": persistent,
        "ephemeral": ephemeral,
        "autopilot": autopilot,
    }
    blockers = [
        blocker for state in states.values() for blocker in state.get("blockers") or []
    ]
    passed = (
        configuration["passed"] and provider["passed"] and scm["passed"]
        and capacity_passed and autopilot["passed"])
    reason_code = "" if passed else (
        str(blockers[0].get("code")) if blockers else "project_execution_not_ready")
    return {
        "schema": SCHEMA,
        "project": project,
        "status": "ready" if passed else "blocked",
        "passed": passed,
        "reason_code": reason_code,
        "message": (
            "Project is ready for Start and Autopilot admission."
            if passed else "Project execution readiness is blocked."),
        "blockers": blockers,
        "states": states,
        "checked_inputs": {
            "repo_topology": True,
            "execution_policy_revision": (
                (policy.get("lifecycle") or {}).get("revision") or 0),
            "provider_connection_count": len(connections),
            "scm_connection_reference": scm_reference,
            "live_host_count": len(hosts),
        },
    }


__all__ = ["SCHEMA", "get_project_execution_readiness"]
