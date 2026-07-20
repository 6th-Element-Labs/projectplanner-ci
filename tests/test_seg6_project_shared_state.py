#!/usr/bin/env python3
"""SEG-6: project isolation, cardinality, and bounded-cache proof."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="seg6-shared-state-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")

import store  # noqa: E402
import rag  # noqa: E402
import read_cache  # noqa: E402
from scripts.seg6_scope_ratchet import violations  # noqa: E402
from switchboard.application.commands import project_digest  # noqa: E402
from switchboard.domain.projects.context import ProjectContext  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


try:
    for project in ("maxwell", "helm", "switchboard"):
        store.init_db(project)
        store.set_meta("project", f"label-{project}", project=project)
        store.add_digest(1.0, f"digest-{project}", project=project)
        store.append_activity("comment", "test", {"project": project}, project=project)

    for project in ("maxwell", "helm", "switchboard"):
        ctx = ProjectContext(project_id=project, source="test")
        rows = project_digest.list_recent(ctx)
        check([row["content"] for row in rows] == [f"digest-{project}"],
              f"digest rows stay isolated for {project}")
        acts = store.activity_since(0, project=project)
        check(len(acts) == 1 and acts[0]["payload"]["project"] == project,
              f"activity rows stay isolated for {project}")

    read_cache._READ_CACHE.clear()
    old_limit = read_cache._READ_CACHE_MAX_ENTRIES
    read_cache._READ_CACHE_MAX_ENTRIES = 8
    for number in range(40):
        read_cache.ttl_read_cache("seg6", f"project-{number}", number, lambda n=number: n)
    check(len(read_cache._READ_CACHE) <= 8, "shared read cache has a hard entry bound")
    read_cache._READ_CACHE_MAX_ENTRIES = old_limit

    rag._dyn = OrderedDict()
    rag._dyn_ver = {}
    old_rag_limit = rag._DYN_CACHE_MAX_PROJECTS
    rag._DYN_CACHE_MAX_PROJECTS = 4
    old_max_id, old_chunks = store.rag_docs_max_id, store.all_rag_chunks
    store.rag_docs_max_id = lambda project: 0
    store.all_rag_chunks = lambda project: []
    for number in range(20):
        rag._load_dyn(f"project-{number}")
    check(len(rag._dyn) <= 4 and len(rag._dyn_ver) <= 4,
          "dynamic RAG cache has a hard project-cardinality bound")
    store.rag_docs_max_id, store.all_rag_chunks = old_max_id, old_chunks
    rag._DYN_CACHE_MAX_PROJECTS = old_rag_limit

    check(not violations(), "mechanical digest scope ratchet passes")
    shell_loc = sum(len((ROOT / name).read_text().splitlines())
                    for name in ("app.py", "mcp_server.py", "store.py"))
    check(shell_loc <= 950, "combined app.py/mcp_server.py/store.py LOC stays below ratchet")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
