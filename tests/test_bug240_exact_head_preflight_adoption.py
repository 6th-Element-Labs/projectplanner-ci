"""BUG-240: an exact-head preflight must stay valid across later Work Sessions.

A fresh review or remediation generation opens a NEW Work Session with no
preflight of its own. merge_gate read only ``session.hygiene.repo_preflight``
for the caller-supplied session, so it raised ``missing_work_session_preflight``
even though preflight_runs already held a clean run at the exact gated head
recorded by the implementation session (prod 2026-07-30, QA-25 / PR #1100).
The review generation cannot satisfy that demand, so the mission reducer looped
START_REMEDIATION forever.

This is BUG-234 clause 2 — "exact-head proof remains valid across later Work
Sessions; never compare to the newest session" — applied to the preflight gate,
which that fix never reached. The fences that must survive: a preflight on a
DIFFERENT head is never adopted, and neither is one belonging to another task.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug240-preflight-")
os.environ.setdefault("PM_DB_PATH", os.path.join(_TMP, "maxwell.db"))
os.environ.setdefault("PM_HELM_DB_PATH", os.path.join(_TMP, "helm.db"))
os.environ.setdefault("PM_SWITCHBOARD_DB_PATH", os.path.join(_TMP, "switchboard.db"))
os.environ.setdefault("PM_PROJECT_REGISTRY_DB_PATH", os.path.join(_TMP, "project_registry.db"))
os.environ.setdefault("PM_DYNAMIC_PROJECTS_DIR", _TMP)

import store  # noqa: E402
from switchboard.application.commands import merge_gate as mg  # noqa: E402

_PROJECT = "switchboard"
_HEAD = "a7170c31f5cf573547864daf398cc26b333e184b"
_OTHER_HEAD = "b" * 40


def _make_task(label):
    """Create the board task a Work Session must reference; return its real id."""
    task = store.create_task(
        {"workstream_id": "QA", "title": f"{label} preflight fixture"},
        actor="test", project=_PROJECT)
    return task["task_id"]


def _make_session(session_id, task_id, *, head=_HEAD, repo_role="canonical"):
    created = store.create_work_session(
        {
            "work_session_id": session_id,
            "task_id": task_id,
            "agent_id": f"agent/test/{task_id.lower()}",
            "repo_role": repo_role,
            "repo": "6th-Element-Labs/projectplanner",
            "default_branch": "master",
            "branch": f"agent/switchboard/{task_id}/g1",
            "head_sha": head,
            "base_sha": head,
            "storage_mode": "worktree",
            "worktree_path": f"/tmp/ws/{session_id}",
            "status": "active",
        },
        actor="test", project=_PROJECT)
    assert not created.get("error"), f"fixture session invalid: {created}"
    return created


def _record_preflight(session_id, task_id, head, *, verdict="pass", ok=True):
    from switchboard.storage.repositories.preflight_runs import record_preflight_run
    return record_preflight_run(
        {
            "ok": ok,
            "verdict": verdict,
            "head_sha": head,
            "base_sha": head,
            "branch": f"agent/switchboard/{task_id}/g1",
            "repo_role": "canonical",
            "repo_path": "/tmp/ws",
            "findings": [],
            "changed_files": ["docs/evidence/x.md"],
        },
        work_session_id=session_id,
        actor="test",
        source="test",
        project=_PROJECT,
    )


class Bug240ExactHeadPreflightAdoption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_project_registry()
        store.init_db(_PROJECT)

    def _adopt(self, task_id, head, exclude):
        return mg._exact_head_preflight_for_task(
            task_id, project=_PROJECT, head_sha=head,
            exclude_session_id=exclude)

    def test_adopts_implementation_preflight_for_a_fresh_review_session(self):
        task = _make_task("QA-240A")
        impl, review = f"ws-{task}-impl", f"ws-{task}-review"
        _make_session(impl, task)
        _make_session(review, task)
        _record_preflight(impl, task, _HEAD)

        adopted = self._adopt(task, _HEAD, review)
        self.assertTrue(adopted, "exact-head preflight from a sibling session must be adopted")
        self.assertEqual(adopted.get("head_sha"), _HEAD)
        self.assertEqual(adopted.get("adopted_from_work_session_id"), impl)

    def test_preflight_on_a_different_head_is_never_adopted(self):
        task = _make_task("QA-240B")
        impl, review = f"ws-{task}-impl", f"ws-{task}-review"
        _make_session(impl, task, head=_OTHER_HEAD)
        _make_session(review, task)
        _record_preflight(impl, task, _OTHER_HEAD)

        self.assertFalse(self._adopt(task, _HEAD, review),
                         "a preflight on another head must never authorize this head")

    def test_another_tasks_preflight_is_never_adopted(self):
        mine, theirs = _make_task("QA-240C"), _make_task("QA-240D")
        their_ws, my_review = f"ws-{theirs}-impl", f"ws-{mine}-review"
        _make_session(their_ws, theirs)
        _make_session(my_review, mine)
        _record_preflight(their_ws, theirs, _HEAD)

        self.assertFalse(self._adopt(mine, _HEAD, my_review),
                         "another task's preflight must never satisfy this task's gate")

    def test_no_preflight_anywhere_still_reports_missing(self):
        task = _make_task("QA-240E")
        review = f"ws-{task}-review"
        _make_session(review, task)
        self.assertFalse(self._adopt(task, _HEAD, review),
                         "a never-run preflight must still block")

    def test_adoption_requires_a_gated_head(self):
        task = _make_task("QA-240F")
        impl, review = f"ws-{task}-impl", f"ws-{task}-review"
        _make_session(impl, task)
        _make_session(review, task)
        _record_preflight(impl, task, _HEAD)
        self.assertFalse(self._adopt(task, "", review),
                         "with no gated head there is no identity to trust")


if __name__ == "__main__":
    unittest.main()
