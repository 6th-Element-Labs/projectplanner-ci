"""Module-level constants and static configuration for the store layer.

Extracted verbatim from store.py (ARCH-2). Pure data only — compiled regex, path/env
vars, the built-in project + repo-topology registries, role scopes, session-policy
profiles, and preflight class sets. No logic, no DB access, no imports beyond os/re.
store.py re-exports these via `from constants import *` so every caller is unchanged.
"""
import os
import re

DB_PATH = os.environ.get("PM_DB_PATH", os.path.join(os.path.dirname(__file__), "taikun_pm.db"))
SEED_PATH = os.environ.get("PM_SEED_PATH", os.path.join(os.path.dirname(__file__), "seed_plan.json"))
HELM_DB_PATH = os.environ.get("PM_HELM_DB_PATH", os.path.join(os.path.dirname(__file__), "helm.db"))
HELM_SEED_PATH = os.environ.get("PM_HELM_SEED_PATH",
                                os.path.join(os.path.dirname(__file__), "seeds", "helm_seed_plan.json"))
SWITCHBOARD_DB_PATH = os.environ.get("PM_SWITCHBOARD_DB_PATH",
                                     os.path.join(os.path.dirname(__file__), "switchboard.db"))
SWITCHBOARD_SEED_PATH = os.environ.get("PM_SWITCHBOARD_SEED_PATH",
                                       os.path.join(os.path.dirname(__file__), "seeds",
                                                    "switchboard_seed_plan.json"))
PROJECT_REGISTRY_DB_PATH = os.environ.get(
    "PM_PROJECT_REGISTRY_DB_PATH",
    os.path.join(os.path.dirname(DB_PATH), "project_registry.db"),
)
PROJECT_ID_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
PROJECT_ID_VALID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
GITHUB_PR_SHORTHAND_RE = re.compile(r"\bPR\s*#?\s*(\d+)\b", re.I)
BRANCH_EVIDENCE_RE = re.compile(r"\bbranch(?:\s+was)?[:\s]+`?([A-Za-z0-9._/-]+)`?", re.I)
HEAD_EVIDENCE_RE = re.compile(r"\bhead(?:_sha|\s+sha)?[:\s]+`?([0-9a-f]{7,40})`?", re.I)

# Multi-project registry. Each project is its OWN sqlite file — physical isolation, so a Helm
# request can never read or write Maxwell's rows (no shared table, no project_id column). The
# default is always 'maxwell', so every existing caller behaves exactly as before.
BUILTIN_PROJECTS = {
    "maxwell": {"db": DB_PATH, "seed": SEED_PATH,
                "label": "Project Maxwell", "pretitle": "TEEP Barnett · TotalEnergies E&P"},
    "helm": {"db": HELM_DB_PATH, "seed": HELM_SEED_PATH,
             "label": "Helm — Marine Nav Companion", "pretitle": "6th Element Labs · web-first chartplotter"},
    "switchboard": {"db": SWITCHBOARD_DB_PATH, "seed": SWITCHBOARD_SEED_PATH,
                    "label": "Switchboard — Agent Coordination Layer",
                    "pretitle": "6th Element Labs · live dogfood control plane"},
}
BUILTIN_GITHUB_REPOS = {
    "helm": "StevenRidder/Helm",
    "switchboard": "6th-Element-Labs/projectplanner",
}
DEFAULT_PUBLIC_CI_REPO = (os.environ.get("PM_PUBLIC_CI_REPO") or "").strip()
REPO_TOPOLOGY_SCHEMA = "switchboard.project_repo_topology.v1"
CLAIM_GATE_MODES = frozenset({"off", "warn", "enforce"})
DEFAULT_CLAIM_GATE_MODE = "warn"
BUILTIN_REPO_TOPOLOGIES = {
    "helm": {
        "schema": REPO_TOPOLOGY_SCHEMA,
        "topology_type": "private_canonical_public_mirror_public_ci",
        "roles": {
            "canonical": {
                "repo": "StevenRidder/Helm",
                "default_branch": "main",
                "claim_gate": DEFAULT_CLAIM_GATE_MODE,
                "authority": ["done", "merge_provenance", "code_truth"],
                "description": "Private canonical Helm repo. Only this role can satisfy code Done.",
            },
            "public_ci": {
                "repo": DEFAULT_PUBLIC_CI_REPO or "StevenRidder/helm-ci",
                "repo_placeholder": "<public-CI>",
                "default_branch": "main",
                "authority": ["verification_only"],
                "required_status_contexts": ["helm-ci/full-suite"],
                "sync_scripts": ["scripts/ci-sandbox.sh"],
                "shared": True,
                "description": "Shared public CI sandbox. Verifies the canonical SHA but is not code truth.",
            },
            "public": {
                "repo": "",
                "repo_placeholder": "<public mirror>",
                "default_branch": "main",
                "authority": ["publish_evidence_only"],
                "publish_scripts": ["scripts/publish-public-mirror.sh"],
                "description": "Public source mirror. Publication evidence only; never code Done.",
            },
        },
    },
    "switchboard": {
        "schema": REPO_TOPOLOGY_SCHEMA,
        "topology_type": "private_canonical_public_mirror_public_ci",
        "roles": {
            "canonical": {
                "repo": "6th-Element-Labs/projectplanner",
                "default_branch": "master",
                "claim_gate": DEFAULT_CLAIM_GATE_MODE,
                "authority": ["done", "merge_provenance", "code_truth"],
                "description": "Canonical Switchboard/projectplanner repo.",
            },
            "public_ci": {
                "repo": DEFAULT_PUBLIC_CI_REPO,
                "repo_placeholder": "<public-CI>",
                "default_branch": "main",
                "authority": ["verification_only"],
                "required_status_contexts": [],
                "sync_scripts": [],
                "shared": True,
                "description": "Shared public CI sandbox. Verifies canonical SHAs but is not code truth.",
            },
            "public": {
                "repo": "",
                "repo_placeholder": "<switchboard public mirror>",
                "default_branch": "main",
                "authority": ["publish_evidence_only"],
                "publish_scripts": [],
                "description": "Public Switchboard mirror. Publication evidence only; never code Done.",
            },
        },
    },
}
# Back-compat for older call sites that only need the built-in project set. New code should call
# project_ids(), has_project(), projects(), or _resolve() so dynamic projects are included.
PROJECTS = BUILTIN_PROJECTS
DEFAULT_PROJECT = "maxwell"
TASK_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")
DEFAULT_ORG_ID = "org-6th-element-labs"
ROLE_SCOPES = {
    "viewer": ["read"],
    "commenter": ["read", "write:comments"],
    # write:projects lets contributors create their own projects (private by default; ACCESS-14).
    "contributor": ["read", "write:tasks", "write:ixp", "write:bug_intake", "write:projects"],
    "operator": ["read", "read:credentials", "write:tasks", "write:ixp", "write:bug_intake", "write:projects", "write:credentials", "use:credentials"],
    "admin": ["read", "read:credentials", "write:tasks", "write:ixp", "write:system", "write:bug_intake", "write:projects", "write:credentials", "use:credentials", "admin"],
    "owner": ["read", "read:credentials", "write:tasks", "write:ixp", "write:system", "write:bug_intake", "write:projects", "write:credentials", "use:credentials", "admin"],
}
# The deployment MCP credential and the task-bound direct CLI credential are two
# transports for the same autonomous operator. Keep their authority identical;
# task/project/agent binding is enforced separately from scopes.
MCP_OPERATOR_SCOPES = tuple(ROLE_SCOPES["owner"])
VALID_PRINCIPAL_KINDS = {"human", "user", "agent", "host", "system"}
# Agent Host bearers use a transport-specific write scope so a compromised
# personal host cannot invoke generic IXP task, wake-request, or fleet-control
# mutations. Operators retain their broader role scopes and are admitted by the
# route-level compatibility gate.
VALID_PRINCIPAL_SCOPES = sorted(
    {s for scopes in ROLE_SCOPES.values() for s in scopes} | {"write:agent_host"}
)
WORK_SESSION_SCHEMA = "switchboard.work_session.v1"
MANAGED_WORK_SESSION_SCHEMA = "switchboard.managed_work_session.v1"
WORK_SESSION_HEALTH_SCHEMA = "switchboard.session_health.v1"
TASK_SESSION_HEALTH_SCHEMA = "switchboard.task_session_health.v1"
EXECUTED_TEST_RUN_SCHEMA = "switchboard.executed_test_run.v1"
SESSION_POLICY_PROFILE_SCHEMA = "switchboard.session_policy_profiles.v1"
WORK_SESSION_STATUSES = {"proposed", "active", "blocked", "completed", "archived", "expired"}
WORK_SESSION_STORAGE_MODES = {"worktree", "clone", "external"}
WORK_SESSION_DIRTY_STATUSES = {"clean", "dirty", "unknown"}
WORK_SESSION_REQUIRED_PATH_MODES = {"worktree", "clone"}
SESSION_POLICY_PROFILE_ALIASES = {
    "strict": "code_strict",
    "code": "code_strict",
    "docs": "docs_review",
    "documentation": "docs_review",
    "offline": "offline_evidence",
    "non_code_offline": "offline_evidence",
    # ADR-0006 collapsed the enforcement profiles to the three with distinct behaviour
    # (code_strict / docs_review default / offline_evidence). The old ui_preview and
    # no_repo profiles carried no enforcement beyond the default, so their aliases now
    # resolve to the docs_review default.
    "preview": "docs_review",
    "ui_preview": "docs_review",
    "none": "docs_review",
    "no-repo": "docs_review",
    "no_repo": "docs_review",
}
BUILTIN_SESSION_POLICY_PROFILES = {
    "code_strict": {
        "profile": "code_strict",
        "label": "Code strict",
        "description": "For code/repo work that can move product truth. Requires a bound clean Work Session and canonical merge provenance.",
        "work_session_required": True,
        "pre_tool_missing_session": "deny",
        "allowed_storage_modes": ["worktree", "clone"],
        "preferred_storage_mode": "worktree",
        "prefer_full_clone": False,
        "deny_hygiene": [
            "dirty_work_session",
            "conflict_markers",
            "wrong_branch",
            "missing_upstream",
            "missing_base_sha",
        ],
        "warn_hygiene": [],
        "requires_branch_task_scope": True,
        "requires_upstream": True,
        "requires_base_sha": True,
        "requires_tests": True,
        "requires_executed_tests": True,
        "requires_diff_check": True,
        "merge_requires_work_session": True,
        "merge_authority": "canonical_repo_only",
        "completion_evidence": ["branch", "head_sha", "pr_url_or_remote_ref", "executed_test_run", "git_diff_check"],
    },
    "docs_review": {
        "profile": "docs_review",
        "label": "Docs review",
        "description": "For docs, planning, and review work where a Work Session is useful but not mandatory.",
        "work_session_required": False,
        "pre_tool_missing_session": "warn",
        "allowed_storage_modes": ["external", "worktree", "clone"],
        "preferred_storage_mode": "external",
        "prefer_full_clone": False,
        "deny_hygiene": ["conflict_markers"],
        "warn_hygiene": ["dirty_work_session", "missing_upstream", "missing_base_sha"],
        "requires_branch_task_scope": False,
        "requires_upstream": False,
        "requires_base_sha": False,
        "requires_tests": False,
        "requires_executed_tests": False,
        "requires_diff_check": False,
        "merge_requires_work_session": False,
        "merge_authority": "canonical_repo_only_when_code_changes",
        "completion_evidence": ["artifact_or_review_note"],
    },
    "offline_evidence": {
        "profile": "offline_evidence",
        "label": "Offline evidence",
        "description": "For non-PR/offline deliverables that need explicit verifier evidence instead of repo merge provenance.",
        "work_session_required": False,
        "pre_tool_missing_session": "allow",
        "allowed_storage_modes": ["external"],
        "preferred_storage_mode": "external",
        "prefer_full_clone": False,
        "deny_hygiene": [],
        "warn_hygiene": [],
        "requires_branch_task_scope": False,
        "requires_upstream": False,
        "requires_base_sha": False,
        "requires_tests": False,
        "requires_executed_tests": False,
        "requires_diff_check": False,
        "merge_requires_work_session": False,
        "merge_authority": "offline_verifier",
        "completion_evidence": ["offline_evidence", "artifact_url_or_verification"],
    },
}
# ADR-0006: collapsed from five to the three profiles with genuinely distinct behaviour —
# code_strict (enforced Work Session + tests), docs_review (the non-enforcing default), and
# offline_evidence (non-PR work that needs explicit verifier evidence at completion). The
# retired ui_preview / no_repo profiles added no enforcement beyond the default; their names
# alias to docs_review (see SESSION_POLICY_PROFILE_ALIASES) so existing task tags still resolve.
WORK_SESSION_STRICT_PROFILES = {
    name for name, profile in BUILTIN_SESSION_POLICY_PROFILES.items()
    if profile.get("work_session_required")
}
REPO_PREFLIGHT_SCHEMA = "switchboard.repo_preflight.v1"
PREFLIGHT_RUN_SCHEMA = "switchboard.preflight_run.v1"
PREFLIGHT_FINDING_SCHEMA = "switchboard.preflight_finding.v1"
PREFLIGHT_CALIBRATION_SCHEMA = "switchboard.preflight_calibration.v1"
PRE_TOOL_CHECK_SCHEMA = "switchboard.pre_tool_check.v1"
MERGE_GATE_SCHEMA = "switchboard.merge_gate.v1"
VERIFY_CI_SCHEMA = "switchboard.verify_ci.v1"
REPO_PREFLIGHT_VERDICTS = {"pass", "warn", "deny"}
REPO_PREFLIGHT_DENY_CLASSES = {
    "dirty_worktree",
    "conflict_markers",
    "wrong_repo",
    "wrong_branch",
    "stale_base",
    "shared_worktree_collision",
    "detached_head",
    "merge_or_rebase_in_progress",
    "co_change_contract",
}
REPO_PREFLIGHT_WARN_CLASSES = {
    "missing_upstream",
    "missing_base_ref",
    "git_signal_unavailable",
}


# --- seed config (moved from store.py in ARCH-4) ---
META_SECTIONS = ["project", "generated", "schedule_start", "schedule_note", "owner_orgs",
                 "rollups", "executive_summary", "timeline_note", "critical_path",
                 "milestones", "consolidated_risks", "consolidated_decisions", "people",
                 "working_agreement"]

# A sensible default people list for the assignee picker (the real names in the plan).
DEFAULT_PEOPLE = ["Steve Ridder", "Taikun eng", "Darko", "Sahir", "Sebastian", "Mike",
                  "Michelle", "Sierra", "Clovis", "Devin", "Brent", "IFS owner", "Nubo"]
