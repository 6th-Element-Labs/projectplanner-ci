import importlib.util
import os
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


def fake_python(path: Path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo '{{\"executable\":\"{path}\",\"version\":{list(version)}}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


with tempfile.TemporaryDirectory(prefix="switchboard-python-select-") as tmp:
    tmp_path = Path(tmp)
    py39 = fake_python(tmp_path / "py39", (3, 9, 6))
    py312 = fake_python(tmp_path / "py312", (3, 12, 1))
    selected = gate.select_ci_python(tmp_path, explicit=str(py312))
    ok(selected["path"] == str(py312) and selected["version_text"] == "3.12.1",
       "PR gate selects an explicit supported Python runtime")
    saved_env = {k: os.environ.get(k) for k in ("PATH", "PYTHON", "SWITCHBOARD_CI_PYTHON")}
    try:
        os.environ["PATH"] = ""
        os.environ.pop("PYTHON", None)
        os.environ.pop("SWITCHBOARD_CI_PYTHON", None)
        try:
            gate.select_ci_python(tmp_path / "empty", explicit=str(py39))
        except gate.GateError as exc:
            ok("Need Python 3.10+" in str(exc) and "3.9.6" in str(exc),
               "PR gate fails closed with clear unsupported-Python diagnostic")
        else:
            raise AssertionError("unsupported Python should fail closed")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    repo_py = fake_python(tmp_path / "repo" / ".venv" / "bin" / "python", (3, 11, 8))
    saved_python = {k: os.environ.get(k) for k in ("PYTHON", "SWITCHBOARD_CI_PYTHON")}
    try:
        os.environ.pop("PYTHON", None)
        os.environ.pop("SWITCHBOARD_CI_PYTHON", None)
        selected_repo = gate.select_ci_python(tmp_path / "repo")
    finally:
        for key, value in saved_python.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    ok(selected_repo["path"] == str(repo_py) and selected_repo["source"] == "repo_venv",
       "PR gate selects repo .venv before ambient python3")

    calls = []
    original_run = gate._run
    try:
        gate._run = lambda cmd, **_kwargs: calls.append(cmd)
        log_path = tmp_path / "gate.log"
        gate.run_switchboard_gate(
            tmp_path / "worktree",
            log_path,
            timeout_s=30,
            python_runtime=selected,
        )
    finally:
        gate._run = original_run
    ok(calls and calls[0][:3] == [str(py312), "-m", "venv"],
       "PR gate creates its venv with the selected Python runtime")
    ok("version=3.12.1" in log_path.read_text(encoding="utf-8"),
       "PR gate log records selected Python version")

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

print("\n17 passed, 0 failed")
