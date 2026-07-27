"""The board-evidence axis of ``scenario.v1`` (COORD-78).

Blind spot #1 from the 2026-07-26 COORD-57 incident. The scenario vocabulary
was PR-world only — draft, CI, mergeability, review, queue, runner — and T1
hardcoded ``merge_gate={"findings": []}``. So the entire *evidence* family was
unrepresentable: a missing ``executed_test_run``, a near-miss key one letter
away, a green external-CI receipt on the exact head, a borrowed Work Session.
The last three completion incidents (COORD-57, COORD-61, CO-21) were all
evidence-family, and none of them could be written down as a T1 scenario.

This module adds ``world.evidence`` and — the part that makes it proof rather
than decoration — feeds it by running the **real**
``switchboard.application.commands.merge_gate``. Its ACTUAL findings go into
``build_completion_snapshot``. Findings are never hand-built here.

That rule is not stylistic. ``_merge_gate_finding`` SPLATS its ``details``
argument onto the finding, so ``finding["details"]`` does not exist — and two
different authors independently wrote consumers that read ``finding["details"]``,
shipped them green, and shipped them dead, because their tests hand-built the
nested shape the gate never emits (COORD-61's artifact lift and BUG-182's
conflict decomposer, both on 2026-07-25). A conformance harness that hand-builds
findings would make that class of defect permanently invisible. Run the gate.

Only DB and GitHub seams are stubbed, and only at ``merge_gate``'s own
indirection layer (``merge_gate._store_facade`` delegates — the module already
declares that boundary). Everything that produces a finding stays real:

  * ``claims._executed_test_run_gate``           (pure; near-miss report included)
  * ``external_ci._external_ci_review_gate``     (real, fed a real
    ``_external_ci_summary`` — the ENFORCE-16 pattern)
  * ``review_verdicts.review_merge_gate_findings`` (real, over an in-memory DB
    carrying the real ``db.schema`` schema)
  * ``projects.get_project_repo_topology`` / ``_session_policy_profile_rules``
    (real, with ``get_meta`` returning its default so no DB is opened)
  * ``work_sessions.pr_backed_by_process`` / ``_task_work_session_profile``
    (real; the fixture inputs keep them off their DB branches)

Hermetic: no file DB, no network, no ``gh``, no clock dependence.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
import sqlite3
from typing import Any, Iterator
from unittest.mock import patch

from db.core import _coerce_str_list
from db.schema import apply_schema
from switchboard.application.commands import merge_gate as merge_gate_mod
from switchboard.storage.repositories import projects as projects_repo
from switchboard.storage.repositories import review_verdicts as review_repo
from switchboard.storage.repositories import work_sessions as work_sessions_repo
from switchboard.storage.repositories.claims import (
    EXECUTED_TEST_RUN_SCHEMA,
    _executed_test_run_gate,
)
from switchboard.storage.repositories.external_ci import (
    _external_ci_review_gate,
    _external_ci_summary,
)
from switchboard.storage.repositories.provenance import (
    _github_repo_from_pr_url,
    _parse_evidence,
)


#: One evidence world per axis value. Each name is a state the last three
#: completion incidents actually reached, not a hypothetical.
EVIDENCE_AXES: dict[str, tuple[str, ...]] = {
    # COORD-57: the gate demanded a fact it had already recorded.
    # CO-21: the fact was present under `executed_tests`, one key away.
    "executed_test_run": ("valid", "missing", "near_miss_key", "failed", "stale_head"),
    # ENFORCE-16 / PR #955: a green run on the EXACT head derives the
    # executed-test verdict; any other head, a red run, or no run does not.
    "external_ci": (
        "green_exact_head", "green_other_head", "failed", "pending", "none",
    ),
    # BUG-176: a co-resolved task legitimately borrows the session that
    # produced the head being gated.
    "work_session": ("present", "missing", "borrowed"),
    # COORD-18 exact-head review gate, as merge_gate consumes it.
    "review_verdict": ("pass", "missing", "stale", "open_findings", "not_passed"),
}

#: ``world.evidence`` is optional. When a scenario omits it the effective
#: evidence world is derived so it AGREES with the PR world rather than
#: contradicting it: a scenario whose CI is red has no green CI receipt.
#: This is what lets the 27 pre-COORD-78 scenarios keep their expectations
#: while still being graded against real gate output instead of ``[]``.
#: Every value of ``world.ci`` must appear here. Deliberately exhaustive rather
#: than defaulted: a new CI conclusion added to the PR world should fail loudly
#: with a KeyError here, not silently acquire an evidence world nobody chose.
_EXTERNAL_CI_FOR_CI = {
    "pass": "green_exact_head",
    "fail": "failed",
    "error": "failed",
    "pending": "pending",
    "missing": "none",
    # COORD-76's extended GitHub conclusions. None of them is a passing run, so
    # none of them can produce a receipt the executed-test gate may derive from.
    "timed_out": "failed",
    "action_required": "failed",
    "cancelled": "failed",
    "stale": "failed",
    "startup_failure": "failed",
}

OTHER_HEAD = "d" * 40
BRANCH = "conformance/scenario"
WORK_SESSION_ID = "worksession-conformance-evidence"
BORROWED_FROM_TASK = "COORD-64"
BORROWED_SESSION_ID = "worksession-conformance-borrowed"
REVIEWER_PRINCIPAL = "conformance-reviewer"
CANONICAL_REPO = "6th-Element-Labs/projectplanner"


def default_evidence(world: dict[str, Any]) -> dict[str, str]:
    """The evidence world a scenario that declares none is asserting."""
    return {
        "executed_test_run": "valid",
        "external_ci": _EXTERNAL_CI_FOR_CI[str(world.get("ci"))],
        "work_session": "present",
        "review_verdict": "pass",
    }


def effective_evidence(world: dict[str, Any]) -> dict[str, str]:
    """Resolve ``world.evidence``, applying the derived default when absent."""
    declared = world.get("evidence")
    if isinstance(declared, dict) and declared:
        return dict(declared)
    return default_evidence(world)


def validate_evidence(value: Any, source: Any, require: Any) -> None:
    """Validate an explicitly declared ``world.evidence`` block."""
    if value is None:
        return
    require(isinstance(value, dict), f"{source}: world.evidence must be an object")
    require(
        set(value) == set(EVIDENCE_AXES),
        f"{source}: world.evidence must declare exactly {sorted(EVIDENCE_AXES)}",
    )
    for axis, allowed in EVIDENCE_AXES.items():
        require(
            value.get(axis) in allowed,
            f"{source}: invalid world.evidence.{axis} {value.get(axis)!r}",
        )


# ---------------------------------------------------------------------------
# Executed-test-run evidence (the payload the gate reads).
# ---------------------------------------------------------------------------

def _valid_run(head_sha: str, work_session_id: str) -> dict[str, Any]:
    return {
        "schema": EXECUTED_TEST_RUN_SCHEMA,
        "run_id": "conformance-test-run",
        "commands": ["bash scripts/switchboard_ci.sh"],
        "passed": True,
        "exit_code": 0,
        "output_sha256": "ab" * 32,
        "completed_at": "2026-07-26T22:00:00Z",
        "work_session_id": work_session_id,
        "branch": BRANCH,
        "head_sha": head_sha,
    }


def _run_session_id(work_session_state: str) -> str:
    """Which session the recorded run belongs to.

    On a borrowed session the run was produced in the OTHER co-resolved task's
    session — that is the whole shape of BUG-176. Pinning the run to the
    task's own session id there would make the gate refuse with
    ``wrong_test_work_session`` and turn "borrowed session accepted" into a
    test of something else.
    """
    if work_session_state == "borrowed":
        return BORROWED_SESSION_ID
    return WORK_SESSION_ID


def _executed_test_evidence(state: str, head_sha: str,
                            work_session_state: str) -> dict[str, Any]:
    """Evidence keys for one ``executed_test_run`` axis value.

    ``near_miss_key`` is deliberately NOT placed here: CO-21 recorded its five
    real suites in the Work Session's hygiene blob, and the near-miss report
    names the holding session, so it belongs on the session (see
    :func:`_work_session_rows`).
    """
    session_id = _run_session_id(work_session_state)
    if state == "valid":
        return {"executed_test_run": _valid_run(head_sha, session_id)}
    if state == "failed":
        run = _valid_run(head_sha, session_id)
        run.update({"passed": False, "exit_code": 1, "status": "failure"})
        return {"executed_test_run": run}
    if state == "stale_head":
        run = _valid_run(head_sha, session_id)
        run["head_sha"] = OTHER_HEAD
        return {"executed_test_run": run}
    return {}


# ---------------------------------------------------------------------------
# External CI receipts (rows -> the real summary aggregator).
# ---------------------------------------------------------------------------

_CI_CONTRACT = {
    "source_repo": CANONICAL_REPO,
    "ci_repo": "6th-Element-Labs/projectplanner-ci",
}


def _external_ci_rows(state: str, head_sha: str,
                      status_context: str) -> tuple[list[dict[str, Any]], str]:
    """Real ``external_ci_runs`` row shapes for one axis value, plus source_sha."""
    if state == "none":
        return [], head_sha
    sha = head_sha if state != "green_other_head" else OTHER_HEAD
    status, conclusion = {
        "green_exact_head": ("success", "success"),
        "green_other_head": ("success", "success"),
        "failed": ("failure", "failure"),
        "pending": ("running", ""),
    }[state]
    row = {
        "run_id": f"ecir-{sha[:8]}-{status}",
        "status": status,
        "conclusion": conclusion,
        "source_sha": sha,
        "source_repo": _CI_CONTRACT["source_repo"],
        "ci_repo": _CI_CONTRACT["ci_repo"],
        "status_context": status_context,
        "run_url": (
            "https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/1"
        ),
        "result": {},
    }
    # A run on another head must be visible to the aggregator, so it cannot be
    # filtered out by source_sha before the gate gets to refuse it.
    return [row], (head_sha if state != "green_other_head" else "")


def _external_ci_gate(state: str, head_sha: str, status_context: str):
    """Bind the real review gate to a real summary over fixture rows.

    ``_external_ci_review_gate`` takes an explicit ``summary=`` override, so the
    only thing replaced is the database read. The ``required``/``gate``
    projection an agent actually sees is produced by production code.
    """
    rows, source_sha = _external_ci_rows(state, head_sha, status_context)
    summary = _external_ci_summary(
        rows, source_sha=source_sha, contract=dict(_CI_CONTRACT))

    def gate(task, evidence=None, project="switchboard", **_kwargs):
        return _external_ci_review_gate(
            task, evidence=evidence, project=project, summary=summary)

    return gate


# ---------------------------------------------------------------------------
# Work Session rows (the list_work_sessions DB seam).
# ---------------------------------------------------------------------------

def _session_row(session_id: str, task_id: str, head_sha: str,
                 hygiene_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    hygiene: dict[str, Any] = {
        "repo_preflight": {
            "verdict": "pass",
            "ok": True,
            "findings": [],
            "changed_files": [],
        },
    }
    hygiene.update(hygiene_extra or {})
    return {
        "work_session_id": session_id,
        "task_id": task_id,
        "project_id": "switchboard",
        "repo": CANONICAL_REPO,
        "repo_role": "canonical",
        "storage_mode": "worktree",
        "branch": BRANCH,
        "head_sha": head_sha,
        "base_sha": OTHER_HEAD,
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
        "hygiene": hygiene,
    }


def _work_session_rows(state: str, executed_test_state: str, task_id: str,
                       head_sha: str) -> dict[str, list[dict[str, Any]]]:
    """The rows each ``list_work_sessions`` lookup shape should return.

    merge_gate makes two distinct lookups — task-scoped, then branch-scoped —
    and the branch-scoped one exists precisely so a co-resolved task can borrow
    the session that produced the gated head (BUG-176). Keying the stub on the
    lookup shape is what makes ``borrowed`` a real state rather than a rename of
    ``present``.
    """
    hygiene_extra: dict[str, Any] = {}
    if executed_test_state == "near_miss_key":
        # CO-21, verbatim in shape: five executed suites recorded under
        # `executed_tests`, a key the contract does not accept.
        hygiene_extra["executed_tests"] = [
            {"command": f"python tests/test_suite_{index}.py", "passed": True}
            for index in range(1, 6)
        ]
    if state == "missing":
        return {"task": [], "branch": []}
    if state == "borrowed":
        return {
            "task": [],
            "branch": [_session_row(
                BORROWED_SESSION_ID, BORROWED_FROM_TASK, head_sha,
                hygiene_extra)],
        }
    return {
        "task": [_session_row(WORK_SESSION_ID, task_id, head_sha, hygiene_extra)],
        "branch": [_session_row(
            WORK_SESSION_ID, task_id, head_sha, hygiene_extra)],
    }


def _list_work_sessions_stub(rows: dict[str, list[dict[str, Any]]]):
    def list_work_sessions(project, **kwargs):
        del project
        if str(kwargs.get("task_id") or "").strip():
            return copy.deepcopy(rows["task"])
        if str(kwargs.get("branch") or "").strip():
            return copy.deepcopy(rows["branch"])
        return copy.deepcopy(rows["branch"])

    return list_work_sessions


# ---------------------------------------------------------------------------
# Review verdicts (the real gate over an in-memory copy of the real schema).
# ---------------------------------------------------------------------------

_REVIEW_DB: sqlite3.Connection | None = None


def _review_db() -> sqlite3.Connection:
    """One in-memory database carrying the REAL ``db.schema`` schema.

    Built with ``apply_schema`` rather than hand-written DDL so the review gate
    reads the same columns production does; ``_gold_shared`` already
    established connection-level patching as the hermetic seam for a repository
    module.
    """
    global _REVIEW_DB
    if _REVIEW_DB is None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        apply_schema(db)
        _REVIEW_DB = db
    return _REVIEW_DB


def _seed_review(db: sqlite3.Connection, state: str, task_id: str,
                 head_sha: str, pr_url: str, pr_number: int) -> None:
    """Seed exactly one review world.

    ``pushed_at`` stays NULL on purpose: it is the input to the review *stall*
    escalation, and a conformance scenario must not depend on wall-clock drift
    between the seed and the gate.
    """
    now = 1_785_000_000.0
    db.execute("DELETE FROM review_findings WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM review_verdicts WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM task_git_state WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    db.execute(
        "INSERT INTO tasks(task_id, workstream_id, title, status, sort_order, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (task_id, "COORD", "conformance evidence scenario", "In Review", 1,
         now, now),
    )
    # `stale` is the one world where the board's current head is NOT the head
    # being gated — that is the definition of a stale verdict.
    current_head = OTHER_HEAD if state == "stale" else head_sha
    db.execute(
        "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, "
        "pr_number, pr_url, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, BRANCH, current_head, None, pr_number, pr_url, "{}", now),
    )
    if state == "missing":
        db.commit()
        return
    verdict_status = "changes_requested" if state == "not_passed" else "pass"
    # Task-scoped: the reseed above deletes by task_id, and more than one task
    # id runs through this database (T1 and the convergence tier use different
    # ones), so a task-independent verdict_id collides on the UNIQUE key.
    verdict_id = f"reviewverdict-conformance-{task_id.lower()}-{state}"
    db.execute(
        "INSERT INTO review_verdicts(verdict_id, task_id, pr_url, head_sha, "
        "reviewer_principal, reviewer_principal_id, review_mode, status, "
        "source, created_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (verdict_id, task_id, pr_url, current_head, REVIEWER_PRINCIPAL,
         REVIEWER_PRINCIPAL, "agent_self_review", verdict_status,
         "review_command", now, now),
    )
    if state == "open_findings":
        db.execute(
            "INSERT INTO review_findings(verdict_id, task_id, finding_id, "
            "location, category, severity, invariant_violated, "
            "repair_requirement, finding_class, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (verdict_id, task_id, "CONFORMANCE-1",
             "src/switchboard/domain/completion/state_machine.py:1",
             "correctness", "high", "an open finding blocks merge",
             "resolve the finding", "automatic", "open", now, now),
        )
    db.commit()


# ---------------------------------------------------------------------------
# The gate call.
# ---------------------------------------------------------------------------

def _meta_default(_key: Any, default: Any = None, **_kwargs: Any) -> Any:
    """``get_meta`` with the database removed: every project override absent."""
    return default


@contextmanager
def _gate_seams(scenario_id: str, evidence: dict[str, str], *, task_id: str,
                head_sha: str, pr_url: str, pr_number: int,
                status_context: str, session_rows: dict[str, Any],
                db: sqlite3.Connection) -> Iterator[None]:
    """Patch every DB/GitHub seam ``merge_gate`` reaches — and nothing else.

    ``merge_gate`` resolves each of these through a one-line module-level
    delegate to the ``store`` façade (``merge_gate._store_facade``), which is
    the module's own declaration of where its I/O boundary is. Patching that
    layer is what "only DB/GitHub seams stubbed" means here; the gate body,
    every sub-gate that emits a finding, and the finding shapes themselves are
    untouched production code.
    """
    task_row = {
        "task_id": task_id,
        "workstream_id": "COORD",
        "title": f"conformance scenario {scenario_id}",
        "status": "In Review",
        # Explicit so the real classify_task/ui gate resolve deterministically
        # instead of inferring UI impact from scenario prose.
        "ui_impact": "no",
        "agent_state": {},
        "git_state": {
            "head_sha": head_sha,
            "branch": BRANCH,
            "pr_number": pr_number,
            "pr_url": pr_url,
        },
    }
    with ExitStack() as stack:
        enter = stack.enter_context

        def seam(name: str, **kwargs: Any) -> None:
            enter(patch.object(merge_gate_mod, name, **kwargs))

        # --- project/board reads: real logic, DB replaced by its default -----
        enter(patch.object(projects_repo, "get_meta", side_effect=_meta_default))
        seam("has_project", return_value=True)
        seam("get_task", return_value=task_row)
        seam("get_project_github_repo", return_value=CANONICAL_REPO)
        seam("get_project_repo_topology",
             side_effect=projects_repo.get_project_repo_topology)
        seam("get_project_repo_role",
             side_effect=projects_repo.get_project_repo_role)
        seam("get_session_policy_profiles",
             side_effect=projects_repo.get_session_policy_profiles)
        seam("_session_policy_profile_rules",
             side_effect=projects_repo._session_policy_profile_rules)
        seam("_normalize_session_policy_profile",
             side_effect=projects_repo._normalize_session_policy_profile)

        # --- pure production helpers reached through the façade --------------
        seam("_parse_evidence", side_effect=_parse_evidence)
        seam("_coerce_str_list", side_effect=_coerce_str_list)
        seam("_executed_test_run_gate", side_effect=_executed_test_run_gate)
        seam("_task_work_session_profile",
             side_effect=work_sessions_repo._task_work_session_profile)
        seam("pr_backed_by_process",
             side_effect=work_sessions_repo.pr_backed_by_process)

        # --- Work Session store ---------------------------------------------
        seam("list_work_sessions",
             side_effect=_list_work_sessions_stub(session_rows))
        seam("get_work_session", return_value=None)
        seam("_work_session_row", side_effect=lambda row: dict(row or {}))

        # --- external CI receipts -------------------------------------------
        seam("_external_ci_review_gate",
             side_effect=_external_ci_gate(
                 evidence["external_ci"], head_sha, status_context))

        # --- review verdicts: real gate, in-memory schema -------------------
        _seed_review(db, evidence["review_verdict"], task_id, head_sha,
                     pr_url, pr_number)
        enter(patch.object(review_repo, "_conn", return_value=db))
        seam("review_merge_gate_findings",
             side_effect=review_repo.review_merge_gate_findings)

        # --- execution publications (real reader, empty in-memory table) ----
        seam("_conn", return_value=db)

        # --- GitHub ----------------------------------------------------------
        seam("_github_token", return_value="")
        seam("_github_pr", return_value={})
        seam("_github_repo_from_pr_url", side_effect=_github_repo_from_pr_url)

        # --- writes: record=False already skips these; fail loudly if not ----
        seam("append_activity",
             side_effect=AssertionError("conformance merge_gate must not record"))
        seam("record_review_save",
             side_effect=AssertionError("conformance merge_gate must not record"))
        yield


def merge_gate_result(scenario_id: str, world: dict[str, Any], *,
                      task_id: str, head_sha: str, pr_url: str,
                      pr_number: int, github_pr: dict[str, Any],
                      required_status_contexts: list[str],
                      status_contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the REAL merge gate for one scenario world and return its result.

    The whole result is returned, not just ``findings``:
    ``build_completion_snapshot`` stores it as ``snapshot["merge_gate"]`` and
    lifts ``findings`` from it, so handing over the real object keeps every
    downstream reader looking at what production would have handed it.
    """
    evidence = effective_evidence(world)
    session_rows = _work_session_rows(
        evidence["work_session"], evidence["executed_test_run"], task_id,
        head_sha)
    status_context = str(world["ci_context"])
    payload: dict[str, Any] = {
        "task_id": task_id,
        "head_sha": head_sha,
        "branch": BRANCH,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "session_policy_profile": "code_strict",
        "required_status_contexts": list(required_status_contexts),
        # The gate reads the same PR object and the same context rows the
        # snapshot does; a divergence there would make the two views of one
        # world disagree for reasons the scenario never declared.
        "github_pr": {**copy.deepcopy(github_pr),
                      "status_contexts": copy.deepcopy(status_contexts)},
        **_executed_test_evidence(
            evidence["executed_test_run"], head_sha, evidence["work_session"]),
    }
    db = _review_db()
    with _gate_seams(
            scenario_id, evidence, task_id=task_id, head_sha=head_sha,
            pr_url=pr_url, pr_number=pr_number, status_context=status_context,
            session_rows=session_rows, db=db):
        return merge_gate_mod.merge_gate(
            payload, actor=f"conformance/{scenario_id}", project="switchboard",
            record=False)


def missing_artifact_near_miss_keys(decision: dict[str, Any]) -> list[str]:
    """The near-miss evidence keys one decision carries, in stable order.

    CO-21's whole lesson: the gate computed "your five suites are under
    ``executed_tests``, one key away, in worksession-aa0ccd80bb504bbd" at the
    moment it refused, and the classifier dropped it — so the repair dispatch
    re-derived the diagnosis and changed nothing. Asserting the reason code
    alone cannot catch that regression; asserting the named key can.
    """
    report = (decision or {}).get("missing_artifact") or {}
    near_miss = report.get("found_near_miss") or []
    return sorted({
        str(item.get("key") or "") for item in near_miss
        if isinstance(item, dict) and item.get("key")
    })


def evidence_coverage(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Which evidence-axis values any scenario reaches, and which none do.

    Reported per axis rather than as a cartesian product with the PR axes: the
    full cross would be ~243,000 cells, and a coverage table nobody can read is
    the same as no coverage table. Effective (default-applied) evidence is what
    counts, because that is the world the gate was actually run against.
    """
    covered: dict[str, dict[str, list[str]]] = {
        axis: {value: [] for value in values}
        for axis, values in EVIDENCE_AXES.items()
    }
    for scenario in scenarios:
        evidence = effective_evidence(scenario["world"])
        for axis, value in evidence.items():
            covered[axis][value].append(scenario["id"])
    undefined = [
        f"{axis}={value}"
        for axis, values in covered.items()
        for value, ids in values.items()
        if not ids
    ]
    return {"covered": covered, "undefined": sorted(undefined)}


def reset_state() -> None:
    """Drop the cached in-memory database (test-isolation helper)."""
    global _REVIEW_DB
    if _REVIEW_DB is not None:
        _REVIEW_DB.close()
        _REVIEW_DB = None


__all__ = [
    "EVIDENCE_AXES", "OTHER_HEAD", "BRANCH", "WORK_SESSION_ID",
    "BORROWED_FROM_TASK", "default_evidence", "effective_evidence",
    "validate_evidence", "merge_gate_result", "evidence_coverage",
    "missing_artifact_near_miss_keys", "reset_state",
]
