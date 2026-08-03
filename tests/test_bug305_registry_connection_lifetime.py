"""BUG-305: registry context ownership includes the SQLite connection."""
from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from db import core


class RegistryConnectionLifetimeTest(unittest.TestCase):
    def test_registry_context_closes_without_garbage_collection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = os.path.join(temp_dir, "project_registry.db")
            opened = []

            with patch.object(core, "PROJECT_REGISTRY_DB_PATH", registry_path):
                gc.disable()
                try:
                    for value in range(300):
                        with core._registry_conn() as registry:
                            registry.execute(
                                "CREATE TABLE IF NOT EXISTS probe (value INTEGER)"
                            )
                            registry.execute(
                                "INSERT INTO probe(value) VALUES (?)", (value,)
                            )
                        opened.append(registry)
                finally:
                    gc.enable()

            self.assertEqual(len(opened), 300)
            for registry in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    registry.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
