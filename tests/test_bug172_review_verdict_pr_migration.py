"""BUG-172: replacement-PR verdict migration is atomic and lossless."""
from __future__ import annotations

import sqlite3
import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.storage.migrations import runner


LEGACY_SQL = """
CREATE TABLE review_verdicts (
    verdict_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    reviewer_principal TEXT NOT NULL,
    reviewer_principal_id TEXT,
    review_mode TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'review_command',
    created_at REAL NOT NULL,
    recorded_at REAL NOT NULL,
    UNIQUE(task_id, head_sha)
)
"""


def connection() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE schema_migrations("
        "name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    c.execute(LEGACY_SQL)
    c.execute(
        "CREATE TABLE review_findings("
        "verdict_id TEXT NOT NULL, finding_id TEXT NOT NULL)"
    )
    c.execute(
        "CREATE TABLE review_remediations("
        "remediation_id TEXT PRIMARY KEY, verdict_id TEXT NOT NULL)"
    )
    c.execute(
        "INSERT INTO review_verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "verdict-old",
            "COORD-46",
            "https://example.test/pull/825",
            "a" * 40,
            "codex/reviewer",
            "principal/shared",
            "adversarial",
            "changes_requested",
            "review_command",
            1.0,
            2.0,
        ),
    )
    c.execute(
        "INSERT INTO review_findings VALUES (?,?)",
        ("verdict-old", "F-1"),
    )
    c.execute(
        "INSERT INTO review_remediations VALUES (?,?)",
        ("remediation-old", "verdict-old"),
    )
    all_names = {
        item[0] for item in runner.ADDITIVE_COLUMN_MIGRATIONS
    } | {item[0] for item in runner.DDL_MIGRATIONS}
    for name in sorted(
        all_names - {runner.REVIEW_VERDICT_PR_IDENTITY_MIGRATION}
    ):
        c.execute(
            "INSERT INTO schema_migrations VALUES (?,1)",
            (name,),
        )
    c.commit()
    return c


class ReviewVerdictPrIdentityMigration(unittest.TestCase):
    def test_rebuild_preserves_history_and_allows_same_sha_replacement(self):
        c = connection()
        try:
            applied = runner.run_additive_migrations(c)
            self.assertEqual(
                applied, [runner.REVIEW_VERDICT_PR_IDENTITY_MIGRATION])
            old = c.execute(
                "SELECT * FROM review_verdicts WHERE verdict_id='verdict-old'"
            ).fetchone()
            self.assertEqual(old["review_mode"], "adversarial")
            self.assertEqual(
                c.execute("SELECT verdict_id FROM review_findings").fetchone()[0],
                "verdict-old",
            )
            self.assertEqual(
                c.execute(
                    "SELECT verdict_id FROM review_remediations"
                ).fetchone()[0],
                "verdict-old",
            )
            c.execute(
                "INSERT INTO review_verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "verdict-new",
                    "COORD-46",
                    "https://example.test/pull/826",
                    "a" * 40,
                    "claude/reviewer",
                    "principal/shared",
                    "adversarial",
                    "pass",
                    "review_command",
                    3.0,
                    4.0,
                ),
            )
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM review_verdicts "
                    "WHERE task_id='COORD-46' AND head_sha=?",
                    ("a" * 40,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(runner.run_additive_migrations(c), [])
        finally:
            c.close()

    def test_mid_rebuild_failure_rolls_back_original_table(self):
        c = connection()
        try:
            def deny_drop(action, arg1, _arg2, _db, _trigger):
                if (
                    action == sqlite3.SQLITE_DROP_TABLE
                    and arg1 == "review_verdicts"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            c.execute("BEGIN IMMEDIATE")
            c.set_authorizer(deny_drop)
            with self.assertRaises(sqlite3.DatabaseError):
                runner._migrate_review_verdict_pr_identity(c)
            c.set_authorizer(None)
            c.rollback()
            self.assertEqual(
                c.execute(
                    "SELECT verdict_id FROM review_verdicts"
                ).fetchone()[0],
                "verdict-old",
            )
            tables = {
                row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("review_verdicts__0116", tables)
            unique_sets = runner._review_verdict_unique_columns(c)
            self.assertIn(("task_id", "head_sha"), unique_sets)
            self.assertNotIn(
                ("task_id", "pr_url", "head_sha"), unique_sets)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
