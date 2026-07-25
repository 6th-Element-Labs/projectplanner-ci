#!/usr/bin/env python3
"""ADAPTER-28: provider CLIs launch only from the resolved isolated workspace.

ADAPTER-27 proved a workspace can be materialized.  This proves the launch path
*uses* it and nothing else: the CLI's cwd is the verified checkout rather than
the host's own application repo, one generation owns the workspace and the
provider credential, a workspace that stopped being authorized refuses before
any process exists, retries reuse instead of re-cloning, and teardown denies
further writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

STATE = Path(tempfile.mkdtemp(prefix="adapter28-state-"))
os.environ["PM_AGENT_HOST_STATE_DIR"] = str(STATE)
os.environ["PM_AGENT_HOST_REPO_CACHE_ROOT"] = str(STATE / "cache")
os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"] = str(STATE / "workspaces")

from adapters import agent_host  # noqa: E402

# agent_host resolves the materializer as a top-level module; importing it as
# ``adapters.repository_workspace`` here would create a second module object
# whose exception classes agent_host's handlers would not catch.
import repository_workspace  # noqa: E402
from repository_workspace import (  # noqa: E402
    MaterializedWorkspace,
    WorkspaceMaterializationError,
    materialize,
    revoke,
    safe_receipt,
    verify,
)
from switchboard.application.commands.execution_context import (  # noqa: E402
    ExecutionContextError,
    require_generation_binding,
    with_generation,
)
from switchboard.connect.execution_assignment import (  # noqa: E402
    build_execution_assignment,
)

passed = 0
failed = 0
SLUG = "6th-Element-Labs/ActionEngine"


def ok(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def refuses(code, fn, label):
    """Assert one typed refusal rather than a generic exception."""
    try:
        fn()
    except (WorkspaceMaterializationError, ExecutionContextError) as exc:
        ok(exc.code == code, f"{label} (got {exc.code})")
        return
    except ValueError as exc:
        ok(code in str(exc), f"{label} (got {exc})")
        return
    ok(False, f"{label} (no refusal raised)")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()


def action_engine_remote(root: Path):
    """A remote shaped like the real ActionEngine checkout, not a bare README."""
    source = root / "sources" / "ActionEngine"
    source.mkdir(parents=True)
    git("init", "-b", "main", cwd=source)
    git("config", "user.email", "adapter28@example.test", cwd=source)
    git("config", "user.name", "ADAPTER-28", cwd=source)
    for relative in ("src/actionengine/__init__.py", "src/actionengine/app.py",
                     "tests/test_app.py", "pyproject.toml", "README.md"):
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


def context(sha, *, task="ADAPTER-28", generation=1, project="switchboard",
            provider_state="active", revocation=""):
    return with_generation({
        "schema": "switchboard.execution_context.v1",
        "project_id": project,
        "task_id": task,
        "repo_role": "canonical",
        "repository": SLUG,
        "default_branch": "main",
        "base_sha": sha,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"requested": "codex", "registry_name": "codex"},
        "provider": {
            "provider": "openai-codex",
            "connection_reference": "provider-actionengine",
            "credential_version": 3,
            "lifecycle_state": provider_state,
            "revocation_state": revocation,
        },
        "scm": {"provider": "github_app", "connection_reference": "scm-test"},
        "authority_digest": "sha256:adapter28-authority",
    }, generation)


def connect_wake(ctx, *, execution_id="execlease-adapter28", generation=1,
                 runtime="codex", credential_reference="provider-actionengine"):
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": "implementation", "head_sha": "", "pr_number": 0, "pr_url": "",
        "ttl_seconds": 7200, "execution_id": execution_id,
        "generation": generation, "fence_epoch": 1,
    }
    assignment = {
        "schema": "switchboard.connect.assignment.v1",
        "assignment_id": "assignment-adapter28",
        "principal_ref": f"agent/{runtime}/adapter-28",
        "work_ref": "task:switchboard:ADAPTER-28",
        "runtime": runtime,
        "provider": {"codex": "openai", "claude-code": "anthropic",
                     "cursor": "cursor"}.get(runtime, runtime),
        "workspace_ref": "repo:canonical", "queued_at": 1.0,
        "limits": {"max_runtime_seconds": 7200, "spend_limit_microunits": 0,
                   "memory_limit_bytes": 0},
    }
    policy = {
        "mode": "connect",
        "assignment": assignment,
        "lifecycle": lifecycle,
        "execution_context": ctx,
        "execution_assignment": build_execution_assignment(
            task_id="ADAPTER-28", assignment=assignment, lifecycle=lifecycle),
    }
    if credential_reference:
        policy["account_binding"] = {
            "credential_reference": credential_reference,
            "provider": "openai-codex",
        }
    return {
        "wake_id": "wake-adapter28", "task_id": "ADAPTER-28",
        "selector": {"runtime": runtime, "task_id": "ADAPTER-28",
                     "agent_id": f"agent/{runtime}/adapter-28"},
        "policy": policy,
    }


def host_inventory(runtime="codex"):
    return {
        "host_id": "host/adapter28",
        # Deliberately a real directory that is NOT the workspace: a launch that
        # falls back to inventory.repo_root must be visible as such.
        "repo_root": str(ROOT),
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        "runtimes": [{
            "runtime": runtime,
            "provider": {"codex": "openai", "claude-code": "anthropic",
                         "cursor": "cursor"}.get(runtime, runtime),
            "lanes": ["ADAPTER"],
            "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
    }


class Launcher:
    """Run agent_host.launch with the supervisor and token mint intercepted."""

    def __init__(self, remote):
        self.remote = remote
        self.calls = []

    def __enter__(self):
        self._saved = (agent_host.subprocess.run,
                       agent_host._issue_connect_session_mcp_token,
                       repository_workspace.materialize)
        outer = self

        real_run = self._saved[0]

        def fake_run(command, **kwargs):
            # agent_host.subprocess is the subprocess module itself, so this
            # patch is global: git calls made by the real materializer must
            # still reach the real subprocess.run. Only the supervisor start is
            # intercepted.
            if not any("supervisor.py" in str(part) for part in command):
                return real_run(command, **kwargs)
            outer.calls.append({"command": list(command),
                                "env": dict(kwargs.get("env") or {})})

            class Receipt:
                returncode = 0
                stdout = json.dumps({"runner_session_id": "run_adapter28",
                                     "status": "running", "pid": 4242})
                stderr = ""
            return Receipt()

        # The host never receives a remote URL from the Execution Context; the
        # repository slug resolves to github.com in production.  Pin the fixture
        # remote here so the launch path itself stays untouched.
        def pinned_materialize(*args, **kwargs):
            return outer._saved[2](*args, **{**kwargs, "remote_url": outer.remote})

        agent_host.subprocess.run = fake_run
        agent_host._issue_connect_session_mcp_token = (
            lambda *_a, **_k: "dst-adapter28")
        agent_host.materialize_repository_workspace = pinned_materialize
        agent_host.verify_repository_workspace = (
            lambda *a, **k: verify(*a, **{**k, "remote_url": outer.remote}))
        return self

    def __exit__(self, *_exc):
        (agent_host.subprocess.run,
         agent_host._issue_connect_session_mcp_token, _) = self._saved
        agent_host.materialize_repository_workspace = repository_workspace.materialize
        agent_host.verify_repository_workspace = repository_workspace.verify
        return False

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def cwd_of(call):
    command = call["command"]
    return command[command.index("--cwd") + 1]


def test_connect_launches_from_the_verified_workspace(root):
    remote, sha = action_engine_remote(root)
    wake = connect_wake(context(sha))
    with Launcher(remote) as launcher:
        rec = agent_host.launch(
            wake, host_inventory(), runner_session_id="run_adapter28")

    workspace = Path(rec["cwd"])
    ok(rec.get("runner_session_id") == "run_adapter28",
       "connect launch returns the supervisor receipt")
    ok(cwd_of(launcher.last) == str(workspace),
       "supervisor is given the materialized workspace as cwd")
    ok(str(workspace) != str(ROOT)
       and not str(workspace).startswith(str(ROOT)),
       "launch cwd is never the host application checkout (inventory.repo_root)")
    ok(workspace.resolve().is_relative_to(
        Path(os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"]).resolve()),
       "launch cwd lives under the configured isolated workspace root")

    # cwd / origin / branch / base / project / claim / runner / credential all
    # describe one execution.
    receipt = rec["metadata"]["workspace_receipt"]
    ok(git("rev-parse", "HEAD", cwd=workspace) == sha
       and receipt["base_sha"] == sha,
       "workspace HEAD and receipt agree with the Execution Context base SHA")
    ok(git("branch", "--show-current", cwd=workspace) == receipt["branch"]
       and receipt["execution_id"] in receipt["branch"]
       and receipt["branch"].endswith("-g1"),
       "branch is scoped to the exact execution id and generation")
    ok(git("remote", "get-url", "origin", cwd=workspace) == remote,
       "workspace origin is the Execution Context repository")
    ok(receipt["project_id"] == "switchboard"
       and receipt["task_id"] == "ADAPTER-28"
       and receipt["generation"] == 1,
       "receipt carries the project, task, and generation of this execution")
    ok((workspace / "src" / "actionengine" / "app.py").is_file(),
       "the ActionEngine-shaped repository is actually checked out")
    env = launcher.last["env"]
    ok(env["SWITCHBOARD_CONNECT_RUNNER_ID"] == "run_adapter28"
       and env["PM_MCP_TOKEN"] == "dst-adapter28"
       and "SWITCHBOARD_TOKEN" not in env,
       "control-plane identity and minted task principal reach the CLI")
    ok(json.loads(env["SWITCHBOARD_EXECUTION_CONTEXT_JSON"])["generation"] == 1
       and json.loads(
           env["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"])["generation"] == 1,
       "the CLI receives one generation across context and assignment")
    ok(Path(env["SWITCHBOARD_WORKSPACE_RECEIPT"]).is_file(),
       "the durable workspace receipt is handed to the session")
    return wake, remote, sha, workspace


def test_receipts_published_centrally_are_safe(root):
    remote, sha = action_engine_remote(root)
    with Launcher(remote):
        rec = agent_host.launch(
            connect_wake(context(sha)), host_inventory(),
            runner_session_id="run_adapter28")
    receipt = rec["metadata"]["workspace_receipt"]
    encoded = json.dumps(receipt)
    ok("cache_key" not in receipt
       and os.environ["PM_AGENT_HOST_REPO_CACHE_ROOT"] not in encoded,
       "published receipt does not leak the host repository cache layout")
    ok(receipt["cache_created"] in (True, False)
       and receipt["cache_quarantined"] in (True, False),
       "cache state is published as a boolean, not a host path")
    ok(receipt["remote"] == repository_workspace._redacted_remote(remote),
       "published remote is the redacted form")
    leaked = safe_receipt(
        {"remote": "https://user:pw@example.test/a/b.git"})["remote"]
    ok("pw" not in leaked and "user" not in leaked
       and leaked == "https://example.test/a/b",
       "a receipt remote is republished without any embedded credential")


def test_missing_workspace_refuses_before_any_process(root):
    remote, sha = action_engine_remote(root)
    import shutil

    wake = connect_wake(context(sha), execution_id="execlease-missing")
    with Launcher(remote) as launcher:
        materialize_only = agent_host.materialize_repository_workspace

        def vanishing_materialize(*args, **kwargs):
            """Materialize, then lose the checkout before the process starts."""
            workspace = materialize_only(*args, **kwargs)
            shutil.rmtree(workspace.path)
            return workspace

        agent_host.materialize_repository_workspace = vanishing_materialize
        rec = agent_host.launch(
            wake, host_inventory(), runner_session_id="run_missing")

    ok(rec.get("started") is False and rec.get("reason") == "workspace_missing",
       f"a workspace lost before start refuses by name (got {rec.get('reason')})")
    ok(rec.get("failure_class") == "failed_gate"
       and rec.get("workspace_verification", {}).get("error") == "workspace_missing",
       "the refusal carries the original failing signal, not a generic error")
    ok(launcher.calls == [], "no supervisor process is started on refusal")


def test_rewound_repointed_and_revoked_workspaces_refuse(root):
    remote, sha = action_engine_remote(root)
    ctx = context(sha)
    kwargs = {
        "execution_context": ctx, "task_id": "ADAPTER-28",
        "execution_id": "execlease-drift",
        "branch": "agent/switchboard/ADAPTER-28/execlease-drift-g1",
        "cache_root": root / "cache", "workspace_root": root / "workspaces",
        "remote_url": remote,
    }
    workspace = materialize(**kwargs)
    ok(verify(**kwargs).path == workspace.path,
       "an untouched authorized workspace verifies")

    # A materialized workspace is a fresh clone with no committer identity, and
    # a CI runner has no global one either. Supply it per-command rather than
    # writing config into the workspace under test.
    git("-c", "user.email=adapter28@example.test", "-c", "user.name=ADAPTER-28",
        "commit", "--allow-empty", "-m", "drift", cwd=workspace.path)
    refuses("workspace_head_mismatch", lambda: verify(**kwargs),
            "a workspace moved off the exact base SHA refuses")
    git("reset", "--hard", sha, cwd=workspace.path)

    git("checkout", "-b", "someone-elses-branch", cwd=workspace.path)
    refuses("workspace_branch_mismatch", lambda: verify(**kwargs),
            "a workspace on another branch refuses")
    git("checkout", kwargs["branch"], cwd=workspace.path)

    other, _ = action_engine_remote(root / "other")
    git("remote", "set-url", "origin", other, cwd=workspace.path)
    refuses("workspace_origin_mismatch", lambda: verify(**kwargs),
            "a workspace re-pointed at another origin refuses")
    git("remote", "set-url", "origin", remote, cwd=workspace.path)

    receipt = json.loads(workspace.receipt_path.read_text())
    workspace.receipt_path.write_text(
        json.dumps({**receipt, "base_sha": "0" * 40}))
    refuses("workspace_receipt_mismatch", lambda: verify(**kwargs),
            "a receipt edited to disagree with the Execution Context refuses")
    workspace.receipt_path.write_text(json.dumps(receipt))

    result = revoke(workspace, reason="operator-revoked")
    ok(result["revoked"] is True and not workspace.path.exists(),
       "revocation removes the workspace so a live child cannot keep writing")
    refuses("workspace_revoked", lambda: verify(**kwargs),
            "a revoked workspace refuses to be reused")
    again = revoke(workspace, reason="operator-revoked")
    ok(again["already_revoked"] is True, "revoking twice is a no-op, not an error")


def test_teardown_never_acts_outside_the_workspace_root(root):
    outside = root / "not-a-workspace"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("important", encoding="utf-8")
    forged = MaterializedWorkspace(
        path=outside, branch="b", head_sha="0" * 40,
        cache_path=root / "cache.git",
        receipt_path=root / "workspaces" / ".receipts" / "forged.json",
        receipt={}, workspace_root=root / "workspaces")
    refuses("workspace_path_escape",
            lambda: revoke(forged, reason="forged-binding"),
            "teardown refuses a receipt naming a path outside the workspace root")
    ok((outside / "keep.txt").is_file(),
       "the unrelated directory is left untouched")

    binding = agent_host._workspace_binding_path("run_forged")
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(json.dumps({
        "runner_session_id": "run_forged",
        "workspace_path": str(outside),
        "receipt_path": str(root / "forged.json"),
    }), encoding="utf-8")
    result = agent_host.revoke_runner_workspace("run_forged", "kill")
    ok(result and result.get("error") == "workspace_root_missing",
       "a runner binding with no recorded root revokes nothing")
    ok((outside / "keep.txt").is_file(),
       "a binding without a root cannot delete an arbitrary directory")


def test_retries_dedupe_and_teardown_revokes(root):
    remote, sha = action_engine_remote(root)
    wake = connect_wake(context(sha), execution_id="execlease-retry")
    with Launcher(remote):
        first = agent_host.launch(
            wake, host_inventory(), runner_session_id="run_retry")
        second = agent_host.launch(
            wake, host_inventory(), runner_session_id="run_retry")
    ok(first["cwd"] == second["cwd"],
       "a retry of the same execution reuses the same workspace")
    ok(first["metadata"]["workspace_receipt"]["created_at"]
       == second["metadata"]["workspace_receipt"]["created_at"],
       "a retry reuses the existing checkout instead of re-cloning it")

    workspace = Path(first["cwd"])
    binding = agent_host._workspace_binding_path("run_retry")
    ok(binding.is_file(), "the runner's workspace binding is durable")
    revoked = agent_host.revoke_runner_workspace("run_retry", "runner_killed")
    ok(revoked and revoked.get("revoked") is True and not workspace.exists(),
       "terminating the runner revokes its workspace")
    ok(not binding.exists(), "the binding is dropped once revoked")
    ok(agent_host.revoke_runner_workspace("run_never_launched", "kill") is None,
       "runners that own no isolated workspace tear down as a no-op")


def test_one_generation_owns_workspace_credential_and_identity(root):
    remote, sha = action_engine_remote(root)
    inventory = host_inventory()

    stale = connect_wake(context(sha, generation=2), generation=1)
    refuses("generation mismatch",
            lambda: agent_host.launch_command(
                stale, inventory, runner_session_id="run_gen",
                workspace_path=str(root)),
            "a context from another generation refuses to launch")
    refuses("execution_generation_mismatch",
            lambda: require_generation_binding(
                context(sha, generation=2), generation=1),
            "the generation gate itself names the disagreement")

    forged = connect_wake(context(sha))
    forged["policy"]["execution_context"] = {
        **forged["policy"]["execution_context"], "base_sha": "9" * 40}
    refuses("execution_context_digest_mismatch",
            lambda: agent_host.launch_command(
                forged, inventory, runner_session_id="run_gen",
                workspace_path=str(root)),
            "a context edited after signing refuses to launch")

    revoked = connect_wake(context(sha, revocation="revoked"))
    refuses("provider_connection_revoked",
            lambda: agent_host.launch_command(
                revoked, inventory, runner_session_id="run_gen",
                workspace_path=str(root)),
            "a revoked provider credential refuses to launch")

    inactive = connect_wake(context(sha, provider_state="suspended"))
    refuses("provider_connection_revoked",
            lambda: agent_host.launch_command(
                inactive, inventory, runner_session_id="run_gen",
                workspace_path=str(root)),
            "an inactive provider connection refuses to launch")

    other = connect_wake(context(sha), credential_reference="provider-someone-else")
    refuses("provider_credential_reference_mismatch",
            lambda: agent_host.launch_command(
                other, inventory, runner_session_id="run_gen",
                workspace_path=str(root)),
            "a host credential from another connection refuses to launch")

    bound = require_generation_binding(
        context(sha), generation=1,
        credential_reference="provider-actionengine")
    ok(bound["credential_version"] == 3 and bound["generation"] == 1
       and bound["base_sha"] == sha,
       "the accepted binding names the credential version it launched with")


def test_only_supported_provider_clis_launch(root):
    remote, sha = action_engine_remote(root)
    inventory = host_inventory("codex")
    inventory["runtimes"].append({
        "runtime": "aider", "provider": "aider", "lanes": ["ADAPTER"],
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    })
    wake = connect_wake(context(sha), runtime="aider")
    refuses("no supported provider CLI",
            lambda: agent_host.launch_command(
                wake, inventory, runner_session_id="run_unsupported",
                workspace_path=str(root)),
            "an unsupported runtime refuses instead of guessing '<runtime> --prompt'")
    ok(set(agent_host.CONNECT_RUNTIME_DEFAULTS) == {"codex", "claude-code", "cursor"},
       "the supported provider CLI set is explicit")


def test_launch_has_no_repo_root_fallback_for_connect():
    source = (Path(__file__).parents[1] / "adapters" / "agent_host.py").read_text(
        encoding="utf-8")
    connect_block = source[source.index("def launch_command("):
                           source.index("def launch(")]
    ok('inventory["repo_root"]' in connect_block
       and 'spec.cwd if mode == "connect"' in connect_block,
       "repo_root remains the cwd only for non-Connect modes")


with tempfile.TemporaryDirectory(prefix="adapter28-") as temporary:
    base = Path(temporary)
    test_connect_launches_from_the_verified_workspace(base / "launch")
    test_receipts_published_centrally_are_safe(base / "receipts")
    test_missing_workspace_refuses_before_any_process(base / "missing")
    test_rewound_repointed_and_revoked_workspaces_refuse(base / "drift")
    test_teardown_never_acts_outside_the_workspace_root(base / "containment")
    test_retries_dedupe_and_teardown_revokes(base / "retry")
    test_one_generation_owns_workspace_credential_and_identity(base / "generation")
    test_only_supported_provider_clis_launch(base / "runtimes")
test_launch_has_no_repo_root_fallback_for_connect()

print(f"\nADAPTER-28 workspace launch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
