import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "switchboard_pr_gate", ROOT / "scripts" / "switchboard_pr_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


calls = []


def fake_request(method, path, *, token, body=None):
    calls.append({"method": method, "path": path, "token": token, "body": body})
    return {"ok": True}


original_request = gate._github_request
try:
    gate._github_request = fake_request
    gate.post_status(
        "6th-Element-Labs/projectplanner",
        "abc123",
        "success",
        context="Switchboard CI / VM gate",
        description="x" * 200,
        target_url="https://github.com/6th-Element-Labs/projectplanner/pull/18",
        token="token-value",
    )
finally:
    gate._github_request = original_request

call = calls[0]
body = call["body"]
ok(call["method"] == "POST", "commit status uses POST")
ok(call["path"] == "repos/6th-Element-Labs/projectplanner/statuses/abc123",
   "commit status targets the PR head SHA")
ok(call["token"] == "token-value", "commit status passes the configured token")
ok(body["state"] == "success", "commit status preserves the success state")
ok(body["context"] == "Switchboard CI / VM gate",
   "commit status uses the documented VM-gate context")
ok(len(body["description"]) <= 140, "commit status description is GitHub-safe")
ok(body["target_url"].endswith("/pull/18"), "commit status links back to the PR")
tag_a = gate._run_tag(31, "abcdef1234567890")
tag_b = gate._run_tag(31, "abcdef1234567890")
ok(tag_a.startswith("pr-31-abcdef123456"), "gate run tag preserves PR and SHA prefix")
ok(tag_a != tag_b, "gate run tag is unique for concurrent invocations")

try:
    gate.post_status(
        "6th-Element-Labs/projectplanner",
        "abc123",
        "pending",
        context="Switchboard CI / VM gate",
        description="running",
        token="",
    )
except gate.GateError:
    print("  PASS  missing token fails closed")
else:
    raise AssertionError("missing token should fail closed")

with tempfile.TemporaryDirectory(prefix="switchboard-pr-gate-") as tmp:
    log_path = Path(tmp) / "gate.log"
    gate._write_preflight_log(log_path, Path(tmp), {
        "status": "red",
        "project": "switchboard",
        "intended_branch": "master",
        "repo_path": tmp,
        "target_ref": "HEAD",
        "target_sha": "abc123",
        "upstream_ref": "origin/master",
        "upstream_sha": "def456",
        "branch_distance": {"behind": 2, "ahead": 1},
        "dirty": True,
        "dirty_count": 1,
        "findings": [{"severity": "high", "code": "target_branch_behind_upstream",
                      "detail": "Target is behind.", "blocking": True}],
    })
    text = log_path.read_text(encoding="utf-8")
    ok("Switchboard review git preflight" in text,
       "gate log includes review preflight header")
    ok("target_branch_behind_upstream" in text and "branch_distance" in text,
       "gate log includes stale-branch evidence")

print("\n12 passed, 0 failed")
