#!/usr/bin/env python3
"""ADAPTER-20: hermetic Cursor Cloud Agents v1 adapter tests."""

from copy import deepcopy
import time

from adapters.cloud_execution import CANONICAL_REPO
from adapters.cursor.cloud_execution import (
    CONTINUITY_CAPABILITIES,
    CursorAPIError,
    CursorCloudExecutionAdapter,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.agent_id = ""
        self.agent_status = "ACTIVE"
        self.run_status = "CREATING"
        self.raise_post = None
        self.repositories = [{"url": f"https://github.com/{CANONICAL_REPO}"}]

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/v1/me":
            return {"apiKeyName": "test-key", "userId": 42}
        if method == "GET" and path == "/v1/repositories?limit=100":
            return {"items": self.repositories}
        if method == "GET" and path == "/v1/agents?limit=100":
            return {"items": []}
        if method == "POST" and path == "/v1/agents":
            if self.raise_post:
                raise self.raise_post
            self.agent_id = payload["agentId"]
            return {
                "agent": {"id": self.agent_id, "url": f"https://cursor.com/agents/{self.agent_id}"},
                "run": {"id": "run-20", "status": "CREATING"},
            }
        if method == "GET" and path.startswith("/v1/agents/") and "/runs/" not in path \
                and "/usage" not in path:
            session_id = path.rsplit("/", 1)[-1]
            return {
                "id": session_id,
                "url": f"https://cursor.com/agents/{session_id}",
                "status": self.agent_status,
                "latestRunId": "run-20",
            }
        if method == "GET" and "/runs/run-20" in path:
            return {
                "id": "run-20",
                "status": self.run_status,
                "git": {"branches": []},
            }
        if method == "POST" and path.endswith("/runs"):
            return {"run": {"id": "run-followup", "status": "CREATING"}}
        if method == "GET" and "/usage" in path:
            return {
                "totalUsage": {
                    "inputTokens": 100,
                    "outputTokens": 20,
                    "cacheWriteTokens": 30,
                    "cacheReadTokens": 40,
                    "totalTokens": 190,
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")


dispatch = {
    "schema": "switchboard.cloud_dispatch.v1",
    "project": "switchboard",
    "task_id": "ADAPTER-20",
    "claim_id": "taskclaim-20",
    "wake_id": "wake-20",
    "dev_brief": "Implement ADAPTER-20, run tests, and open a PR.",
    "canonical_repo": CANONICAL_REPO,
    "branch": "cursor/ADAPTER-20-cloud-agent",
    "continuity": "fresh_only",
    "mcp_access": {
        "endpoint": "https://plan.taikunai.com/mcp",
        "token_ref": "vault://switchboard/task/ADAPTER-20",
        "scopes": ["read:task", "write:claim", "write:evidence"],
        "expires_at": time.time() + 3600,
    },
}


def adapter(client=None, *, token="scoped-secret", branch=True, api_key="cursor-key"):
    return CursorCloudExecutionAdapter(
        api_key=api_key,
        client=client or FakeCursor(),
        resolve_mcp_token=lambda ref: token if ref == dispatch["mcp_access"]["token_ref"] else "",
        branch_probe=lambda repo, name: branch and repo.endswith(CANONICAL_REPO)
        and name == dispatch["branch"],
    )


client = FakeCursor()
cursor = adapter(client)
ready = cursor.preflight(dispatch)
ok(ready["allowed"] is True and ready["adopted"] is False
   and ready["dev_status"] == "queued", "provider/account/repo/branch preflight passes")

original = deepcopy(dispatch)
receipt = cursor.trigger(dispatch)
ok(receipt["allowed"] is True and receipt["adopted"] is True,
   "v1 create plus authoritative readback adopts the Cursor agent")
ok(receipt["session_url"].startswith("https://cursor.com/agents/bc-"),
   "binding receipt exposes an app-visible Cursor URL")
ok(receipt["runner_session_id"].startswith("cloud/cursor-background-agent/bc-"),
   "binding receipt supplies Switchboard runner_session identity")
ok(receipt["dev_status"] == "queued" and receipt["provider_status"] == "provisioning",
   "CREATING readback remains queued instead of optimistic running")
ok(dispatch == original and "scoped-secret" not in repr(receipt),
   "scoped MCP token is neither inserted into dispatch nor returned in receipt")

post = next(call for call in client.calls if call[0:2] == ("POST", "/v1/agents"))
body = post[2]
ok(body["repos"] == [{"url": f"https://github.com/{CANONICAL_REPO}",
                      "startingRef": dispatch["branch"]}],
   "Cursor starts from the already-pushed task branch")
ok(body["workOnCurrentBranch"] is True and body["autoCreatePR"] is True,
   "Cursor is required to work on the task branch and open a PR")
ok(body["mcpServers"][0]["url"] == "https://plan.taikunai.com/mcp"
   and body["mcpServers"][0]["headers"]["Authorization"] == "Bearer scoped-secret",
   "scoped token is injected only into Cursor's inline taikun-plan MCP config")
ok(body["agentId"].startswith("bc-") and body["agentId"] in receipt["session_url"],
   "wake-derived stable Cursor agent id makes launch retries adoptable")

replay_client = FakeCursor()
replay = adapter(replay_client)
expected_id = replay._agent_id(dispatch)
replay_client.raise_post = CursorAPIError(409, {"code": "agent_id_conflict"})
replayed = replay.trigger(dispatch)
ok(replayed["allowed"] is True and replayed["idempotent_replay"] is True
   and expected_id in replayed["session_url"],
   "409 for the deterministic agent id reads back the existing run instead of duplicating it")

ok(CONTINUITY_CAPABILITIES["same_run_resume"] is False
   and CONTINUITY_CAPABILITIES["same_agent_follow_up"] is True,
   "capabilities distinguish unsupported exact resume from supported follow-up runs")
resume = cursor.resume_session(receipt["provider_session_id"])
ok(resume["allowed"] is False and resume["reason"] == "same_run_resume_unsupported",
   "exact same-run resume fails closed")
follow_up = cursor.follow_up(receipt["provider_session_id"], "Also update the operator docs.")
ok(follow_up["allowed"] is True and follow_up["provider_run_id"] == "run-followup"
   and follow_up["exact_resume"] is False,
   "same-agent follow-up is explicit and never mislabeled exact resume")

usage = cursor.get_usage(receipt["provider_session_id"], task_id="ADAPTER-20", run_id="run-20")
ok(usage["total_tokens"] == 190 and usage["confidence"] == "reported",
   "Cursor v1 token usage projects into a reported Tally receipt")
ok(usage["cost_usd"] == 0 and usage["cost_status"] == "unknown_until_provider_reconcile"
   and "provider_session_id" not in usage,
   "adapter does not invent dollar cost and hashes provider identity for Tally")

ok(adapter(api_key="").preflight(dispatch)["missing"] == ["cursor_api_key"],
   "missing Cursor Cloud API key fails before provider calls")
ok(adapter(token="").preflight(dispatch)["missing"] == ["scoped_mcp_token_ref"],
   "unresolved scoped MCP token fails before launch")
stale = adapter(branch=False).preflight(dispatch)
ok(stale["allowed"] is False and stale["reason"] == "pushed_task_branch_missing",
   "unpublished task branch fails closed")
no_repo_client = FakeCursor()
no_repo_client.repositories = [{"url": "https://github.com/someone/other"}]
no_repo = adapter(no_repo_client).preflight(dispatch)
ok(no_repo["allowed"] is False and no_repo["missing"] == ["github_repo_grant"],
   "missing canonical repository grant fails closed")

bad_resume = deepcopy(dispatch)
bad_resume["continuity"] = "resume_same_run"
denied_resume = adapter().preflight(bad_resume)
ok(denied_resume["allowed"] is False
   and denied_resume["reason"] == "same_run_resume_unsupported",
   "dispatch cannot request unsupported exact resume")

wrong_branch = deepcopy(dispatch)
wrong_branch["branch"] = "cursor/OTHER-1-unrelated"
ok(adapter().preflight(wrong_branch)["reason"] == "cursor_task_branch_required",
   "Cursor dispatch branch must be scoped to the assigned task")
wrong_mcp = deepcopy(dispatch)
wrong_mcp["mcp_access"]["endpoint"] = "https://attacker.example/mcp"
ok(adapter().preflight(wrong_mcp)["reason"] == "untrusted_mcp_endpoint",
   "scoped Switchboard token cannot be sent to an untrusted MCP endpoint")
expired = deepcopy(dispatch)
expired["mcp_access"]["expires_at"] = time.time() - 1
ok(adapter().preflight(expired)["reason"] == "scoped_mcp_token_expiry_invalid",
   "expired scoped MCP token fails before provider launch")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
