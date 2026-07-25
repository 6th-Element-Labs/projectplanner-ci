#!/usr/bin/env python3
"""ADAPTER-28 smoke: start a provider CLI from the resolved isolated workspace.

Unlike tests/test_adapter28_workspace_launch.py, this starts a *real process*
through the real supervisor and then reads that process's own working directory
from the operating system.  Argv assertions cannot prove a CLI actually ran in
the workspace; ``lsof``/``/proc`` can.

    python3 scripts/adapter28_workspace_launch_smoke.py            # stub CLI
    ADAPTER28_REAL_CLI=codex python3 scripts/...smoke.py           # real codex

The default runs a tiny recording executable so the smoke is hermetic and safe
to run in CI.  ``ADAPTER28_REAL_CLI`` substitutes the operator's actual provider
binary; the process is killed as soon as its cwd has been read, but it is a real
CLI boot against a real account, so it stays opt-in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "adapters"))
sys.path.insert(0, str(HERE / "src"))

SLUG = "6th-Element-Labs/ActionEngine"
REAL_CLI = os.environ.get("ADAPTER28_REAL_CLI", "").strip()


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()


def action_engine_remote(root: Path):
    source = root / "sources" / "ActionEngine"
    source.mkdir(parents=True)
    git("init", "-b", "main", cwd=source)
    git("config", "user.email", "adapter28@example.test", cwd=source)
    git("config", "user.name", "ADAPTER-28", cwd=source)
    for relative in ("src/actionengine/__init__.py", "src/actionengine/app.py",
                     "src/actionengine/engine.py", "tests/test_app.py",
                     "pyproject.toml", "README.md"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative}\n", encoding="utf-8")
    git("add", "-A", cwd=source)
    git("commit", "-m", "actionengine fixture", cwd=source)
    sha = git("rev-parse", "HEAD", cwd=source)
    remote = root / "remotes" / f"{SLUG}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--bare", str(source), str(remote))
    return remote.as_uri(), sha


def recording_cli(root: Path) -> Path:
    """A stand-in provider CLI that reports its own cwd and env, then idles."""
    path = root / "fake-provider-cli"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "print(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:],\n"
        "                  'assignment': os.environ.get("
        "'SWITCHBOARD_CONNECT_ASSIGNMENT_ID', ''),\n"
        "                  'runner': os.environ.get("
        "'SWITCHBOARD_CONNECT_RUNNER_ID', ''),\n"
        "                  'receipt': os.environ.get("
        "'SWITCHBOARD_WORKSPACE_RECEIPT', '')}), flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


def process_cwd(pid: int) -> str:
    """The kernel's answer to 'where is this process actually running'."""
    linux = Path(f"/proc/{pid}/cwd")
    if linux.exists():
        return str(linux.resolve())
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True, capture_output=True, check=False).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:].strip()
    return ""


def main() -> int:
    from switchboard.application.commands.execution_context import with_generation
    from switchboard.connect.execution_assignment import build_execution_assignment
    import agent_host

    root = Path(tempfile.mkdtemp(prefix="adapter28-smoke-"))
    state = root / "state"
    os.environ["PM_AGENT_HOST_STATE_DIR"] = str(state)
    os.environ["PM_AGENT_HOST_REPO_CACHE_ROOT"] = str(state / "cache")
    os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"] = str(state / "workspaces")
    os.environ["PM_RUNNER_DIR"] = str(state / "runner")
    os.environ["PM_AGENT_HOST_RUNNER_DIR"] = str(state / "runner")

    remote, sha = action_engine_remote(root)
    executable = REAL_CLI or str(recording_cli(root))
    os.environ["PM_CONNECT_CODEX_EXECUTABLE"] = executable
    if not REAL_CLI:
        os.environ["PM_CONNECT_CODEX_ARGS"] = "--smoke"

    context = with_generation({
        "schema": "switchboard.execution_context.v1",
        "project_id": "switchboard",
        "task_id": "ADAPTER-28",
        "repo_role": "canonical",
        "repository": SLUG,
        "default_branch": "main",
        "base_sha": sha,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"requested": "codex", "registry_name": "codex"},
        "provider": {
            "provider": "openai-codex",
            "connection_reference": "provider-smoke",
            "credential_version": 1,
            "lifecycle_state": "active",
            "revocation_state": "",
        },
        "authority_digest": "sha256:adapter28-smoke",
    }, 1)
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": "implementation", "head_sha": "", "pr_number": 0, "pr_url": "",
        "ttl_seconds": 7200, "execution_id": "execlease-smoke",
        "generation": 1, "fence_epoch": 1,
    }
    assignment = {
        "schema": "switchboard.connect.assignment.v1",
        "assignment_id": "assignment-smoke",
        "principal_ref": "agent/codex/adapter-28-smoke",
        "work_ref": "task:switchboard:ADAPTER-28",
        "runtime": "codex", "provider": "openai",
        "workspace_ref": "repo:canonical", "queued_at": time.time(),
        "limits": {"max_runtime_seconds": 600, "spend_limit_microunits": 0,
                   "memory_limit_bytes": 0},
    }
    wake = {
        "wake_id": "wake-smoke", "task_id": "ADAPTER-28",
        "selector": {"runtime": "codex", "task_id": "ADAPTER-28",
                     "agent_id": "agent/codex/adapter-28-smoke"},
        "policy": {
            "mode": "connect", "assignment": assignment, "lifecycle": lifecycle,
            "execution_context": context,
            "execution_assignment": build_execution_assignment(
                task_id="ADAPTER-28", assignment=assignment,
                lifecycle=lifecycle),
            "account_binding": {"credential_reference": "provider-smoke",
                                "provider": "openai-codex"},
        },
    }
    inventory = {
        "host_id": "host/adapter28-smoke",
        "repo_root": str(HERE),
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        "runtimes": [{
            "runtime": "codex", "provider": "openai", "lanes": ["ADAPTER"],
            "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
    }

    saved_materialize = agent_host.materialize_repository_workspace
    saved_verify = agent_host.verify_repository_workspace
    saved_token = agent_host._issue_connect_session_mcp_token
    agent_host.materialize_repository_workspace = (
        lambda *a, **k: saved_materialize(*a, **{**k, "remote_url": remote}))
    agent_host.verify_repository_workspace = (
        lambda *a, **k: saved_verify(*a, **{**k, "remote_url": remote}))
    agent_host._issue_connect_session_mcp_token = (
        lambda *_a, **_k: "dst-adapter28-smoke")

    failures = []

    def check(condition, label):
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")
        if not condition:
            failures.append(label)

    runner_id = "run_adapter28_smoke"
    try:
        rec = agent_host.launch(wake, inventory, runner_session_id=runner_id)
        print(json.dumps({"launch": {k: rec.get(k) for k in
                                     ("started", "pid", "cwd", "reason")}},
                         indent=2))
        pid = int(rec.get("pid") or 0)
        workspace = Path(rec.get("cwd") or "")
        check(bool(pid), "a real supervised process was started")
        if not pid:
            return 1
        time.sleep(1.5)
        actual = process_cwd(pid)
        check(bool(actual) and Path(actual).resolve() == workspace.resolve(),
              f"the running CLI's own cwd is the isolated workspace "
              f"(kernel says {actual!r})")
        check(Path(actual).resolve() != HERE.resolve(),
              "the running CLI is not in the host application checkout")
        check(git("rev-parse", "HEAD", cwd=workspace) == sha,
              "the running CLI sees the exact Execution Context base SHA")
        check(git("remote", "get-url", "origin", cwd=workspace) == remote,
              "the running CLI sees the Execution Context origin")
        check((workspace / "src" / "actionengine" / "engine.py").is_file(),
              "the ActionEngine-shaped repository is present in the workspace")

        log = Path(rec.get("log_path") or "")
        if not REAL_CLI and log.is_file():
            first = next((line for line in log.read_text(
                encoding="utf-8", errors="replace").splitlines()
                if line.strip().startswith("{")), "")
            reported = json.loads(first) if first else {}
            check(reported.get("cwd", "") and
                  Path(reported["cwd"]).resolve() == workspace.resolve(),
                  "the CLI itself reports the workspace as its cwd")
            check(reported.get("runner") == runner_id
                  and reported.get("assignment") == "assignment-smoke",
                  "the CLI receives the runner and assignment identity")
            check(bool(reported.get("receipt"))
                  and Path(reported["receipt"]).is_file(),
                  "the CLI receives a readable workspace receipt")

        agent_host.supervisor_action(
            "kill", runner_id, {"grace_seconds": 2.0, "reason": "smoke"})
        revoked = agent_host.revoke_runner_workspace(runner_id, "smoke-teardown")
        check(bool(revoked) and revoked.get("revoked") is True
              and not workspace.exists(),
              "teardown revokes the workspace so further writes are denied")
    finally:
        agent_host.materialize_repository_workspace = saved_materialize
        agent_host.verify_repository_workspace = saved_verify
        agent_host._issue_connect_session_mcp_token = saved_token
        subprocess.run(["pkill", "-f", runner_id], check=False,
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)

    mode = f"real CLI ({REAL_CLI})" if REAL_CLI else "recording stub CLI"
    print(f"\nADAPTER-28 launch smoke [{mode}]: "
          f"{'PASS' if not failures else 'FAIL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
