"""Project execution policy repository (ACCESS-27).

The project-level authority a runner reads before it places, materializes, or
authorizes work: which runtimes may run, which repo role and isolation the
workspace is built from, which host classes and trust zones may host it, whether
burst capacity is allowed, which provider selectors and SCM connection apply, and
whether Autopilot is enabled.

Two rules keep this a *policy* object rather than a second configuration system:

* references and policy only — provider and SCM material is referenced by id, never
  copied here;
* no per-project branches or env configuration — branch truth stays in
  ``repo_topology``, and env/secret material stays in the credential vault.

Both are enforced on write, so a project can never be onboarded by smuggling
environment configuration through the policy record.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    DEFAULT_PROJECT,
    PROJECT_EXECUTION_HOST_CLASSES,
    PROJECT_EXECUTION_ISOLATION_MODES,
    PROJECT_EXECUTION_POLICY_SCHEMA,
    PROJECT_EXECUTION_POLICY_STATUSES,
    PROJECT_EXECUTION_RUNTIMES,
    PROJECT_EXECUTION_TRUST_ZONES,
    PROJECT_EXECUTION_WORKSPACE_ROLES,
)
from switchboard.storage.repositories.activity import append_activity, get_meta, set_meta
from switchboard.storage.repositories.projects import get_project_repo_topology

META_KEY = "execution_policy"

# Fields that would turn this policy into per-project branch/env configuration.
# Rejected on write so the constraint cannot erode one caller at a time.
FORBIDDEN_INPUT_FIELDS = (
    "branch", "branches", "default_branch", "env", "environment", "env_file",
    "secret", "secrets", "token", "tokens", "password", "api_key",
    "credential_value", "private_key",
)

READINESS_GATE = "project_execution_policy_ready"


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.replace(",", " ").split()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        return []
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_policy() -> Dict[str, Any]:
    """The empty shape. Deliberately unconfigured — readiness fails closed."""
    return {
        "runtimes": {"allowed": [], "default": ""},
        "workspace": {"repo_role": "canonical", "isolation": "worktree"},
        "placement": {
            "host_classes": [],
            "trust_zones": [],
            "burst": {"enabled": False, "max_concurrent_ephemeral": 0},
        },
        "providers": {"selectors": []},
        "scm": {"provider": "", "connection_reference": ""},
        "autopilot": {"enabled": False, "profile_id": ""},
        "lifecycle": {
            "status": "draft", "revision": 0,
            "created_at": None, "updated_at": None, "updated_by": "",
        },
    }


def _normalize_selector(raw: Any, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip()
    reference = str(
        raw.get("connection_reference") or raw.get("credential_reference") or "").strip()
    return {
        "provider": provider,
        "connection_reference": reference,
        "account_affinity_id": str(raw.get("account_affinity_id") or "").strip(),
        "priority": _as_int(raw.get("priority"), index),
    }


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    policy = _default_policy()
    if not isinstance(raw, dict):
        return policy

    runtimes = raw.get("runtimes") if isinstance(raw.get("runtimes"), dict) else {}
    policy["runtimes"]["allowed"] = _str_list(runtimes.get("allowed"))
    policy["runtimes"]["default"] = str(runtimes.get("default") or "").strip()

    workspace = raw.get("workspace") if isinstance(raw.get("workspace"), dict) else {}
    if workspace.get("repo_role"):
        policy["workspace"]["repo_role"] = str(workspace.get("repo_role")).strip()
    if workspace.get("isolation"):
        policy["workspace"]["isolation"] = str(workspace.get("isolation")).strip()

    placement = raw.get("placement") if isinstance(raw.get("placement"), dict) else {}
    policy["placement"]["host_classes"] = _str_list(placement.get("host_classes"))
    policy["placement"]["trust_zones"] = _str_list(placement.get("trust_zones"))
    burst = placement.get("burst") if isinstance(placement.get("burst"), dict) else {}
    policy["placement"]["burst"] = {
        "enabled": _as_bool(burst.get("enabled")),
        "max_concurrent_ephemeral": max(
            0, _as_int(burst.get("max_concurrent_ephemeral"), 0)),
    }

    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    selectors = providers.get("selectors")
    policy["providers"]["selectors"] = [
        selector for selector in (
            _normalize_selector(item, index)
            for index, item in enumerate(selectors if isinstance(selectors, list) else [])
        ) if selector is not None
    ]

    scm = raw.get("scm") if isinstance(raw.get("scm"), dict) else {}
    policy["scm"] = {
        "provider": str(scm.get("provider") or "").strip(),
        "connection_reference": str(
            scm.get("connection_reference") or scm.get("connection_id") or "").strip(),
    }

    autopilot = raw.get("autopilot") if isinstance(raw.get("autopilot"), dict) else {}
    policy["autopilot"] = {
        "enabled": _as_bool(autopilot.get("enabled")),
        "profile_id": str(autopilot.get("profile_id") or "").strip(),
    }

    lifecycle = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), dict) else {}
    policy["lifecycle"] = {
        "status": str(lifecycle.get("status") or "draft").strip().lower(),
        "revision": max(0, _as_int(lifecycle.get("revision"), 0)),
        "created_at": lifecycle.get("created_at"),
        "updated_at": lifecycle.get("updated_at"),
        "updated_by": str(lifecycle.get("updated_by") or ""),
    }
    return policy


def _validate(policy: Dict[str, Any], project: str,
              configured: bool) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Return (missing, invalid). ``missing`` is unset-but-required policy."""
    missing: List[str] = []
    invalid: List[Dict[str, Any]] = []

    def bad(field: str, value: Any, error: str) -> None:
        invalid.append({"field": field, "value": value, "error": error})

    allowed = policy["runtimes"]["allowed"]
    if not allowed:
        missing.append("runtimes.allowed")
    for runtime in allowed:
        if runtime not in PROJECT_EXECUTION_RUNTIMES:
            bad("runtimes.allowed", runtime, "unknown runtime")
    default_runtime = policy["runtimes"]["default"]
    if not default_runtime:
        missing.append("runtimes.default")
    elif default_runtime not in allowed:
        bad("runtimes.default", default_runtime, "default runtime is not allowed")

    repo_role = policy["workspace"]["repo_role"]
    if repo_role not in PROJECT_EXECUTION_WORKSPACE_ROLES:
        bad("workspace.repo_role", repo_role, "unknown repo role")
    elif configured:
        # A workspace can only be materialized from a repo role this project has
        # actually configured; otherwise readiness would lie to the runner.
        roles = (get_project_repo_topology(project).get("roles") or {})
        if not (roles.get(repo_role) or {}).get("configured"):
            bad("workspace.repo_role", repo_role,
                "repo role is not configured in repo_topology")
    isolation = policy["workspace"]["isolation"]
    if isolation not in PROJECT_EXECUTION_ISOLATION_MODES:
        bad("workspace.isolation", isolation, "unknown isolation mode")

    host_classes = policy["placement"]["host_classes"]
    if not host_classes:
        missing.append("placement.host_classes")
    for host_class in host_classes:
        if host_class not in PROJECT_EXECUTION_HOST_CLASSES:
            bad("placement.host_classes", host_class, "unknown host class")
    trust_zones = policy["placement"]["trust_zones"]
    if not trust_zones:
        missing.append("placement.trust_zones")
    for zone in trust_zones:
        if zone not in PROJECT_EXECUTION_TRUST_ZONES:
            bad("placement.trust_zones", zone, "unknown trust zone")

    burst = policy["placement"]["burst"]
    if burst["enabled"]:
        if "ephemeral" not in host_classes:
            bad("placement.burst.enabled", True,
                "burst requires the ephemeral host class")
        if burst["max_concurrent_ephemeral"] <= 0:
            bad("placement.burst.max_concurrent_ephemeral",
                burst["max_concurrent_ephemeral"],
                "burst requires a positive ephemeral ceiling")

    selectors = policy["providers"]["selectors"]
    if not selectors:
        missing.append("providers.selectors")
    for index, selector in enumerate(selectors):
        if not selector.get("provider"):
            bad(f"providers.selectors[{index}].provider", "", "provider is required")
        if not selector.get("connection_reference"):
            bad(f"providers.selectors[{index}].connection_reference", "",
                "connection reference is required")

    if not policy["scm"]["connection_reference"]:
        missing.append("scm.connection_reference")
    if not policy["scm"]["provider"]:
        missing.append("scm.provider")

    if policy["autopilot"]["enabled"] and not policy["autopilot"]["profile_id"]:
        bad("autopilot.profile_id", "", "Autopilot enablement requires a profile id")

    status = policy["lifecycle"]["status"]
    if status not in PROJECT_EXECUTION_POLICY_STATUSES:
        bad("lifecycle.status", status, "unknown lifecycle status")
    return missing, invalid


def _readiness(policy: Dict[str, Any], *, configured: bool, missing: List[str],
               invalid: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Typed readiness. Never optimistic: absent policy is a blocked gate."""
    status = policy["lifecycle"]["status"]
    if not configured:
        reason_code = "project_execution_policy_missing"
        message = ("no project execution policy is configured; runners cannot place, "
                   "materialize, or authorize work for this project")
    elif invalid:
        reason_code = "project_execution_policy_invalid"
        message = "project execution policy has invalid values"
    elif missing:
        reason_code = "project_execution_policy_incomplete"
        message = "project execution policy is missing required fields"
    elif status != "active":
        reason_code = "project_execution_policy_not_active"
        message = f"project execution policy lifecycle status is '{status}', not 'active'"
    else:
        return {
            "schema": PROJECT_EXECUTION_POLICY_SCHEMA,
            "name": READINESS_GATE,
            "passed": True,
            "status": "passed",
            "reason_code": "",
            "message": "project execution policy is active and complete",
            "missing": [],
            "invalid": [],
        }
    return {
        "schema": PROJECT_EXECUTION_POLICY_SCHEMA,
        "name": READINESS_GATE,
        "passed": False,
        "status": "blocked",
        "reason_code": reason_code,
        "message": message,
        "missing": list(missing),
        "invalid": copy.deepcopy(invalid),
    }


def get_project_execution_policy(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Normalized execution policy plus its typed readiness gate for one project."""
    raw = get_meta(META_KEY, {}, project=project) or {}
    warnings: List[str] = []
    if raw and not isinstance(raw, dict):
        warnings.append("execution_policy meta must be an object")
        raw = {}
    configured = bool(raw)
    policy = _normalize(raw)
    missing, invalid = _validate(policy, project, configured)
    readiness = _readiness(policy, configured=configured, missing=missing, invalid=invalid)
    return {
        "schema": PROJECT_EXECUTION_POLICY_SCHEMA,
        "scope": "project",
        "project": project,
        "configured": configured,
        **policy,
        "readiness": readiness,
        "valid": readiness["passed"],
        "missing": missing,
        "invalid": invalid,
        "warnings": warnings,
        "notes": [
            "references and policy only; provider and SCM material is referenced by id",
            "branch truth stays in repo_topology; env/secret material stays in the vault",
        ],
    }


def project_execution_readiness(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Just the typed gate — for readiness probes that do not want the whole policy."""
    return get_project_execution_policy(project)["readiness"]


def _forbidden_fields(updates: Dict[str, Any]) -> List[str]:
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).strip().lower() in FORBIDDEN_INPUT_FIELDS:
                    found.append(str(key))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(updates)
    return found


def set_project_execution_policy(project: str = DEFAULT_PROJECT, *,
                                 updates: Optional[Dict[str, Any]] = None,
                                 actor: str = "") -> Dict[str, Any]:
    """Merge ``updates`` into the stored policy after validating the merged result.

    Fails closed: an invalid or forbidden update is rejected with a typed error and
    nothing is persisted, so a half-configured project can never look ready.
    """
    updates = updates if isinstance(updates, dict) else {}
    forbidden = _forbidden_fields(updates)
    if forbidden:
        return {
            "error": "project_execution_policy_forbidden_field",
            "project": project,
            "fields": sorted(set(forbidden)),
            "message": ("project execution policy stores references and policy only; "
                        "branch truth belongs to repo_topology and env/secret material "
                        "to the credential vault"),
        }

    stored = get_meta(META_KEY, {}, project=project) or {}
    if not isinstance(stored, dict):
        stored = {}
    merged = _normalize(_deep_merge(_normalize(stored) if stored else {}, updates))

    now = time.time()
    lifecycle = merged["lifecycle"]
    previous = (stored.get("lifecycle") or {}) if isinstance(stored, dict) else {}
    lifecycle["revision"] = max(0, _as_int(previous.get("revision"), 0)) + 1
    lifecycle["created_at"] = previous.get("created_at") or now
    lifecycle["updated_at"] = now
    lifecycle["updated_by"] = actor or lifecycle.get("updated_by") or ""

    missing, invalid = _validate(merged, project, True)
    if invalid:
        return {
            "error": "project_execution_policy_invalid",
            "project": project,
            "invalid": invalid,
            "missing": missing,
            "message": "project execution policy update rejected; nothing was persisted",
        }

    record = {"schema": PROJECT_EXECUTION_POLICY_SCHEMA, **merged}
    set_meta(META_KEY, record, project=project)
    result = get_project_execution_policy(project)
    append_activity(
        "project.execution_policy_configured", actor or "system",
        {
            "project": project,
            "revision": lifecycle["revision"],
            "lifecycle_status": lifecycle["status"],
            "readiness": result["readiness"]["reason_code"] or "ready",
        },
        task_id=None, project=project)
    return {"project": project, "execution_policy": result}


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge nested dicts; lists and scalars are replaced wholesale."""
    merged = copy.deepcopy(base)
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class StoreProjectExecutionPolicyRepository:
    get = staticmethod(get_project_execution_policy)
    set = staticmethod(set_project_execution_policy)
    readiness = staticmethod(project_execution_readiness)


def default_project_execution_policy_repository() -> StoreProjectExecutionPolicyRepository:
    return StoreProjectExecutionPolicyRepository()


__all__ = [
    "META_KEY",
    "READINESS_GATE",
    "FORBIDDEN_INPUT_FIELDS",
    "StoreProjectExecutionPolicyRepository",
    "default_project_execution_policy_repository",
    "get_project_execution_policy",
    "set_project_execution_policy",
    "project_execution_readiness",
]
