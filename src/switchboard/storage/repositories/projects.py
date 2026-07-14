"""Projects bootstrap repository (ARCH-MS-48).

Owns ``init_db`` / ``seed_if_empty`` / readiness probe helpers and project
bootstrap surfaces (repo topology, GitHub repo binding, create_project,
project context) previously living in ``repositories/shell.py``. Cross-cutting
helpers stay reachable via ``_store_facade()`` during the strangler.
``store.py`` / ``shell.py`` re-export these symbols; root ``projects_store.py``
is a compatibility shim.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import (  # noqa: F401
    PROJECT_REGISTRY_DB_PATH,
    _conn,
    _dynamic_projects,
    _registry_conn,
    _resolve,
    bust_project_cache,
    project_lifecycle_status,
)
from db.core import *  # noqa: F401,F403
from db.schema import apply_schema, init_project_registry, seed_from_plan  # noqa: F401
from switchboard.storage.repositories.access import (  # noqa: F401
    ensure_bootstrap_project_owner,
    get_project_record,
    grant_project_role,
    normalize_project_id,
    project_access,
    project_ids,
    projects,
    set_project_access,
)
from switchboard.storage.repositories.provenance import _normalize_repo_slug  # noqa: F401
from switchboard.storage.repositories.activity import (  # noqa: F401 — ARCH-MS-55
    append_activity,
    get_meta,
    set_meta,
)


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def list_boards(*args, **kwargs):
    return _store_facade().list_boards(*args, **kwargs)


def list_project_boards(*args, **kwargs):
    return _store_facade().list_project_boards(*args, **kwargs)


def list_deliverable_links_for_task(*args, **kwargs):
    return _store_facade().list_deliverable_links_for_task(*args, **kwargs)


def list_task_deliverable_links(*args, **kwargs):
    return _store_facade().list_task_deliverable_links(*args, **kwargs)


def _task_hierarchy_breadcrumb(*args, **kwargs):
    return _store_facade()._task_hierarchy_breadcrumb(*args, **kwargs)


def code_repo_gate(*args, **kwargs):
    return _store_facade().code_repo_gate(*args, **kwargs)


def init_db(project: str = DEFAULT_PROJECT):
    if project_lifecycle_status(project) == "archived":
        return False
    with _conn(project) as c:
        apply_schema(c)
    return True


def seed_if_empty(project: str = DEFAULT_PROJECT):
    if project_lifecycle_status(project) == "archived":
        return False
    with _conn(project) as c:
        seeded = seed_from_plan(c, _resolve(project)["seed"])
        # COORD-18 historical data repair runs after seeding and on every
        # restart.  It is idempotent, project-scoped, and therefore also heals
        # long-lived live boards whose task rows predate the new tables.
        from switchboard.storage.repositories.review_verdicts import (
            ensure_historical_review_backfills_in,
        )
        ensure_historical_review_backfills_in(c, project)
        return seeded


# Core tables that apply_schema() creates and every request path assumes exist.
# A board db missing any of these is not safely serveable, so readiness fails closed.
READINESS_REQUIRED_TABLES = ("tasks", "activity", "meta")


def probe_project_db(project: str) -> Optional[str]:
    """Cheap liveness+schema check for one board db. Returns None when the db is
    accessible and carries the required schema, else a SHORT reason string that
    NEVER embeds task/project data (safe to surface on an unauthenticated probe)."""
    try:
        with _conn(project) as c:
            present = {
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    except Exception as e:  # locked / missing / corrupt db, permission error, etc.
        return type(e).__name__
    missing = [t for t in READINESS_REQUIRED_TABLES if t not in present]
    if missing:
        return "missing_tables:" + ",".join(missing)
    return None

def _project_env_suffix(project: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (project or "").upper()).strip("_")


def _project_hierarchy_contract(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    return {
        "scope": "project",
        "project_id": project,
        "authority_boundary": [
            "repo",
            "trust",
            "policy",
            "access",
            "ci",
            "model",
            "budget",
            "done",
        ],
        "children": {
            "boards_missions_deliverables": "outcome cockpits under the Project boundary",
            "epics_workstreams_tasks": "execution planning below boards/missions/deliverables",
        },
        "compatibility": {
            "current_switchboard_project_id": project,
            "project_arg_is_workspace_alias": True,
            "repo_topology_is_board_level_truth": False,
        },
    }


def _legacy_project_github_repo(project: str = DEFAULT_PROJECT) -> str:
    configured = (get_meta("github_repo", "", project=project) or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    for key in (
        f"PM_GITHUB_REPO_{suffix}" if suffix else "",
        f"GITHUB_REPOSITORY_{suffix}" if suffix else "",
    ):
        if key and os.environ.get(key):
            return os.environ[key].strip()
    if project in BUILTIN_GITHUB_REPOS:
        return BUILTIN_GITHUB_REPOS[project]
    if project in (DEFAULT_PROJECT, "switchboard"):
        return (os.environ.get("PM_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    return ""


def get_project_github_repo(project: str = DEFAULT_PROJECT) -> str:
    """Canonical repository used for PR-state reconciliation on one board.

    New deployments should read get_project_repo_topology() for all repo roles. This
    compatibility helper still returns the canonical repo so older reconcile and webhook
    paths remain centered on the code-truth repository.
    """
    topology = get_project_repo_topology(project=project)
    return ((topology.get("roles") or {}).get("canonical") or {}).get("repo", "").strip()


def list_canonical_repos(projects: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """Map each configured canonical repo -> the project ids that claim it as code truth.

    Registry-driven so anything that fans out per repo (the PR provenance/claim gate,
    future per-repo automation) automatically covers a new project the moment it sets a
    canonical repo via set_project_repo_topology — no per-repo allowlist to keep in sync.
    A shared repo (e.g. StevenRidder/Helm backs several Helm boards) appears once, mapping
    to all of its projects.
    """
    out: Dict[str, List[str]] = {}
    for pid in (projects if projects is not None else project_ids()):
        try:
            repo = get_project_github_repo(pid)
        except Exception:
            repo = ""
        repo = (repo or "").strip()
        if repo:
            out.setdefault(repo, []).append(pid)
    return out


def resolve_claim_gate_mode(repo: str, primary_repo: str = "",
                            primary_project: str = "switchboard") -> str:
    """Resolve claim-gate mode for a canonical GitHub repo from project repo_topology.

    Each project's ``roles.canonical.claim_gate`` declares off|warn|enforce for fleet
    PR provenance on that repo. The primary repo prefers the CI-home project's mode;
    other canonical repos use the owning project's declaration (default warn).
    """
    repo_norm = _normalize_repo_slug(repo)
    if not repo_norm:
        return DEFAULT_CLAIM_GATE_MODE
    primary_norm = _normalize_repo_slug(primary_repo)
    project_ids: List[str] = []
    for canonical_repo, pids in list_canonical_repos().items():
        if _normalize_repo_slug(canonical_repo) == repo_norm:
            project_ids.extend(pids)
    if not project_ids:
        return DEFAULT_CLAIM_GATE_MODE
    by_project: Dict[str, str] = {}
    for pid in project_ids:
        canonical = ((get_project_repo_topology(project=pid).get("roles") or {})
                     .get("canonical") or {})
        by_project[pid] = _normalize_claim_gate(canonical.get("claim_gate"))
    if primary_norm and repo_norm == primary_norm and primary_project in by_project:
        return by_project[primary_project]
    return next(iter(by_project.values()))


def get_project_repo_role(repo: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Classify one GitHub repo against a project's repo_topology roles."""
    repo_norm = _normalize_repo_slug(repo)
    topology = get_project_repo_topology(project=project)
    roles = topology.get("roles") or {}
    matches: List[Dict[str, Any]] = []
    for role, data in roles.items():
        role_repo = (data or {}).get("repo") or ""
        if repo_norm and _normalize_repo_slug(role_repo) == repo_norm:
            matches.append({
                "role": role,
                "repo": role_repo,
                "authority": list((data or {}).get("authority") or []),
                "default_branch": (data or {}).get("default_branch") or "",
            })
    selected = next((m for m in matches if m["role"] == "canonical"), None)
    selected = selected or (matches[0] if matches else {})
    role = selected.get("role") or "unknown"
    return {
        "project": project,
        "repo": repo,
        "normalized_repo": repo_norm,
        "matched": bool(matches),
        "role": role,
        "canonical": role == "canonical",
        "evidence_only": role in {"public_ci", "public", "release"},
        "authority": selected.get("authority") or [],
        "default_branch": selected.get("default_branch") or "",
        "matches": matches,
        "code_repo_gate": topology.get("code_repo_gate"),
    }


def _validate_github_repo(repo: str) -> Tuple[str, str]:
    clean = (repo or "").strip()
    if clean and not GITHUB_REPO_RE.match(clean):
        return clean, "github repo must be 'owner/name'"
    return clean, ""


def _normalize_session_policy_profile(profile: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "_", (profile or "").strip().lower()).strip("_")
    return SESSION_POLICY_PROFILE_ALIASES.get(clean, clean)


def _session_profile_text(task: Dict[str, Any]) -> str:
    return "\n".join(str(task.get(k) or "") for k in (
        "title", "description", "entry_criteria", "exit_criteria", "deliverable"))


def _project_session_policy_defaults(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    builtins = {
        "helm": {
            "default_profile": "docs_review",
            "code_task_default_profile": "code_strict",
            "notes": ["Helm code tasks default to code_strict; docs/review tasks may opt into docs_review or offline_evidence."],
        },
        "switchboard": {
            "default_profile": "docs_review",
            "code_task_default_profile": "docs_review",
            "notes": ["Switchboard exposes code_strict for code/control-plane tasks; tasks can opt in explicitly while legacy board fixtures remain docs_review by default."],
        },
    }
    default = copy.deepcopy(builtins.get(project) or {
        "default_profile": "docs_review",
        "code_task_default_profile": "docs_review",
        "notes": ["Projects can opt code-like tasks into code_strict by setting code_task_default_profile or a task-level policy_profile."],
    })
    raw = get_meta("session_policy_profiles", {}, project=project) or {}
    if isinstance(raw, dict):
        for key in ("default_profile", "code_task_default_profile"):
            if raw.get(key):
                default[key] = _normalize_session_policy_profile(str(raw.get(key) or ""))
    default["default_profile"] = _normalize_session_policy_profile(default.get("default_profile") or "docs_review")
    default["code_task_default_profile"] = _normalize_session_policy_profile(
        default.get("code_task_default_profile") or "code_strict")
    return default


def get_session_policy_profiles(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Named Work Session enforcement profiles for a project.

    These profiles are intentionally policy data, not hidden prompt convention. Adapters and
    humans can read the same contract before claiming, writing, completing, or merging work.
    """
    profiles = copy.deepcopy(BUILTIN_SESSION_POLICY_PROFILES)
    raw = get_meta("session_policy_profiles", {}, project=project) or {}
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
        for name, data in raw.get("profiles", {}).items():
            normalized = _normalize_session_policy_profile(str(name))
            if not normalized or not isinstance(data, dict):
                continue
            base = copy.deepcopy(profiles.get(normalized) or {"profile": normalized})
            for key, value in data.items():
                if key in {"allowed_storage_modes", "deny_hygiene", "warn_hygiene", "completion_evidence"}:
                    base[key] = _coerce_str_list(value)
                else:
                    base[key] = value
            base["profile"] = normalized
            profiles[normalized] = base

    defaults = _project_session_policy_defaults(project)
    known = sorted(profiles)
    if defaults.get("default_profile") not in profiles:
        defaults["default_profile"] = "docs_review"
    if defaults.get("code_task_default_profile") not in profiles:
        defaults["code_task_default_profile"] = "code_strict"
    return {
        "schema": SESSION_POLICY_PROFILE_SCHEMA,
        "project": project,
        "defaults": defaults,
        "profiles": profiles,
        "known_profiles": known,
        "task_override_fields": [
            "agent_state.session_policy.profile",
            "agent_state.work_session.policy_profile",
            "policy_profile:<name> in task text",
            "session_profile:<name> in task text",
            "claim/pre_tool/complete evidence session_policy_profile",
        ],
    }


def _session_policy_profile_rules(profile: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    profiles = get_session_policy_profiles(project).get("profiles") or {}
    normalized = _normalize_session_policy_profile(profile)
    return copy.deepcopy(profiles.get(normalized) or {})


def _repo_role_template(role: str) -> Dict[str, Any]:
    authority = {
        "canonical": ["done", "merge_provenance", "code_truth"],
        "public_ci": ["verification_only"],
        "public": ["publish_evidence_only"],
        "release": ["release_evidence_only"],
    }.get(role, [])
    return {
        "repo": "",
        "default_branch": "",
        "authority": authority,
        "required_status_contexts": [],
        "sync_scripts": [],
        "publish_scripts": [],
        "configured": False,
    }


def _normalize_claim_gate(mode: Any) -> str:
    normalized = (str(mode or "") or DEFAULT_CLAIM_GATE_MODE).strip().lower()
    return normalized if normalized in CLAIM_GATE_MODES else DEFAULT_CLAIM_GATE_MODE


def _merge_repo_role(roles: Dict[str, Dict[str, Any]], role: str, data) -> None:
    if not isinstance(data, dict):
        return
    role = "public_ci" if role == "ci" else role
    target = roles.setdefault(role, _repo_role_template(role))
    for key, value in data.items():
        if key in {"required_status_contexts", "sync_scripts", "publish_scripts"}:
            merged = _coerce_str_list(value)
            if merged:
                target[key] = merged
        elif key == "claim_gate":
            target[key] = _normalize_claim_gate(value)
        elif value is not None:
            target[key] = value


def get_project_repo_topology(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Repository role contract for one Project authority boundary.

    The canonical role is the only code-truth / Done authority. Public CI,
    public mirror, and release roles are evidence-only carriers. Missing
    canonical repo is exposed as a blocked gate so code-work projects cannot
    silently claim merge provenance.
    """
    raw = get_meta("repo_topology", {}, project=project) or {}
    raw_error = ""
    if raw and not isinstance(raw, dict):
        raw_error = "repo_topology meta must be an object"
        raw = {}

    roles: Dict[str, Dict[str, Any]] = {
        "canonical": _repo_role_template("canonical"),
        "public_ci": _repo_role_template("public_ci"),
        "public": _repo_role_template("public"),
        "release": _repo_role_template("release"),
    }
    topology_type = "single_repo"
    built_in = copy.deepcopy(BUILTIN_REPO_TOPOLOGIES.get(project) or {})
    if built_in.get("topology_type"):
        topology_type = str(built_in.get("topology_type"))
    for role, data in (built_in.get("roles") or {}).items():
        _merge_repo_role(roles, role, data)

    if raw.get("topology_type"):
        topology_type = str(raw.get("topology_type")).strip() or topology_type
    if isinstance(raw.get("roles"), dict):
        for role, data in raw.get("roles", {}).items():
            _merge_repo_role(roles, str(role), data)

    flattened = {
        "canonical_repo": ("canonical", "repo"),
        "private_repo": ("canonical", "repo"),
        "canonical_default_branch": ("canonical", "default_branch"),
        "default_branch": ("canonical", "default_branch"),
        "canonical_claim_gate": ("canonical", "claim_gate"),
        "claim_gate": ("canonical", "claim_gate"),
        "public_ci_repo": ("public_ci", "repo"),
        "ci_repo": ("public_ci", "repo"),
        "public_ci_default_branch": ("public_ci", "default_branch"),
        "ci_default_branch": ("public_ci", "default_branch"),
        "public_ci_required_status_contexts": ("public_ci", "required_status_contexts"),
        "ci_required_status_contexts": ("public_ci", "required_status_contexts"),
        "required_status_contexts": ("public_ci", "required_status_contexts"),
        "public_ci_sync_scripts": ("public_ci", "sync_scripts"),
        "ci_sync_scripts": ("public_ci", "sync_scripts"),
        "sync_scripts": ("public_ci", "sync_scripts"),
        "public_repo": ("public", "repo"),
        "public_default_branch": ("public", "default_branch"),
        "public_publish_scripts": ("public", "publish_scripts"),
        "publish_scripts": ("public", "publish_scripts"),
        "release_repo": ("release", "repo"),
        "release_default_branch": ("release", "default_branch"),
        "release_publish_scripts": ("release", "publish_scripts"),
    }
    for key, (role, field) in flattened.items():
        if key in raw and raw.get(key) not in (None, ""):
            role_data = roles.setdefault(role, _repo_role_template(role))
            if field in {"required_status_contexts", "sync_scripts", "publish_scripts"}:
                role_data[field] = _coerce_str_list(raw.get(key))
            else:
                role_data[field] = str(raw.get(key)).strip()

    if not (roles.get("canonical") or {}).get("repo"):
        roles["canonical"]["repo"] = _legacy_project_github_repo(project)

    missing: List[str] = []
    warnings: List[str] = []
    invalid: List[Dict[str, str]] = []
    if raw_error:
        warnings.append(raw_error)
    for role, data in roles.items():
        for field in ("required_status_contexts", "sync_scripts", "publish_scripts"):
            data[field] = _coerce_str_list(data.get(field))
        if role == "canonical":
            data["claim_gate"] = _normalize_claim_gate(data.get("claim_gate"))
        repo, error = _validate_github_repo(data.get("repo", ""))
        data["repo"] = repo
        data["configured"] = bool(repo)
        if error:
            data["configured"] = False
            invalid.append({"role": role, "field": "repo", "error": error, "value": repo})
    if invalid:
        warnings.append("one or more repo roles have invalid owner/name values")
    if not roles["canonical"].get("configured"):
        missing.append("roles.canonical.repo")

    gate_passed = not missing and not any(item.get("role") == "canonical" for item in invalid)
    gate = {
        "name": "canonical_repo_configured",
        "passed": gate_passed,
        "status": "passed" if gate_passed else "blocked",
        "message": (
            "canonical repo configured; code Done must be proven from this repo"
            if gate_passed else
            "missing canonical repo; code-work Done cannot be proven by webhook/reconcile"
        ),
    }
    return {
        "schema": REPO_TOPOLOGY_SCHEMA,
        "scope": "project",
        "project": project,
        "project_hierarchy": _project_hierarchy_contract(project),
        "topology_type": topology_type,
        "roles": roles,
        "aliases": {"ci": "public_ci", "private": "canonical"},
        "authority": {
            "done": "canonical",
            "merge_provenance": "canonical",
            "ci_verification": "public_ci",
            "publication": "public",
            "release": "release",
        },
        "code_repo_gate": gate,
        "valid": gate_passed,
        "missing": missing,
        "invalid": invalid,
        "warnings": warnings,
        "notes": [
            "canonical repo is the only code-truth and Done authority",
            "public_ci/public/release repos are evidence roles and cannot mark code work Done",
        ],
    }


def set_project_github_repo(repo: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    repo, error = _validate_github_repo(repo)
    if error:
        return {"error": error, "repo": repo, "project": project}
    set_meta("github_repo", repo, project=project)
    topology = get_meta("repo_topology", {}, project=project) or {}
    if isinstance(topology, dict) and topology:
        roles = topology.setdefault("roles", {})
        canonical = roles.setdefault("canonical", {})
        canonical["repo"] = repo
        set_meta("repo_topology", topology, project=project)
    return {"project": project, "github_repo": repo,
            "repo_topology": get_project_repo_topology(project=project)}


def github_repo_reachable(repo: str) -> Optional[bool]:
    """Best-effort reachability probe for a repo (UI-15 Verify button, explicit only).

    True = the repo exists and we can see it; False = a definitive not-found/forbidden;
    None = the probe itself was inconclusive (offline, rate-limited, timeout). Uses the
    same optional token as reconcile so private canonical repos resolve when creds exist.
    """
    if not repo or "/" not in repo:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}")
    req.add_header("Accept", "application/vnd.github+json")
    token = _github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return (getattr(r, "status", None) or r.getcode()) == 200
    except Exception as exc:  # HTTPError carries .code; other errors are inconclusive
        code = getattr(exc, "code", 0) or 0
        if code in (401, 403, 404):
            return False
        return None


def set_project_repo_topology(project: str = DEFAULT_PROJECT, canonical_repo: str = "",
                              public_ci_repo: str = "", public_repo: str = "",
                              release_repo: str = "", topology_type: str = "",
                              canonical_default_branch: str = "",
                              canonical_claim_gate: str = "",
                              public_ci_required_status_contexts=None,
                              public_ci_sync_scripts=None,
                              public_publish_scripts=None,
                              release_publish_scripts=None,
                              ci_repo: str = "", ci_required_status_contexts=None,
                              ci_sync_scripts=None) -> Dict[str, Any]:
    if ci_repo and not public_ci_repo:
        public_ci_repo = ci_repo
    if ci_required_status_contexts and not public_ci_required_status_contexts:
        public_ci_required_status_contexts = ci_required_status_contexts
    if ci_sync_scripts and not public_ci_sync_scripts:
        public_ci_sync_scripts = ci_sync_scripts

    updates = {
        "canonical": {"repo": canonical_repo, "default_branch": canonical_default_branch,
                      "claim_gate": canonical_claim_gate},
        "public_ci": {"repo": public_ci_repo,
                      "required_status_contexts": public_ci_required_status_contexts,
                      "sync_scripts": public_ci_sync_scripts},
        "public": {"repo": public_repo, "publish_scripts": public_publish_scripts},
        "release": {"repo": release_repo, "publish_scripts": release_publish_scripts},
    }
    for role, data in updates.items():
        repo = (data.get("repo") or "").strip()
        if repo:
            _, error = _validate_github_repo(repo)
            if error:
                return {"error": error, "repo": repo, "role": role, "project": project}

    topology = get_meta("repo_topology", {}, project=project) or {}
    if not isinstance(topology, dict):
        topology = {}
    topology["schema"] = REPO_TOPOLOGY_SCHEMA
    if (topology_type or "").strip():
        topology["topology_type"] = topology_type.strip()
    roles = topology.setdefault("roles", {})
    for role, data in updates.items():
        target = roles.setdefault(role, {})
        repo = (data.get("repo") or "").strip()
        if repo:
            target["repo"] = repo
        default_branch = (data.get("default_branch") or "").strip()
        if default_branch:
            target["default_branch"] = default_branch
        claim_gate = (data.get("claim_gate") or "").strip()
        if claim_gate and role == "canonical":
            target["claim_gate"] = _normalize_claim_gate(claim_gate)
        for field in ("required_status_contexts", "sync_scripts", "publish_scripts"):
            values = _coerce_str_list(data.get(field))
            if values:
                target[field] = values
    set_meta("repo_topology", topology, project=project)
    canonical = ((topology.get("roles") or {}).get("canonical") or {}).get("repo", "").strip()
    if canonical:
        set_meta("github_repo", canonical, project=project)
    return {"project": project, "repo_topology": get_project_repo_topology(project=project)}


REPO_ROLE_LABELS = {
    "canonical": "Done / code truth",
    "public_ci": "CI verification only",
    "public": "Public mirror publication evidence only",
    "release": "Release evidence only",
}


def _repo_role_summary(role: str, data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    repo = (data.get("repo") or "").strip()
    placeholder = (data.get("repo_placeholder") or "").strip()
    return {
        "role": role,
        "label": REPO_ROLE_LABELS.get(role, role),
        "repo": repo or placeholder or None,
        "configured": bool(data.get("configured")),
        "default_branch": data.get("default_branch") or "",
        "authority": list(data.get("authority") or []),
        "description": data.get("description") or "",
    }


def repo_topology_role_guide(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Operator/agent cheat sheet: which repo controls Done, CI, and publication."""
    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public_ci = roles.get("public_ci") or {}
    public = roles.get("public") or {}
    release = roles.get("release") or {}
    ci_message = "public_ci verifies canonical SHAs but is not code truth."
    if project == "helm":
        ci_message += " helm-ci is CI-only; canonical Done remains private Helm merge provenance."
    return {
        "project": project,
        "topology_type": topology.get("topology_type"),
        "done_authority": {
            "role": "canonical",
            "repo": (canonical.get("repo") or "").strip() or None,
            "default_branch": canonical.get("default_branch") or "",
            "message": "Only the canonical repo can mark code work Done via merge provenance.",
        },
        "ci_verification": {
            "role": "public_ci",
            "repo": ((public_ci.get("repo") or "").strip()
                     or (public_ci.get("repo_placeholder") or "").strip() or None),
            "default_branch": public_ci.get("default_branch") or "",
            "message": ci_message,
        },
        "publication_evidence": {
            "role": "public",
            "repo": ((public.get("repo") or "").strip()
                     or (public.get("repo_placeholder") or "").strip() or None),
            "default_branch": public.get("default_branch") or "",
            "message": "public mirror roles carry publish evidence only; they never prove code Done.",
        },
        "release_evidence": {
            "role": "release",
            "repo": (release.get("repo") or "").strip() or None,
            "message": "release roles carry release/packaging evidence only.",
        },
        "role_summaries": [
            _repo_role_summary(role, data)
            for role, data in (
                ("canonical", canonical),
                ("public_ci", public_ci),
                ("public", public),
                ("release", release),
            )
        ],
    }


def get_project_context(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    topology = get_project_repo_topology(project)
    access = project_access(project)
    boards = list_project_boards(project=project)
    hierarchy = topology.get("project_hierarchy") or _project_hierarchy_contract(project)
    return {
        "project": project,
        "project_label": next((p.get("label") for p in projects() if p["id"] == project), project),
        "project_boundary": access.get("boundary") or "",
        "project_purpose": access.get("purpose") or "",
        "project_hierarchy": hierarchy,
        "hierarchy_stack": [
            {"level": "project", "id": project, "label": hierarchy.get("project_id") or project},
            {"level": "board_or_mission",
             "note": hierarchy["children"]["boards_missions_deliverables"]},
            {"level": "epic_or_workstream",
             "note": hierarchy["children"]["epics_workstreams_tasks"]},
            {"level": "task", "note": "atomic execution unit with provenance and gates"},
        ],
        "repo_topology": topology,
        "repo_role_guide": repo_topology_role_guide(project),
        "session_policy_profiles": get_session_policy_profiles(project),
        "boards_missions": boards,
        "code_repo_gate": topology.get("code_repo_gate"),
    }


def _enrich_task_project_context(task: Dict[str, Any], project: str = DEFAULT_PROJECT) -> None:
    ctx = get_project_context(project)
    links = list_task_deliverable_links(task.get("task_id") or "", project=project)
    task["project_context"] = {
        "project": project,
        "project_hierarchy": ctx.get("project_hierarchy"),
        "hierarchy_breadcrumb": _task_hierarchy_breadcrumb(task, project, links=links),
        "repo_topology": ctx.get("repo_topology"),
        "repo_role_guide": ctx.get("repo_role_guide"),
        "session_policy_profiles": ctx.get("session_policy_profiles"),
        "boards_missions": ctx.get("boards_missions"),
        "deliverable_links": links,
        "code_repo_gate": ctx.get("code_repo_gate"),
    }


def create_project(name: str, project_id: str = "", label: str = "", pretitle: str = "",
                   actor: str = "system", seed_path: str = "",
                   github_repo: str = "", owner_principal_id: str = "",
                   org_id: str = DEFAULT_ORG_ID, purpose: str = "",
                   boundary: str = "", visibility: str = "") -> Dict[str, Any]:
    """Create a physically isolated project board and register it for routing.

    Dynamic projects mirror the built-ins: one row in the lightweight registry, one SQLite
    file for that board's actual task/activity state. The returned id is the value callers pass
    as project="..." to all normal board tools.
    """
    clean_name = (name or "").strip()
    pid = normalize_project_id(project_id or clean_name)
    if not clean_name and not pid:
        return {"error": "project name or project_id required"}
    if not PROJECT_ID_VALID_RE.match(pid):
        return {"error": "invalid project id; use 2-63 chars: lowercase letters, digits, '-' or '_'",
                "project_id": pid}
    repo, repo_error = _validate_github_repo(github_repo)
    if repo_error:
        return {"error": repo_error, "repo": repo, "project_id": pid}

    existing = _dynamic_projects().get(pid)
    if existing:
        if get_project_record(pid).get("is_protected"):
            return {"error": f"reserved protected project id: {pid}", "project_id": pid}
        init_db(pid)
        seed_if_empty(pid)
        if repo:
            set_meta("github_repo", repo, project=pid)
        current_access = project_access(pid)
        access = set_project_access(
            pid,
            org_id or current_access.get("org_id") or DEFAULT_ORG_ID,
            owner_user_id=owner_principal_id or current_access.get("owner_user_id") or "",
            purpose=purpose or current_access.get("purpose") or f"{pid} work control plane",
            boundary=boundary or current_access.get("boundary") or f"Only work belonging to project={pid} belongs here.",
            created_by=actor,
            visibility=visibility,
        )
        grant = {}
        if owner_principal_id:
            grant = grant_project_role(pid, "principal", owner_principal_id, "admin",
                                       created_by=actor)
        return {"created": False, "project": {"id": pid, "label": existing["label"],
                "pretitle": existing.get("pretitle", ""), "db": existing["db"],
                "seed": existing.get("seed"),
                "github_repo": get_project_github_repo(pid) or None,
                "repo_topology": get_project_repo_topology(pid),
                "access": access, "owner_grant": grant or None}}

    base_dir = os.environ.get("PM_DYNAMIC_PROJECTS_DIR") or os.path.dirname(PROJECT_REGISTRY_DB_PATH)
    os.makedirs(base_dir, exist_ok=True)
    db_path = os.path.join(base_dir, f"{pid}.db")
    project_label = (label or clean_name or pid).strip()
    project_pretitle = (pretitle or "").strip()
    seed = (seed_path or "").strip() or None
    now = time.time()

    init_project_registry()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO projects(id, label, pretitle, db_path, seed_path, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, project_label, project_pretitle, db_path, seed, now, actor),
        )
    bust_project_cache()  # read-your-write: the new project resolves immediately in this process
    try:
        init_db(pid)
        set_meta("project", project_label, project=pid)
        set_meta("people", DEFAULT_PEOPLE, project=pid)
        if project_pretitle:
            set_meta("pretitle", project_pretitle, project=pid)
        if repo:
            set_meta("github_repo", repo, project=pid)
        if seed:
            seed_if_empty(pid)
        access = set_project_access(
            pid,
            org_id or DEFAULT_ORG_ID,
            owner_user_id=owner_principal_id or "",
            purpose=purpose or f"{pid} work control plane",
            boundary=boundary or f"Only work belonging to project={pid} belongs here.",
            created_by=actor,
            visibility=visibility,
        )
        grant = {}
        if owner_principal_id:
            grant = grant_project_role(pid, "principal", owner_principal_id, "admin",
                                       created_by=actor)
    except Exception:
        with _registry_conn() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
        raise

    return {"created": True, "project": {"id": pid, "label": project_label,
            "pretitle": project_pretitle, "db": db_path, "seed": seed,
            "github_repo": get_project_github_repo(pid) or None,
            "repo_topology": get_project_repo_topology(pid),
            "access": access, "owner_grant": grant or None}}


class StoreProjectsRepository:
    """Thin repository wrapper over module-level project bootstrap helpers."""

    def init_db(self, project=DEFAULT_PROJECT):
        return init_db(project)

    def seed_if_empty(self, project=DEFAULT_PROJECT):
        return seed_if_empty(project)

    def probe_project_db(self, project):
        return probe_project_db(project)

    def create_project(self, *args, **kwargs):
        return create_project(*args, **kwargs)

    def get_project_repo_topology(self, project=DEFAULT_PROJECT):
        return get_project_repo_topology(project)

    def set_project_repo_topology(self, *args, **kwargs):
        return set_project_repo_topology(*args, **kwargs)

    def get_project_github_repo(self, project=DEFAULT_PROJECT):
        return get_project_github_repo(project)

    def set_project_github_repo(self, repo, project=DEFAULT_PROJECT):
        return set_project_github_repo(repo, project=project)

    def get_project_context(self, project=DEFAULT_PROJECT):
        return get_project_context(project)

    def get_session_policy_profiles(self, project=DEFAULT_PROJECT):
        return get_session_policy_profiles(project)


def default_projects_repository() -> StoreProjectsRepository:
    return StoreProjectsRepository()


__all__ = [
    "READINESS_REQUIRED_TABLES",
    "StoreProjectsRepository",
    "default_projects_repository",
    "init_db",
    "seed_if_empty",
    "probe_project_db",
    "create_project",
    "get_project_github_repo",
    "set_project_github_repo",
    "get_project_repo_topology",
    "set_project_repo_topology",
    "get_project_repo_role",
    "list_canonical_repos",
    "resolve_claim_gate_mode",
    "get_project_context",
    "get_session_policy_profiles",
    "repo_topology_role_guide",
    "github_repo_reachable",
    "_enrich_task_project_context",
    "_project_env_suffix",
    "_project_hierarchy_contract",
    "_legacy_project_github_repo",
    "_validate_github_repo",
    "_normalize_session_policy_profile",
    "_session_profile_text",
    "_project_session_policy_defaults",
    "_session_policy_profile_rules",
    "_repo_role_template",
    "_normalize_claim_gate",
    "_merge_repo_role",
    "_repo_role_summary",
]
