"""Working-agreement application query (ARCH-MS-63).

Moved from ``repositories/shell.py``. Connect-time policy fan-in read model.
"""
from __future__ import annotations

from typing import Any, Dict

from constants import DEFAULT_PROJECT
from switchboard.domain.validation_policy import project_validation_policy


__all__ = ["get_working_agreement", "execute", "execute_mapping_result"]


def _store():
    import store
    return store


def execute(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Canonical connect-time rules for agents in this workspace."""
    store = _store()
    override = store.get_meta("working_agreement", {}, project=project) or {}
    access = store.project_access(project)
    repo_topology = store.get_project_repo_topology(project)
    default = {
        "project": project,
        "project_hierarchy": repo_topology.get("project_hierarchy"),
        "project_boundary": access.get("boundary") or f"Only work belonging to project={project} belongs here.",
        "project_purpose": access.get("purpose") or f"{project} work control plane",
        "project_owner": access.get("owner_user_id") or access.get("org_id") or "",
        "repo_topology": repo_topology,
        "repo_role_guide": store.repo_topology_role_guide(project),
        "session_policy_profiles": store.get_session_policy_profiles(project),
        "work_session_contract": store.work_session_contract(project),
        "code_repo_gate": repo_topology.get("code_repo_gate"),
        "protocol": store.protocol_envelope(),
        "validation_policy": project_validation_policy(project),
        "canonical_main_sha": store.get_meta("canonical_main_sha", None, project=project),
        "branch_convention": "claude/<TASK-ID>-<slug>",
        "definition_of_done": "Done means merged/rebased into the intended branch with recorded GitHub/default-branch provenance, or verified non-code work with recorded offline evidence provenance; implemented work with branch/head_sha/PR evidence is In Review.",
        "done_policy": {
            "mode": "git_merge_verified",
            "agent_may_set_done": False,
            "requires_evidence": True,
            "requires_merge_provenance": True,
            "code_tasks_should_include_git_evidence": True,
            "implemented_pr_status": "In Review",
            "done_sources": ["github_pr_merged", "default_branch_backfill", "offline_evidence_verified"],
        },
        "push_before_claiming_progress": True,
        "agent_call_patterns": {
            "writes": (
                "Serialize MCP writes to Switchboard: issue one write at a time. "
                "If SQLite reports 'database is locked', wait 5-15 seconds and retry "
                "the same write; do not start a parallel write burst."
            ),
            "heavy_reads": (
                "Never fan out parallel search_tasks, list_deliverables, or board_summary "
                "calls. Run these heavier reads one at a time."
            ),
            "polling": (
                "Prefer get_lane_delta for polling. Call board_summary at most once per "
                "agent session unless the operator explicitly requests a fresh full snapshot."
            ),
            "diagnostics": (
                "Use control_plane_probe to separate Switchboard server latency from "
                "network, MCP bridge, transfer, or client-side latency."
            ),
        },
        "claim_before_starting": (
            "Before building anything, search_tasks for the feature area and claim (or create) "
            "the board task — this prevents two agents shipping the same work. Fleet PRs on the "
            "canonical repo are checked by the 'Switchboard / claim gate' commit status: a PR that "
            "references no claimed task or Work Session is flagged (SESSION-12)."
        ),
        "merge_strategy": "squash",
        "main_writes": "PR only — never push main directly",
        "github_lifecycle": [
            "push the task branch",
            "open or update the PR against the intended branch",
            "include branch, head_sha, pr_number/pr_url in complete_claim evidence",
            "complete_claim moves the task to In Review and releases the claim",
            "after merge/rebase reaches the intended branch, the GitHub webhook or default-branch backfill stamps merged_sha and marks Done",
            "for non-PR/offline work, a verifier uses the offline-evidence path after In Review to stamp provenance and mark Done",
        ],
        "safe_merge_protocol": {
            "merge_authority": "Agents may merge only when their control registration, task instructions, or the human operator explicitly allow it.",
            "target_branch_rule": "Merge into the intended branch from the task/PR; do not assume master/main if the board or PR says otherwise.",
            "pre_merge": [
                "fetch origin and inspect the current target branch head",
                "rebase or merge the task branch onto the current target branch",
                "resolve conflicts intentionally; never overwrite unrelated user/agent work",
                "rerun the relevant tests/checks after the rebase or conflict resolution",
                "verify git status is clean except for intentional committed changes",
                "push the updated branch and ensure the PR points at the pushed head",
            ],
            "merge": [
                "merge through GitHub or the configured merge queue when available",
                "prefer the repository's configured squash/merge strategy",
                "do not force-merge red checks, missing reviews, or unexpected file changes",
            ],
            "merge_queue": [
                "When a merge queue is active (GitHub says 'the merge strategy for <branch> is set by the merge queue'), the QUEUE — not you — rebases the PR onto the current tip, runs the suite on that merge commit, and squashes. So skip the pre_merge hand-rebase and DO NOT pass --squash or --delete-branch; the queue owns strategy and branch cleanup and will reject those flags.",
                "Enqueue once the PR-head checks are green with a plain `gh pr merge <n>` (no strategy/branch flags) or the `enqueuePullRequest` GraphQL mutation.",
                "PR-head green means 'safe to enqueue', NOT 'landed'. The merge-group check that the queue runs on its own commit is the real landing gate — a stale-tip pass on the PR head is not enough.",
                "Wait on the PR's mergeQueueEntry state (QUEUED -> AWAITING_CHECKS -> MERGED); trust the recorded merged_sha on the target branch as done, never first-green.",
            ],
            "code_review": [
                "A merge_gate 'review_required' finding for code_strict tasks means: no review_verdict has EVER been recorded for the exact current head_sha. It is not, and does not require, an independent reviewer — the SAME agent that wrote the code may record its own passing verdict via record_review_verdict / the review_verdict MCP tool.",
                "Do not wait for or seek out a different reviewing agent/session before self-certifying — most fleet agents share one authenticated principal, so 'find an independent reviewer' routes to a request that will time out unacknowledged. Record your own verdict once you have actually reviewed the diff.",
                "The only hard requirements are an authenticated principal on the verdict (reviewer_principal_unbound) and that the reviewer_principal is not spoofed independent of the authenticated actor (reviewer_principal_mismatch) — not who that principal is relative to the implementer.",
                "If merge_gate reports review_stalled_no_verdict (no verdict recorded past the stall window), that is the system telling you to record one now, not a signal that a separate reviewer will materialize.",
            ],
            "post_merge": [
                "fetch/pull the target branch after merge",
                "record the resulting merged_sha or target branch head in evidence",
                "verify the task's changed files/content are present on the intended branch",
                "let the GitHub webhook or default-branch provenance path mark Done",
                "if the webhook is unavailable, run or request reconcile/backfill rather than setting Done manually",
            ],
        },
        "fail_fix_early_policy": {
            "summary": "Surface real failures immediately and repair them before they spread.",
            "schema": store.fail_fix_signal_schema(),
            "surface_immediately": [
                "missing data",
                "broken connections",
                "invalid inputs",
                "stale branches",
                "absent permissions",
                "malformed payloads",
                "failed checks",
            ],
            "do_not_hide_with": [
                "placeholder values",
                "silent defaults",
                "optimistic status updates",
                "fallbacks that make the workflow look green",
            ],
            "fallback_rule": (
                "Fallbacks are allowed only when they are visible, named, and preserve the "
                "original failing signal with an auditable red/yellow status, monitor event, "
                "reconcile finding, task comment, or blocker."
            ),
            "agent_rule": (
                "When a gate uncovers an environment, ingestion, normalization, protocol, "
                "auth, or workflow problem, treat the discovered problem as part of the task "
                "until it is repaired or deliberately handed off."
            ),
            "bug_reporting": (
                "If the failure is product-level or repeated, file it through submit_bug with "
                "one of the fail_fix_signal.v1 failure_class values and complete evidence."
            ),
        },
        "bug_intake_policy": store.bug_intake_policy(),
        "ports_doc": "docs/PORTS.md",
        "byo_data": True,
        "session_start_sequence": [
            "get_working_agreement(project)",
            "register_agent",
            "inbox(unacked)",
            "check+claim before first write",
        ],
        "deliverable_first_startup": {
            "doc": "docs/DELIVERABLE-FIRST-STARTUP.md",
            "ownership": {
                "projects": "repo/trust/policy/access/CI/model/budget/Done authority",
                "boards_missions": "live outcome cockpits; boards own execution routing",
                "deliverables": "shipped-value definition, end_state, milestones, cross-board proof rollup",
                "tasks": "execution units on exactly one project workstream",
            },
            "mission_home_project": (
                "The project database that owns the deliverable record. Pass this as project= "
                "on mission tools even when linked tasks live on other projects."
            ),
            "boot_sequence": [
                "prepare_agent_session(project=<mission_home>, deliverable_id=... | board_id=... | mission_id=...)",
                "get_mission_status(project=<mission_home>, deliverable_id=...)",
                "Read end_state, acceptance_criteria, policy_constraints, milestones, linked_tasks, blockers, next_actions",
                "Workers: claim_next(agent_id, project=<mission_home>, deliverable_id=..., milestone_id=...)",
                "Workers: complete_claim(..., project=<task_project>, evidence={mission_project, deliverable_id, milestone_id, branch, head_sha, pr_url})",
            ],
            "coordinator_sequence": [
                "get_mission_status",
                "run_mission_coordinator(deliverable_id=..., coordinator_agent_id=..., auto_start=true)",
                "Follow next_actions (approve_breakdown, claim_task, verify_merge_provenance)",
                "claim_next(deliverable_id=...) or approve_deliverable_breakdown",
                "update_mission_narrative when material state changes",
            ],
        },
        "session_start_sequence_deliverable": [
            "prepare_agent_session(project, deliverable_id|board_id|mission_id)",
            "get_mission_status",
            "register_agent",
            "inbox(unacked)",
            "claim_next(deliverable_id=...) or claim_task on an explicit linked task",
        ],
        "agent_completion_rule": "complete_claim(evidence=...) records branch/head_sha/PR/offline evidence and moves to In Review; agents cannot mark Done. Done is reserved for GitHub/default-branch merge provenance or verifier-stamped offline evidence.",
    }
    agreement = {**default, **override, "project": project}
    if "done_policy" not in override:
        agreement["done_policy"] = default["done_policy"]
        agreement["definition_of_done"] = default["definition_of_done"]
        agreement["agent_completion_rule"] = default["agent_completion_rule"]
    return agreement


def execute_mapping_result(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    return execute(project=project)


get_working_agreement = execute
