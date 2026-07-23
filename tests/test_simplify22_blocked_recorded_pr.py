"""SIMPLIFY-22 — Blocked remediation with a recorded PR reaches Done after dropped webhook."""
from __future__ import annotations

import os
import tempfile
import unittest

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="simplify22-blocked-pr-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import store  # noqa: E402


class BlockedRecordedPrRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = "simplify22-blocked-pr"
        store.create_project("SIMPLIFY-22 Blocked PR", project_id=cls.project, actor="test")
        store.set_project_github_repo("6th-Element-Labs/projectplanner", project=cls.project)
        store.init_db(cls.project)

    def test_blocked_recorded_pr_stamps_done_when_webhook_dropped(self):
        task = store.create_task(
            {"workstream_id": "SIMPLIFY", "title": "blocked recorded PR"},
            actor="test", project=self.project,
        )
        task_id = task["task_id"]
        pr_url = "https://github.com/6th-Element-Labs/projectplanner/pull/822"
        head = "d" * 40
        merge_sha = "e" * 40
        with store._conn(self.project) as c:
            c.execute("UPDATE tasks SET status='Blocked' WHERE task_id=?", (task_id,))
            store._upsert_git_state(c, task_id, {
                "pr_number": 822,
                "pr_url": pr_url,
                "branch": f"codex/{task_id}-slug",
                "head_sha": head,
            })

        original_fetch = store._fetch_github_prs
        original_token = store._github_token
        original_orphan = store._orphan_merge_discovery_findings

        def fake_fetch_prs(pr_keys, token=""):
            out = {}
            for repo, pr_number in pr_keys:
                if int(pr_number) == 822:
                    out[(repo, 822)] = {
                        "merged_at": "2026-07-24T12:00:00Z",
                        "merge_commit_sha": merge_sha,
                        "html_url": pr_url,
                        "base": {"ref": "master", "repo": {"default_branch": "master"}},
                        "head": {"ref": f"codex/{task_id}-slug", "sha": head},
                        "title": f"{task_id}: blocked recorded PR",
                        "body": "",
                    }
            return out, {"github_prs_fetch": "mocked"}

        store._fetch_github_prs = fake_fetch_prs
        store._github_token = lambda: "tok"
        store._orphan_merge_discovery_findings = (
            lambda *a, **k: ([], [], {"orphan_merge_discovery": "skipped_test"})
        )
        try:
            report = store.reconcile(project=self.project)
        finally:
            store._fetch_github_prs = original_fetch
            store._github_token = original_token
            store._orphan_merge_discovery_findings = original_orphan

        after = store.get_task(task_id, project=self.project)
        self.assertEqual(after["status"], "Done")
        self.assertEqual(after["git_state"]["merged_sha"], merge_sha)
        self.assertTrue(any(b.get("task_id") == task_id for b in report.get("backfilled") or []))


if __name__ == "__main__":
    unittest.main()
