"""Residual store implementation moved out of ``store.py`` (ARCH-MS-45).

Phase 1 exit requires ``store.py`` to be a thin compatibility façade. This module
owns the remaining persistence/control-plane helpers that have not yet been
split into dedicated repositories. ``store.py`` re-exports these symbols.
"""
import json
import hashlib
import copy
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import evidence_claims
import deliverable_gates
import deliverable_policy
import narration_outbox
import push_verification
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

# Module-level constants + static config live in constants.py (ARCH-2); re-exported
# here so `import store` callers keep seeing store.DEFAULT_PROJECT, store.BUILTIN_PROJECTS, etc.
from constants import *  # noqa: F401,F403
from db.core import *  # noqa: F401,F403 — Layer-0 primitives extracted to db/core.py (ARCH-3)
from db.schema import *  # noqa: F401,F403 — schema/DDL extracted to db/schema.py (ARCH-4)
from db.connection import *  # noqa: F401,F403 — conn/resolve extracted to db/connection.py (ARCH-5)
from rag_store import *        # noqa: F401,F403
from digests_store import *    # noqa: F401,F403
from inbox_store import *      # noqa: F401,F403
from summaries_store import *  # noqa: F401,F403
from decisions_store import *  # noqa: F401,F403
from jobs_store import *       # noqa: F401,F403
from switchboard.storage.repositories.runner import (
    _runner_session_row,
    _upsert_runner_session_in,
    claim_runner_control_request,
    complete_runner_control_request,
    get_runner_session,
    list_runner_control_requests,
    list_runner_sessions,
    request_runner_control,
    upsert_runner_session,
)
from switchboard.storage.repositories.access import *  # noqa: F401,F403 — ARCH-MS-24/30
from switchboard.storage.repositories.tasks import *  # noqa: F401,F403 — ARCH-MS-31
from switchboard.storage.repositories.claims import *  # noqa: F401,F403 — ARCH-MS-32
from switchboard.storage.repositories.coordination import *  # noqa: F401,F403 — ARCH-MS-33
from switchboard.storage.repositories.provenance import *  # noqa: F401,F403 — ARCH-MS-34
from switchboard.storage.repositories.provenance import (  # noqa: F401
    StoreProvenanceRepository,
    default_provenance_repository,
)  # ARCH-MS-27/34
from switchboard.storage.repositories.deliverables import *  # noqa: F401,F403 — ARCH-MS-35
from switchboard.storage.repositories.work_sessions import *  # noqa: F401,F403 — ARCH-MS-46
from switchboard.storage.repositories.work_sessions import (  # noqa: F401
    StoreWorkSessionsRepository,
    default_work_sessions_repository,
)  # ARCH-MS-46
from switchboard.storage.repositories.external_ci import *  # noqa: F401,F403 — ARCH-MS-47
from switchboard.storage.repositories.external_ci import (  # noqa: F401
    StoreExternalCiRepository,
    default_external_ci_repository,
)  # ARCH-MS-47
from switchboard.storage.repositories.external_effects import *  # noqa: F401,F403 — ARCH-MS-54
from switchboard.storage.repositories.external_effects import (  # noqa: F401
    StoreExternalEffectsRepository,
    default_external_effects_repository,
)  # ARCH-MS-54
from switchboard.storage.repositories.activity import *  # noqa: F401,F403 — ARCH-MS-55
from switchboard.storage.repositories.activity import (  # noqa: F401
    StoreActivityRepository,
    default_activity_repository,
)  # ARCH-MS-55
from switchboard.storage.repositories.lifecycle_cleanup import *  # noqa: F401,F403 — ARCH-MS-62
from switchboard.storage.repositories.lifecycle_cleanup import (  # noqa: F401
    StoreLifecycleCleanupRepository,
    default_lifecycle_cleanup_repository,
)  # ARCH-MS-62
from switchboard.storage.repositories.narration import *  # noqa: F401,F403 — ARCH-MS-56
from switchboard.storage.repositories.narration import (  # noqa: F401
    StoreNarrationRepository,
    default_narration_repository,
)  # ARCH-MS-56
from switchboard.storage.repositories.plan_chat import *  # noqa: F401,F403 — ARCH-MS-57
from switchboard.storage.repositories.plan_chat import (  # noqa: F401
    StorePlanChatRepository,
    default_plan_chat_repository,
)  # ARCH-MS-57
from switchboard.storage.repositories.publication import *  # noqa: F401,F403 — ARCH-MS-47
from switchboard.storage.repositories.publication import (  # noqa: F401
    StorePublicationRepository,
    default_publication_repository,
)  # ARCH-MS-47
from switchboard.storage.repositories.projects import *  # noqa: F401,F403 — ARCH-MS-48
from switchboard.storage.repositories.projects import (  # noqa: F401
    StoreProjectsRepository,
    default_projects_repository,
)  # ARCH-MS-48
from switchboard.storage.repositories.kpis_economics import *  # noqa: F401,F403 — ARCH-MS-49
from switchboard.storage.repositories.kpis_economics import (  # noqa: F401
    StoreKpisEconomicsRepository,
    default_kpis_economics_repository,
)  # ARCH-MS-49
from switchboard.storage.repositories.review_verdicts import *  # noqa: F401,F403 — COORD-18
from switchboard.storage.repositories.review_verdicts import (  # noqa: F401
    ReviewVerdictRepository,
    default_review_verdict_repository,
)  # COORD-18
from switchboard.storage.repositories.deliverables import (  # noqa: F401
    CLOSURE_REPORT_HISTORY_LIMIT,
    PROOF_REQUIREMENTS_SCHEMA,
    StoreDeliverablesRepository,
    default_deliverables_repository,
)  # ARCH-MS-35
from switchboard.storage.repositories.coordination import (  # noqa: F401
    StoreCoordinationRepository,
    default_coordination_repository,
)  # ARCH-MS-27/33
from switchboard.storage.repositories.claims import (  # noqa: F401
    StoreClaimsRepository,
    default_claims_repository,
)  # ARCH-MS-27/32
from switchboard.storage.repositories.tasks import (  # noqa: F401
    StoreTaskRepository,
    default_task_repository,
)  # ARCH-MS-27/31
from switchboard.domain.access.identity import (
    binding_for_principal,
    binding_for_registered_agent,
    binding_for_system_actor,
    is_unbound_system_actor,
    shared_token_binding_error,
    validate_system_actor_fields,
    write_binding_activity_payload,
)
from switchboard.domain.provenance.preflight import (  # noqa: F401 — ARCH-MS-58
    _repo_git,
    _repo_git_dir,
    _repo_list_candidate_files,
    _repo_merge_state,
    _repo_parse_status,
    _repo_preflight_finding,
    _repo_remote_slug,
    _repo_scan_conflict_markers,
    _repo_worktree_collisions,
    repo_preflight,
)
from switchboard.domain.provenance.preflight import *  # noqa: F401,F403 — ARCH-MS-58
from switchboard.application.commands.pre_tool_check import (  # noqa: F401 — ARCH-MS-60
    _pre_tool_classify,
    _pre_tool_decision,
    _pre_tool_input,
    _pre_tool_relpath,
    _pre_tool_requested_profile,
    _pre_tool_target_path,
    _record_pre_tool_activity,
    pre_tool_check,
)
from switchboard.application.commands.merge_gate import (  # noqa: F401 — ARCH-MS-61
    _merge_gate_bool,
    _merge_gate_context_passed,
    _merge_gate_context_rows,
    _merge_gate_finding,
    _merge_gate_pr_evidence,
    _merge_gate_pr_number,
    _merge_gate_pr_ref,
    _merge_gate_required_contexts,
    _merge_gate_status_contexts,
    merge_gate,
)
from switchboard.domain.board.tasks import (
    EDITABLE_TASK_FIELDS,
    READY_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    apply_terminal_done_view as _apply_terminal_done_view,
    block_done_without_provenance,
    build_dependency_state,
    dependency_rows_from_lookup,
    is_terminal_done_task as _is_terminal_done_task,
    normalize_depends_on as _normalize_depends_on,
    rationale_state as _rationale_state,
)
from switchboard.domain.coordination.delivery import (
    build_message_delivery_receipt,
    classify_agent_delivery,
    infer_runtime_for_agent,
    runtime_matches_selector,
)
from switchboard.domain.coordination.terminal import (
    TERMINAL_RUNNER_STATUSES,
    TERMINAL_WAKE_STATUSES,
)
from switchboard.domain.deliverables.lifecycle import (
    BREAKDOWN_PROPOSAL_STATUSES,
    DELIVERABLE_ID_RE,
    DELIVERABLE_MILESTONE_STATUSES,
    DELIVERABLE_STATUSES,
    PROJECT_BOARD_ID_RE,
    PROJECT_BOARD_KINDS,
    PROJECT_BOARD_STATUSES,
    normalize_deliverable_id,
    normalize_project_board_id,
    validate_deliverable_status,
)
from switchboard.domain.provenance.git import (
    EVIDENCE_HASH_RE,
    has_done_provenance as _has_done_provenance,
    offline_evidence_from_state as _offline_evidence_from_state,
    provenance_summary as _provenance_summary,
    valid_evidence_hash as _valid_evidence_hash,
)


# Fields a PATCH may change (everything an editor touches in an Asana-style board).
EDITABLE = list(EDITABLE_TASK_FIELDS)
task_repository = default_task_repository()
claims_repository = default_claims_repository()
coordination_repository = default_coordination_repository()
provenance_repository = default_provenance_repository()
deliverables_repository = default_deliverables_repository()
work_sessions_repository = default_work_sessions_repository()
external_ci_repository = default_external_ci_repository()
external_effects_repository = default_external_effects_repository()
activity_repository = default_activity_repository()
lifecycle_cleanup_repository = default_lifecycle_cleanup_repository()
narration_repository = default_narration_repository()
plan_chat_repository = default_plan_chat_repository()
publication_repository = default_publication_repository()
projects_repository = default_projects_repository()
kpis_economics_repository = default_kpis_economics_repository()
review_verdict_repository = default_review_verdict_repository
access_repository = default_access_repository()

from switchboard.domain.bug_intake.policy import (  # noqa: F401 — ARCH-MS-59
    BUG_FAILURE_CLASSES,
    BUG_INTAKE_POLICY,
    BUG_REPORT_REQUIRED_FIELDS,
    BUG_SEVERITIES,
    FAIL_FIX_FAILURE_CLASSES,
    FAIL_FIX_REQUIRED_FIELDS,
    _bug_report_description,
    _bug_report_value_present,
    _bug_title,
    _failure_class_detail,
    bug_intake_policy,
    fail_fix_signal_schema,
)

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).

# ARCH-MS-43: canonical protocol envelope lives in domain/ixp; re-export for
# `import store` callers that still read store.PROTOCOL_ENVELOPE.
from switchboard.domain.ixp.protocol import PROTOCOL_ENVELOPE  # noqa: E402


def _control_plane_timeout_s() -> float:
    return _sqlite_timeout_s("PM_CONTROL_PLANE_SQLITE_TIMEOUT_S", 2.0)


def _control_plane_conn(project: str = DEFAULT_PROJECT):
    return _conn(project, timeout_s=_control_plane_timeout_s())


def _control_plane_unavailable(operation: str, project: str, started_at: float,
                               exc: Exception) -> Dict[str, Any]:
    return {
        "error": "control_plane_unavailable",
        "reason": "sqlite_busy",
        "operation": operation,
        "project": project,
        "elapsed_ms": int((time.time() - started_at) * 1000),
        "timeout_ms": int(_control_plane_timeout_s() * 1000),
        "message": str(exc),
    }


def submit_bug(data: Dict[str, Any], actor: str = "agent",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Thin store façade — orchestration lives in application/commands/submit_bug."""
    from switchboard.application.commands.submit_bug import execute_mapping_result
    return execute_mapping_result(data, actor=actor, project=project)



def control_plane_probe(project: str = DEFAULT_PROJECT, lane: str = "",
                        include_heavy: bool = False) -> Dict[str, Any]:
    """Thin store façade — logic lives in application/queries/control_plane_probe."""
    from switchboard.application.queries.control_plane_probe import execute
    return execute(project=project, lane=lane, include_heavy=include_heavy)


def audit_export(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Thin store façade — logic lives in application/queries/audit_export."""
    from switchboard.application.queries.audit_export import execute
    return execute(project=project)


def get_working_agreement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Thin store façade — logic lives in application/queries/working_agreement."""
    from switchboard.application.queries.working_agreement import execute
    return execute(project=project)


# Re-export audit helpers still reached via store.* (e.g. provenance.reconcile).
from switchboard.application.queries.audit_export import (  # noqa: E402,F401 — ARCH-MS-63
    _AUDIT_REDACT_KEYS,
    _audit_redact,
    _canonical_repo_root,
    _evidence_claim_reports,
)


SEVERITY_VALUE = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _severity_value(severity: str) -> int:
    return SEVERITY_VALUE.get((severity or "").strip().lower(), 0)


# ---- incremental RAG corpus (Phase 5) — ingested artifacts, persisted + shared --------
# ---- Live Inbox queue (Phase 5.5) — triaged inbound artifacts awaiting review ----------
# Hot-read cache (lite board, plan signals, mission status/dependency-graph) extracted to
# read_cache.py per ADR-0006 — it's a self-contained leaf (only runs a builder callback,
# no store dependency). Serve-stale-while-revalidate + the stamp/TTL invalidation contract
# live there. Re-exported so store.ttl_read_cache / store._READ_CACHE keep working for the
# callers below (and signals.py, the perf tests).
from read_cache import _READ_CACHE, ttl_read_cache  # noqa: E402,F401
