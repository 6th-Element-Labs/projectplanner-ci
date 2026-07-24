"""Compatibility composition root for Switchboard persistence (ARCH-MS-45/64).

Callers may keep ``import store``. Implementation lives under ``src/switchboard/``
and leaf ``*_store.py`` shims. This module is import-only — no business logic or SQL.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, Optional, Tuple, Union

import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

_AttrSpec = Union[Tuple[str, str], Tuple[str, str, str]]
_cache: Dict[str, Any] = {}
_index: Optional[Dict[str, str]] = None

_EXPORT_MODULES: Tuple[str, ...] = (
    "constants", "db.core", "db.schema", "db.connection",
    "rag_store", "digests_store", "inbox_store", "summaries_store",
    "decisions_store", "jobs_store",
    "switchboard.storage.repositories.runner",
    "switchboard.storage.repositories.access",
    "switchboard.storage.repositories.tasks",
    "switchboard.storage.repositories.claims",
    "switchboard.storage.repositories.coordination",
    "switchboard.storage.repositories.agent_host_enrollments",
    "switchboard.storage.repositories.provenance",
    "switchboard.storage.repositories.deliverables",
    "switchboard.storage.repositories.work_sessions",
    "switchboard.storage.repositories.external_ci",
    "switchboard.storage.repositories.external_effects",
    "switchboard.storage.repositories.activity",
    "switchboard.storage.repositories.autopilot_scopes",
    "switchboard.storage.repositories.kickoff",
    "switchboard.storage.repositories.lifecycle_cleanup",
    "switchboard.storage.repositories.narration",
    "switchboard.storage.repositories.plan_chat",
    "switchboard.storage.repositories.publication",
    "switchboard.storage.repositories.projects",
    "switchboard.storage.repositories.project_execution_policy",
    "switchboard.storage.repositories.project_execution_readiness",
    "switchboard.storage.repositories.kpis_economics",
    "switchboard.storage.repositories.review_verdicts",
    "switchboard.storage.repositories.review_remediations",
    "switchboard.storage.repositories.attention",
    "switchboard.storage.repositories.preflight_runs",
    "switchboard.domain.access.identity",
    "switchboard.domain.provenance.preflight",
    "switchboard.application.commands.pre_tool_check",
    "switchboard.application.commands.merge_gate",
    "switchboard.domain.board.tasks",
    "switchboard.domain.coordination.delivery",
    "switchboard.domain.coordination.terminal",
    "switchboard.domain.deliverables.lifecycle",
    "switchboard.domain.provenance.git",
    "switchboard.domain.bug_intake.policy",
    "switchboard.domain.ixp.protocol",
    "switchboard.application.commands.submit_bug",
    "switchboard.application.queries.control_plane_probe",
    "switchboard.application.queries.audit_export",
    "switchboard.application.queries.working_agreement",
    "switchboard.application.queries.preflight_calibration",
    "read_cache",
)

_MODULE_EXPORTS: Dict[str, str] = {
    "deliverable_gates": "deliverable_gates",
    "deliverable_policy": "deliverable_policy",
    "narration_outbox": "narration_outbox",
    "push_verification": "push_verification",
    "hashlib": "hashlib",
    "shutil": "shutil",
    "urllib": "urllib",
    "scripts": "scripts",
}

_EXPORT_ALIASES: Dict[str, _AttrSpec] = {
    "_apply_terminal_done_view": ("switchboard.domain.board.tasks", "apply_terminal_done_view"),
    "_is_terminal_done_task": ("switchboard.domain.board.tasks", "is_terminal_done_task"),
    "_normalize_depends_on": ("switchboard.domain.board.tasks", "normalize_depends_on"),
    "_rationale_state": ("switchboard.domain.board.tasks", "rationale_state"),
    "_has_done_provenance": ("switchboard.domain.provenance.git", "has_done_provenance"),
    "_offline_evidence_from_state": ("switchboard.domain.provenance.git", "offline_evidence_from_state"),
    "_provenance_summary": ("switchboard.domain.provenance.git", "provenance_summary"),
    "_valid_evidence_hash": ("switchboard.domain.provenance.git", "valid_evidence_hash"),
    "task_repository": ("switchboard.storage.repositories.tasks", "default_task_repository", "call"),
    "claims_repository": ("switchboard.storage.repositories.claims", "default_claims_repository", "call"),
    "coordination_repository": ("switchboard.storage.repositories.coordination", "default_coordination_repository", "call"),
    "agent_host_enrollment_repository": ("switchboard.storage.repositories.agent_host_enrollments", "default_agent_host_enrollment_repository", "call"),
    "provenance_repository": ("switchboard.storage.repositories.provenance", "default_provenance_repository", "call"),
    "deliverables_repository": ("switchboard.storage.repositories.deliverables", "default_deliverables_repository", "call"),
    "work_sessions_repository": ("switchboard.storage.repositories.work_sessions", "default_work_sessions_repository", "call"),
    "external_ci_repository": ("switchboard.storage.repositories.external_ci", "default_external_ci_repository", "call"),
    "external_effects_repository": ("switchboard.storage.repositories.external_effects", "default_external_effects_repository", "call"),
    "activity_repository": ("switchboard.storage.repositories.activity", "default_activity_repository", "call"),
    "lifecycle_cleanup_repository": ("switchboard.storage.repositories.lifecycle_cleanup", "default_lifecycle_cleanup_repository", "call"),
    "narration_repository": ("switchboard.storage.repositories.narration", "default_narration_repository", "call"),
    "plan_chat_repository": ("switchboard.storage.repositories.plan_chat", "default_plan_chat_repository", "call"),
    "publication_repository": ("switchboard.storage.repositories.publication", "default_publication_repository", "call"),
    "projects_repository": ("switchboard.storage.repositories.projects", "default_projects_repository", "call"),
    "project_execution_policy_repository": ("switchboard.storage.repositories.project_execution_policy", "default_project_execution_policy_repository", "call"),
    "kpis_economics_repository": ("switchboard.storage.repositories.kpis_economics", "default_kpis_economics_repository", "call"),
    "review_verdict_repository": ("switchboard.storage.repositories.review_verdicts", "default_review_verdict_repository"),
    "review_remediation_repository": ("switchboard.storage.repositories.review_remediations", "default_review_remediation_repository"),
    "access_repository": ("switchboard.storage.repositories.access", "default_access_repository", "call"),
}


def __getattr__(name: str) -> Any:
    global _index
    if name in _cache:
        return _cache[name]
    if name in _MODULE_EXPORTS:
        value = importlib.import_module(_MODULE_EXPORTS[name])
        _cache[name] = value
        return value
    if name in _EXPORT_ALIASES:
        spec = _EXPORT_ALIASES[name]
        mod = importlib.import_module(spec[0])
        value = getattr(mod, spec[1])
        if len(spec) == 3 and spec[2] == "call":
            value = value()
        _cache[name] = value
        return value
    if _index is None:
        idx: Dict[str, str] = {}
        for path in _EXPORT_MODULES:
            mod = importlib.import_module(path)
            names = getattr(mod, "__all__", None)
            if names is None:
                names = [n for n in dir(mod) if not n.startswith("_")]
            for export_name in names:
                idx[export_name] = path
        _index = idx
    path = _index.get(name)
    if path is None:
        raise AttributeError(f"module 'store' has no attribute {name!r}")
    value = getattr(importlib.import_module(path), name)
    _cache[name] = value
    return value


def __dir__() -> list[str]:
    global _index
    if _index is None:
        idx: Dict[str, str] = {}
        for path in _EXPORT_MODULES:
            mod = importlib.import_module(path)
            names = getattr(mod, "__all__", None)
            if names is None:
                names = [n for n in dir(mod) if not n.startswith("_")]
            for export_name in names:
                idx[export_name] = path
        _index = idx
    return sorted(set(_index) | set(_EXPORT_ALIASES) | set(_MODULE_EXPORTS))
