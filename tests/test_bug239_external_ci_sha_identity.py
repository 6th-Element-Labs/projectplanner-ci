"""BUG-239: exact-head CI evidence must be visible even when the run row has no task_id.

The mirror/organic CI path records runs keyed by ``source_sha`` and leaves
``task_id`` NULL (83% of prod rows). ``_task_external_ci_summary_in`` selected
``WHERE task_id=?`` only, so a PASSING run pinned to the exact gated head was
invisible: the gate reported ``missing``, ENFORCE-16's
``_executed_test_gate_from_external_ci`` derivation (which requires
``summary.passed``) never fired, merge_gate raised ``missing_executed_test_run``,
and the mission reducer emitted START_REMEDIATION every tick forever
(prod 2026-07-30, QA-24 / PR #1097, 35 ticks / 20 min).

The sha is content-addressed, so a run pinned to the exact head is by
construction a run of exactly this code. Cross-task leakage is still fenced:
matching is scoped to the same source project/repo, and a different sha must
never be credited.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug239-external-ci-")
os.environ.setdefault("PM_DB_PATH", os.path.join(_TMP, "maxwell.db"))
os.environ.setdefault("PM_HELM_DB_PATH", os.path.join(_TMP, "helm.db"))
os.environ.setdefault("PM_SWITCHBOARD_DB_PATH", os.path.join(_TMP, "switchboard.db"))
os.environ.setdefault("PM_PROJECT_REGISTRY_DB_PATH", os.path.join(_TMP, "project_registry.db"))
os.environ.setdefault("PM_DYNAMIC_PROJECTS_DIR", _TMP)

import store  # noqa: E402
from switchboard.storage.repositories import external_ci as ci  # noqa: E402


_HEAD = "d76647d3b888af9fedd4ade1d70de9f6b4105e2f"
_OTHER = "a7170c31f5cf573547864daf398cc26b333e184b"
_PROJECT = "switchboard"


def _insert_run(source_sha, *, task_id, status="success", conclusion="success",
                run_id=None, source_repo="6th-Element-Labs/projectplanner"):
    """Insert a mirror run row directly, mirroring how the CI path writes them."""
    with ci._conn(_PROJECT) as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(external_ci_runs)")}
        values = {
            "run_id": run_id or f"ecir-{source_sha[:12]}-{task_id or 'null'}",
            "source_project": _PROJECT,
            "source_repo": source_repo,
            "source_sha": source_sha,
            "mirror_repo": "6th-Element-Labs/projectplanner-ci",
            "mirror_branch": f"ci/pr-1097/{source_sha[:12]}",
            "workflow": "verify",
            "status": status,
            "conclusion": conclusion,
            "task_id": task_id,
        }
        for stamp in ("requested_at", "updated_at", "completed_at"):
            if stamp in cols:
                values[stamp] = 1785372000.0
        usable = {k: v for k, v in values.items() if k in cols}
        c.execute(
            f"INSERT OR REPLACE INTO external_ci_runs({','.join(usable)}) "
            f"VALUES ({','.join('?' for _ in usable)})",
            tuple(usable.values()),
        )


class Bug239ExternalCiShaIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_project_registry()
        store.init_db(_PROJECT)

    def test_passing_run_with_null_task_id_is_seen_at_exact_head(self):
        _insert_run(_HEAD, task_id=None)
        summary = ci.task_external_ci_summary(
            "QA-239-A", source_sha=_HEAD, project=_PROJECT)
        self.assertTrue(
            summary.get("passed"),
            f"exact-head passing run must be credited; got {summary.get('status')}")
        self.assertEqual(summary.get("status"), "passed")

    def test_derivation_gate_fires_so_the_remediation_loop_converges(self):
        _insert_run(_HEAD, task_id=None)
        summary = ci.task_external_ci_summary(
            "QA-239-B", source_sha=_HEAD, project=_PROJECT)
        from switchboard.application.commands.merge_gate import (
            _executed_test_gate_from_external_ci)
        derived = _executed_test_gate_from_external_ci(summary, _HEAD)
        self.assertIsNotNone(
            derived, "ENFORCE-16 derivation must fire on an exact-head green run")
        self.assertTrue(derived.get("ok"))

    def test_run_on_a_different_sha_is_never_credited(self):
        _insert_run(_OTHER, task_id=None)
        summary = ci.task_external_ci_summary(
            "QA-239-C", source_sha="f" * 40, project=_PROJECT)
        self.assertFalse(
            summary.get("passed"), "a run on another head must not be credited")

    def test_task_id_bound_rows_still_work(self):
        _insert_run(_HEAD, task_id="QA-239-D", run_id="ecir-bound-239d")
        summary = ci.task_external_ci_summary(
            "QA-239-D", source_sha=_HEAD, project=_PROJECT)
        self.assertTrue(summary.get("passed"), "task-bound lookup must keep working")

    def test_failing_exact_head_run_is_not_credited_as_passed(self):
        sha = "c" * 40
        _insert_run(sha, task_id=None, status="failure", conclusion="failure")
        summary = ci.task_external_ci_summary(
            "QA-239-E", source_sha=sha, project=_PROJECT)
        self.assertFalse(summary.get("passed"))
        self.assertEqual(summary.get("status"), "failed")

    def test_no_source_sha_does_not_credit_unrelated_runs(self):
        """Without a gated head there is no identity to trust — stay task-scoped."""
        _insert_run("e" * 40, task_id=None)
        summary = ci.task_external_ci_summary(
            "QA-239-F", source_sha="", project=_PROJECT)
        self.assertFalse(
            summary.get("passed"),
            "a sha-less summary must not inherit unrelated NULL-task runs")


if __name__ == "__main__":
    unittest.main()
