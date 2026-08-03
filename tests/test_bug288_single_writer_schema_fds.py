"""BUG-288 — schema initialization must not leak serialized writer FDs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from path_setup import ROOT
from db import connection


class SingleWriterSchemaFdTest(unittest.TestCase):
    def test_closed_writer_preserves_cursor_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "cursor-results.db")
            reader = connection._open_sqlite(db_path, 5.0)
            reader.execute("CREATE TABLE values_to_return (value TEXT)")
            proxy = connection._SerializedWriteProxy(db_path, 5.0, reader)

            with patch.dict(os.environ, {"PM_SQLITE_SINGLE_WRITER": "1"}):
                inserted = proxy.execute(
                    "INSERT INTO values_to_return(value) VALUES (?)", ("first",))
                returning = proxy.execute(
                    "INSERT INTO values_to_return(value) VALUES (?) RETURNING value",
                    ("second",),
                )

            self.assertEqual(inserted.rowcount, 1)
            self.assertEqual(inserted.lastrowid, 1)
            self.assertEqual(returning.fetchone()["value"], "second")
            self.assertIsNone(returning.fetchone())
            reader.close()

    def test_all_builtin_projects_initialize_under_256_fd_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.update({
                "PM_DB_PATH": os.path.join(temp_dir, "maxwell.db"),
                "PM_HELM_DB_PATH": os.path.join(temp_dir, "helm.db"),
                "PM_SWITCHBOARD_DB_PATH": os.path.join(temp_dir, "switchboard.db"),
                "PM_PROJECT_REGISTRY_DB_PATH": os.path.join(temp_dir, "projects.db"),
                "PM_DYNAMIC_PROJECTS_DIR": os.path.join(temp_dir, "projects"),
                "PM_SQLITE_SINGLE_WRITER": "1",
            })
            child = subprocess.run(
                [sys.executable, "-c", textwrap.dedent("""
                    import json
                    import os
                    import resource

                    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                    resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, hard), hard))

                    import store

                    counts = {"before": len(os.listdir("/dev/fd"))}
                    for project in ("maxwell", "helm", "switchboard"):
                        store.init_db(project)
                        counts[project] = len(os.listdir("/dev/fd"))
                    print(json.dumps(counts, sort_keys=True))
                """)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(child.returncode, 0, child.stderr or child.stdout)
        counts = json.loads(child.stdout.strip().splitlines()[-1])
        self.assertLess(
            max(counts.values()),
            64,
            f"schema initialization retained too many file descriptors: {counts}",
        )


if __name__ == "__main__":
    unittest.main()
