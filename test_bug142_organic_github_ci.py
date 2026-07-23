#!/usr/bin/env python3
"""BUG-142: GitHub-native checks become durable exact-head CI evidence."""
import json
import os
import tempfile
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="switchboard-bug142-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")

import organic_github_ci  # noqa: E402
import store  # noqa: E402
import webhook_inbox  # noqa: E402

P = "switchboard"
REPO = "6th-Element-Labs/projectplanner"
SHA1 = "a" * 40
SHA2 = "b" * 40


def payload(kind, *, state="completed", conclusion="success", sha=SHA1, ident=101):
    item = {"id": ident, "head_sha": sha, "status": state,
            "conclusion": conclusion, "html_url": "https://github.test/run/101"}
    if kind == "check_run":
        item["name"] = "unit"
    else:
        item["app"] = {"name": "actions"}
    return {"repository": {"full_name": REPO}, kind: item}


class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self): return json.dumps(self.value).encode()


store.init_project_registry()
store.init_db(P)
store.set_project_repo_topology(project=P, canonical_repo=REPO,
                                public_ci_repo="6th-Element-Labs/projectplanner-ci")
task = store.create_task({"workstream_id": "BUG", "title": "organic checks"}, project=P)
tid = task["task_id"]
store.mark_task_pr_opened(tid, 142, "https://github.com/6th-Element-Labs/projectplanner/pull/142",
                          "codex/BUG-142", SHA1, "test", P)

# check_run: green and duplicate delivery upsert one row.
first = organic_github_ci.handle_webhook("check_run", payload("check_run"), P)
second = organic_github_ci.handle_webhook("check_run", payload("check_run"), P)
rows = store.list_external_ci_runs(task_id=tid, project=P)
assert first["runs"][0]["status"] == "success"
assert second["runs"][0]["idempotent"] is True and len(rows) == 1

# Pending then red update the same source identity.
organic_github_ci.handle_webhook(
    "check_run", payload("check_run", state="in_progress", conclusion=""), P)
assert store.list_external_ci_runs(task_id=tid, project=P)[0]["status"] == "running"
organic_github_ci.handle_webhook(
    "check_run", payload("check_run", conclusion="failure"), P)
assert store.list_external_ci_runs(task_id=tid, project=P)[0]["status"] == "failure"

# check_suite and commit status are accepted as distinct native sources.
organic_github_ci.handle_webhook("check_suite", payload("check_suite", ident=202), P)
organic_github_ci.handle_webhook("status", {
    "repository": {"full_name": REPO}, "id": 303, "sha": SHA1,
    "context": "Switchboard / claim gate", "state": "success",
    "target_url": "https://github.test/status/303"}, P)
assert len(store.list_external_ci_runs(task_id=tid, project=P)) == 3

# PR head change invalidates native prior-head evidence but leaves mirror evidence alone.
mirror = store.create_external_ci_run({
    "source_project": P, "source_repo": REPO, "source_sha": SHA1,
    "mirror_repo": "6th-Element-Labs/projectplanner-ci", "workflow": "verify.yml",
    "task_id": tid, "status": "success", "conclusion": "success"}, project=P)
organic_github_ci.invalidate_prior_head(tid, SHA2, P)
prior = store.list_external_ci_runs(task_id=tid, source_sha=SHA1, project=P)
assert all(r["result"].get("invalidated_by_head_sha") == SHA2
           for r in prior if r["run_id"] != mirror["run_id"])
assert not mirror.get("result", {}).get("invalidated_by_head_sha")

# Missed-webhook recovery polls both check-runs and commit statuses.
responses = iter([
    Response({"check_runs": [{"id": 404, "name": "recovered", "status": "completed",
                              "conclusion": "success", "html_url": "https://github.test/404"}]}),
    Response({"statuses": [{"id": 405, "context": "recovered-status", "state": "pending"}]})])
with mock.patch("organic_github_ci.urllib.request.urlopen", side_effect=lambda *_a, **_k: next(responses)):
    recovered = organic_github_ci.poll_pr_checks(P, REPO, tid, SHA2, token="token")
assert len(recovered["recorded"]) == 2 and not recovered["errors"]
assert {r["status"] for r in recovered["recorded"]} == {"success", "running"}

# Durable inbox dispatches organic event types.
guid = "bug142-check-suite"
webhook_inbox.enqueue_event(P, delivery_guid=guid, event="check_suite",
                            payload_bytes=json.dumps(payload("check_suite", sha=SHA2, ident=406)))
drained = webhook_inbox.drain(P)
assert drained["applied"] == 1

print("PASS BUG-142 organic GitHub CI ingestion")
