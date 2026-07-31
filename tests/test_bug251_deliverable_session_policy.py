#!/usr/bin/env python3
"""BUG-251: the deliverable's declared session_policy covers its linked tasks.

Task authors declare the evidence contract once, on the deliverable
(metadata.session_policy). No per-task text marker is required; dispatch
resolves the profile from the deliverable link. Explicit task inputs (agent
state, legacy text markers) still win; zero or conflicting declarations fall
through to project defaults unchanged.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.storage.repositories import work_sessions

PROFILES = {
    "offline_evidence": {"work_session_required": False},
    "docs_review": {"work_session_required": False},
    "code_strict": {"work_session_required": True},
}


class _Facade:
    def __init__(self, links):
        self._links = links

    def list_task_deliverable_links(self, task_id, project="switchboard"):
        return self._links

    def _normalize_session_policy_profile(self, profile):
        return str(profile or "").strip().lower()

    def _session_policy_profile_rules(self, profile, project="switchboard"):
        return dict(PROFILES.get(str(profile or "").strip().lower()) or {})

    def _project_session_policy_defaults(self, project="switchboard"):
        return {"default_profile": "docs_review",
                "code_task_default_profile": "docs_review"}

    def _session_profile_text(self, task):
        return " ".join(str(task.get(k) or "") for k in ("title", "description"))


def _resolve(task, links):
    with patch.object(work_sessions, "_store_facade", return_value=_Facade(links)):
        return work_sessions._task_work_session_profile(
            task, "", project="switchboard")


def _link(policy):
    return {"deliverable_id": "d1", "deliverable_session_policy": policy}


class DeliverableSessionPolicyTest(unittest.TestCase):
    task = {"task_id": "QA-46", "title": "Wave A1",
            "description": "Non-code task. Offline evidence only."}

    def test_deliverable_declaration_resolves_without_any_marker(self):
        self.assertEqual(
            "offline_evidence", _resolve(self.task, [_link("offline_evidence")]))

    def test_legacy_text_marker_still_wins_over_the_deliverable(self):
        marked = dict(self.task,
                      description="session_profile:code_strict legacy task")
        self.assertEqual(
            "code_strict", _resolve(marked, [_link("offline_evidence")]))

    def test_conflicting_deliverables_fall_through_to_defaults(self):
        self.assertEqual(
            "docs_review",
            _resolve(self.task, [_link("offline_evidence"), _link("code_strict")]))

    def test_unknown_or_absent_declarations_fall_through(self):
        self.assertEqual("docs_review", _resolve(self.task, [_link("bogus")]))
        self.assertEqual("docs_review", _resolve(self.task, []))
        self.assertEqual("docs_review", _resolve(self.task, [_link("")]))

    def test_duplicate_identical_declarations_agree(self):
        self.assertEqual(
            "offline_evidence",
            _resolve(self.task,
                     [_link("offline_evidence"), _link("offline_evidence")]))

    def test_broken_link_read_degrades_to_defaults(self):
        class _Broken(_Facade):
            def list_task_deliverable_links(self, task_id, project="switchboard"):
                raise RuntimeError("control plane busy")

        with patch.object(work_sessions, "_store_facade",
                          return_value=_Broken([])):
            profile = work_sessions._task_work_session_profile(
                self.task, "", project="switchboard")
        self.assertEqual("docs_review", profile)


if __name__ == "__main__":
    unittest.main()
