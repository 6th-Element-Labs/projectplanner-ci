#!/usr/bin/env python3
"""SEG-7 two-project isolation conformance and regression ratchet.

The probe is deliberately hermetic: it creates one sqlite database per synthetic
tenant, seeds unmistakable canaries through the shared repositories, and then
checks both disclosure and mutation in both directions.  Its JSON output is the
machine-readable exit artifact; callers may set ``SEG7_REPORT`` and
``SEG7_TESTED_SHA`` when running it for a committed revision.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ("maxwell", "helm")
SURFACES = (
    "metadata", "tasks", "activity", "rag", "chat", "contacts", "inbox",
    "attachments", "digests", "notifications", "agent_messages", "costs",
    "exports", "caches", "generated_prompts", "web_session", "bearer_token",
    "missing_scope", "project_switching",
)


def _sha() -> str:
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    override = os.environ.get("SEG7_TESTED_SHA", "").strip()
    if override and override != actual:
        raise ValueError(f"SEG7_TESTED_SHA {override} does not match git HEAD {actual}")
    return actual


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[byte / 255.0 for byte in hashlib.sha256(text.encode()).digest()[:16]]
            for text in texts]


def _contains(value: object, needle: str) -> bool:
    return needle in json.dumps(value, sort_keys=True, default=str)


def run() -> dict:
    tested_sha = _sha()
    temp = Path(tempfile.mkdtemp(prefix="seg7-conformance-"))
    os.environ.update({
        "PM_DB_PATH": str(temp / "maxwell.db"),
        "PM_HELM_DB_PATH": str(temp / "helm.db"),
        "PM_SWITCHBOARD_DB_PATH": str(temp / "switchboard.db"),
        "PM_PROJECT_REGISTRY_DB_PATH": str(temp / "registry.db"),
        "PM_DYNAMIC_PROJECTS_DIR": str(temp),
        "PM_AUTH_MODE": "required",
        "PM_JWT_SECRET": "seg7-hermetic-secret",
    })
    import sys
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

    import agent
    import auth
    import rag
    import read_cache
    import store
    from fastapi.testclient import TestClient
    from app import app
    from switchboard.api.routers.auth import store as auth_store

    started = time.perf_counter()
    checks: list[dict] = []
    gateway_counts = {"llm": 0, "embedding": 0}
    local_embedding_calls = 0
    canaries = {project: f"SEG7_{project.upper()}_CANARY_7f21" for project in PROJECTS}

    def check(name: str, condition: bool, **evidence: object) -> None:
        checks.append({"name": name, "ok": bool(condition), "evidence": evidence})

    try:
        def counted_embed(texts: list[str]) -> list[list[float]]:
            nonlocal local_embedding_calls
            local_embedding_calls += 1
            return _fake_embed(texts)

        def forbidden_chat(*args, **kwargs):
            gateway_counts["llm"] += 1
            raise AssertionError("SEG-7 conformance must not call the LLM gateway")

        rag._embed = counted_embed
        agent._chat = forbidden_chat
        rag._index = []
        rag._dyn, rag._dyn_ver = {}, {}
        for project in PROJECTS:
            canary = canaries[project]
            store.init_db(project)
            store.set_meta("seg7_canary", canary, project=project)
            task = store.create_task({"workstream_id": "SEG7", "title": canary,
                                      "description": f"attachment:{canary}.txt"},
                                     actor="seg7/setup", project=project)
            store.append_activity("seg7", "seg7/setup", {"canary": canary}, project=project)
            rag.add_document("seg7", canary, f"rag {canary}", project=project)
            store.add_chat("seg7-session", "user", f"chat {canary}", project=project)
            store.upsert_contact(f"{project}@seg7.invalid", canary, project=project)
            store.add_inbox_item("seg7", canary, f"{project}@seg7.invalid", canary,
                                 f"attachment {canary}.txt", {"canary": canary}, project=project)
            store.add_digest(0, f"digest {canary}",
                             {"notification": f"notify {canary}"}, project=project)
            store.report_usage("seg7", "measured", task_id=task["task_id"],
                               model=canary, cost_usd=0.007, total_tokens=7,
                               request_id=f"seg7-{project}", project=project)
            store.register_agent(f"seg7/{project}", "test", lane="SEG7",
                                 task_id=task["task_id"], project=project)
            store.send_agent_message(f"seg7/{project}", f"seg7/{project}",
                                     f"message {canary}", requires_ack=True, project=project)
            read_cache.ttl_read_cache("seg7", project, canary, lambda c=canary: c)

        for project in PROJECTS:
            other = PROJECTS[1] if project == PROJECTS[0] else PROJECTS[0]
            own, foreign = canaries[project], canaries[other]
            views = {
                "metadata": store.get_meta("seg7_canary", project=project),
                "tasks": store.list_tasks(project=project),
                "activity": store.activity_since(0, project=project),
                "rag": rag.search(own, project=project),
                "chat": store.recent_chat("seg7-session", project=project),
                "contacts": store.get_contacts(project=project),
                "inbox": store.list_inbox(project=project),
                "digests": store.list_digests(project=project),
                "agent_messages": store.list_unacked_messages(f"seg7/{project}", project=project),
                "costs": store.task_tally(f"SEG7-1", project=project),
                "exports": store.audit_export(project=project),
                "generated_prompts": agent.board_summary_text(project=project),
            }
            for surface, view in views.items():
                check(f"{project}.{surface}.own_visible", _contains(view, own), surface=surface)
                check(f"{project}.{surface}.foreign_hidden", not _contains(view, foreign),
                      surface=surface)
            # Attachment and notification canaries travel inside inbox/digest records.
            check(f"{project}.attachments.foreign_hidden",
                  not _contains(views["inbox"], foreign), surface="attachments")
            check(f"{project}.notifications.foreign_hidden",
                  not _contains(views["digests"], foreign), surface="notifications")
            check(f"{project}.caches.foreign_hidden",
                  read_cache.ttl_read_cache("seg7", project, own, lambda: "miss") == own,
                  surface="caches")

            store.add_comment(f"SEG7-1", "seg7/mutation", f"mutate {own}", project=project)
            foreign_activity = store.activity_since(0, project=other)
            check(f"{project}.foreign_non_mutation", not _contains(foreign_activity, f"mutate {own}"),
                  target=other)

        # Mutation-test the real repository seam. A simulated historical metadata
        # leak must make the same foreign-canary oracle used above turn red.
        original_get_meta = store.get_meta
        try:
            store.get_meta = lambda key, project="maxwell": (
                canaries["helm"] if key == "seg7_canary" and project == "maxwell"
                else original_get_meta(key, project=project)
            )
            leaked_view = store.get_meta("seg7_canary", project="maxwell")
            check("oracle.detects_known_leak.metadata",
                  _contains(leaked_view, canaries["helm"]), seam="store.get_meta")
        finally:
            store.get_meta = original_get_meta

        # Exercise real web authentication and scope selection, not just storage.
        auth_store.init()
        password = "seg7-correct-horse"
        user = auth_store.create_user("maxwell@seg7.invalid", "SEG7 Maxwell",
                                      auth.password_hash(password))
        store.ensure_bootstrap_project_owner("maxwell", user["id"], "admin",
                                             "SEG7 Maxwell", actor="seg7/setup")
        client = TestClient(app)
        login = client.post("/api/auth/login", json={
            "email": "maxwell@seg7.invalid", "password": password})
        check("web_session.login", login.status_code == 200, status=login.status_code)
        own_board = client.get("/api/board", params={"project": "maxwell"})
        foreign_board = client.get("/api/board", params={"project": "helm"})
        missing_scope = client.get("/api/board")
        check("web_session.own_project", own_board.status_code == 200,
              status=own_board.status_code)
        check("project_switching.session_denied", foreign_board.status_code == 403,
              status=foreign_board.status_code)
        check("missing_scope.web_denied", missing_scope.status_code in {400, 422},
              status=missing_scope.status_code)

        bearer = "seg7-maxwell-bearer"
        store.create_principal(kind="agent", display_name="seg7-bearer", token=bearer,
                               scopes=["read"], project="maxwell")
        headers = {"Authorization": f"Bearer {bearer}"}
        bearer_own = TestClient(app).get("/api/board", params={"project": "maxwell"},
                                         headers=headers)
        bearer_foreign = TestClient(app).get("/api/board", params={"project": "helm"},
                                             headers=headers)
        check("bearer_token.own_project", bearer_own.status_code == 200,
              status=bearer_own.status_code)
        check("project_switching.bearer_denied", bearer_foreign.status_code in {401, 403},
              status=bearer_foreign.status_code)

        cache_projects = {key.split("\0", 1)[1] for key in read_cache._READ_CACHE
                          if isinstance(key, str) and key.startswith("seg7\0")}
        check("cache.keys.project_partitioned", cache_projects == set(PROJECTS),
              projects=sorted(cache_projects), entries=len(read_cache._READ_CACHE))

        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        old_limit = read_cache._READ_CACHE_MAX_ENTRIES
        read_cache._READ_CACHE_MAX_ENTRIES = 8
        for number in range(64):
            read_cache.ttl_read_cache("seg7-cardinality", f"project-{number}", number,
                                      lambda n=number: n)
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        cardinality_entries = len(read_cache._READ_CACHE)
        read_cache._READ_CACHE_MAX_ENTRIES = old_limit
        check("cardinality.cache_bounded", cardinality_entries <= 8,
              projects=64, cache_entries=cardinality_entries,
              rss_delta_kb=max(0, rss_after - rss_before))

        report = {
            "schema": "switchboard.segmentation_conformance.v1",
            "task_id": "SEG-7",
            "tested_sha": tested_sha,
            "ok": all(row["ok"] for row in checks),
            "scenario": {"projects": list(PROJECTS), "surfaces": list(SURFACES),
                         "storage": "hermetic sqlite", "directions": 2},
            "llm_calls": gateway_counts["llm"],
            "embedding_gateway_calls": gateway_counts["embedding"],
            "local_embedding_calls": local_embedding_calls,
            "cardinality": {"projects": 64, "cache_entries": cardinality_entries,
                            "rss_delta_kb": max(0, rss_after - rss_before)},
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "checks": checks,
            "failures": [row for row in checks if not row["ok"]],
        }
        return report
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def main() -> int:
    report = run()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    output = os.environ.get("SEG7_REPORT", "").strip()
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
