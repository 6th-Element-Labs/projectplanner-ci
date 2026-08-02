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
    with_checkout_sha,
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
                 runtime="codex", credential_reference="provider-actionengine",
                 role="implementation", head_sha="", pr_branch=""):
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": role, "head_sha": head_sha, "pr_number": 0, "pr_url": "",
        "ttl_seconds": 7200, "execution_id": execution_id,
        "generation": generation, "fence_epoch": 1,
    }
    if role in {"review_merge", "remediation"}:
        lifecycle["pr_branch"] = pr_branch
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
            task_id="ADAPTER-28", assignment=assignment, lifecycle=lifecycle,
            execution_context=ctx),
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


def test_review_launches_at_exact_pr_head_not_canonical_base(root):
    remote, base_sha = action_engine_remote(root)
    writer = root / "review-writer"
    git("clone", remote, str(writer))
    git("config", "user.email", "adapter28@example.test", cwd=writer)
    git("config", "user.name", "ADAPTER-28", cwd=writer)
    branch = "agent/switchboard/ADAPTER-28/existing-pr"
    git("checkout", "-b", branch, cwd=writer)
    evidence = writer / "review-head.md"
    evidence.write_text("exact review head\n", encoding="utf-8")
    git("add", "review-head.md", cwd=writer)
    git("commit", "-m", "create exact review head", cwd=writer)
    review_sha = git("rev-parse", "HEAD", cwd=writer)
    git("push", "origin", branch, cwd=writer)

    exact_context = with_checkout_sha(context(base_sha), review_sha)
    wake = connect_wake(
        exact_context,
        execution_id="execlease-adapter28-review",
        role="review_merge",
        head_sha=review_sha,
        pr_branch=branch,
    )
    with Launcher(remote) as launcher:
        rec = agent_host.launch(
            wake, host_inventory(), runner_session_id="run_adapter28_review")

    workspace = Path(rec["cwd"])
    receipt = rec["metadata"]["workspace_receipt"]
    ok(
        git("rev-parse", "HEAD", cwd=workspace) == review_sha
        and receipt["checkout_sha"] == review_sha
        and receipt["base_sha"] == base_sha,
        "fresh review workspace preserves canonical base and checks out exact PR head",
    )
    ok(
        git("branch", "--show-current", cwd=workspace) == branch
        and (workspace / "review-head.md").is_file(),
        "review generation opens the persisted PR branch at its assigned head",
    )
    ok(bool(launcher.calls), "exact-head review reaches the supervisor")

    mismatched = connect_wake(
        with_checkout_sha(context(base_sha), base_sha),
        execution_id="execlease-adapter28-wrong-review",
        role="review_merge",
        head_sha=review_sha,
        pr_branch=branch,
    )
    with Launcher(remote) as refused_launcher:
        refused = agent_host.launch(
            mismatched, host_inventory(), runner_session_id="run_wrong_review")
    ok(
        refused.get("started") is False
        and refused.get("reason") == "workspace_exact_head_mismatch",
        "mismatched review checkout fails closed by name before spawn",
    )
    ok(refused_launcher.calls == [], "mismatched review starts no supervisor")


def test_downstream_launch_proves_current_tip_contains_upstream_merge(root):
    remote, old_sha = action_engine_remote(root)
    writer = root / "upstream-writer"
    git("clone", remote, str(writer))
    git("config", "user.email", "adapter28@example.test", cwd=writer)
    git("config", "user.name", "ADAPTER-28", cwd=writer)
    (writer / "upstream.md").write_text("merged upstream output\n", encoding="utf-8")
    git("add", "upstream.md", cwd=writer)
    git("commit", "-m", "merge upstream dependency", cwd=writer)
    upstream_sha = git("rev-parse", "HEAD", cwd=writer)
    git("push", "origin", "main", cwd=writer)

    stale_context = with_checkout_sha(
        context(old_sha),
        old_sha,
        require_default_branch_tip=True,
        required_ancestor_shas=[upstream_sha],
    )
    stale = connect_wake(
        stale_context, execution_id="execlease-adapter28-stale-downstream")
    with Launcher(remote) as launcher:
        refused = agent_host.launch(
            stale, host_inventory(), runner_session_id="run_stale_downstream")
    ok(
        refused.get("started") is False
        and refused.get("reason") == "workspace_default_branch_tip_mismatch",
        "stale downstream canonical assignment fails before spawn",
    )
    ok(launcher.calls == [], "stale downstream assignment starts no supervisor")

    current_context = with_checkout_sha(
        context(upstream_sha),
        upstream_sha,
        require_default_branch_tip=True,
        required_ancestor_shas=[upstream_sha],
    )
    current = connect_wake(
        current_context, execution_id="execlease-adapter28-current-downstream")
    with Launcher(remote) as launcher:
        rec = agent_host.launch(
            current, host_inventory(), runner_session_id="run_current_downstream")
    proof = rec["metadata"]["workspace_receipt"]["checkout_proof"]
    ok(
        rec.get("runner_session_id") == "run_adapter28"
        and proof["default_branch_tip_sha"] == upstream_sha
        and proof["required_ancestor_shas"] == [upstream_sha],
        "host receipt proves downstream checkout is current and contains upstream merge",
    )
    ok(
        (Path(rec["cwd"]) / "upstream.md").is_file(),
        "downstream runner workspace contains the upstream merged output",
    )


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
    refuses("workspace_exact_head_mismatch", lambda: verify(**kwargs),
            "a workspace moved off the assigned checkout SHA refuses")
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


def test_legacy_wake_without_context_launches_from_private_worktree(root):
    """The compatibility source path still launches, but never in repo_root.

    switchboard has never configured a project execution policy, so the server
    dispatches its Connect wakes WITHOUT an execution_context by design
    (connect_dispatch.enqueue_task's restored COORD-47 contract). The enrolled
    checkout supplies committed git objects, not a process cwd or an invented
    Execution Context.
    """
    remote, sha = action_engine_remote(root)
    source = root / "sources" / "ActionEngine"
    git("remote", "add", "origin", remote, cwd=source)
    wake = connect_wake(context(sha), execution_id="execlease-legacy")
    del wake["policy"]["execution_context"]
    wake["policy"]["execution_assignment"] = build_execution_assignment(
        task_id="ADAPTER-28", assignment=wake["policy"]["assignment"],
        lifecycle=wake["policy"]["lifecycle"])
    wake["policy"].pop("account_binding", None)
    inventory = host_inventory()
    inventory["repo_root"] = str(source)
    with Launcher(remote) as launcher:
        rec = agent_host.launch(
            wake, inventory, runner_session_id="run_legacy")
    ok(bool(rec.get("pid"))
       and rec.get("started") is not False
       and rec.get("reason") not in {"invalid_execution_identity",
                                     "execution_context_invalid"},
       f"a context-less legacy wake launches (got {rec.get('reason')})")
    workspace = Path(rec["cwd"]).resolve()
    private_root = Path(os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"]).resolve()
    ok(launcher.last is not None and cwd_of(launcher.last) == str(workspace),
       "the context-less launch uses the materialized private cwd")
    ok(workspace.is_relative_to(private_root)
       and workspace != source.resolve()
       and not workspace.is_relative_to(source.resolve()),
       "the context-less cwd is inside the private root and outside repo_root")
    receipt = rec["metadata"]["workspace_receipt"]
    ok(receipt["source"] == "repo_root"
       and receipt["isolation"] == "host_worktree"
       and receipt["generation"] == 1,
       "the receipt truthfully names host-derived isolation without context authority")
    ok("SWITCHBOARD_WORKSPACE_RECEIPT" in (launcher.last or {}).get("env", {}),
       "the compatibility workspace uses the same durable receipt lifecycle")


def test_legacy_worktrees_dedupe_isolate_and_teardown(root):
    remote, sha = action_engine_remote(root)
    source = root / "sources" / "ActionEngine"
    git("remote", "add", "origin", remote, cwd=source)
    inventory = host_inventory()
    inventory["repo_root"] = str(source)

    def legacy_wake(generation):
        wake = connect_wake(
            context(sha, generation=generation),
            execution_id="execlease-legacy-shared",
            generation=generation,
        )
        wake["policy"].pop("execution_context")
        wake["policy"]["execution_assignment"] = build_execution_assignment(
            task_id="ADAPTER-28", assignment=wake["policy"]["assignment"],
            lifecycle=wake["policy"]["lifecycle"])
        wake["policy"].pop("account_binding", None)
        return wake

    with Launcher(remote):
        first = agent_host.launch(
            legacy_wake(1), inventory, runner_session_id="run_legacy_g1")
        retry = agent_host.launch(
            legacy_wake(1), inventory, runner_session_id="run_legacy_g1")
        second_generation = agent_host.launch(
            legacy_wake(2), inventory, runner_session_id="run_legacy_g2")
    ok(first["cwd"] == retry["cwd"]
       and first["metadata"]["workspace_receipt"]["created_at"]
       == retry["metadata"]["workspace_receipt"]["created_at"],
       "a retry of one context-less generation reuses its private worktree")
    ok(first["cwd"] != second_generation["cwd"]
       and first["metadata"]["workspace_receipt"]["generation"] == 1
       and second_generation["metadata"]["workspace_receipt"]["generation"] == 2,
       "distinct generations receive distinct private worktrees")

    first_path = Path(first["cwd"])
    removed = agent_host.revoke_runner_workspace(
        "run_legacy_g1", "runner_lease_terminal")
    ok(removed and removed.get("revoked") is True and not first_path.exists(),
       "lease-owned teardown removes the registered host worktree")
    worktree_list = git("worktree", "list", "--porcelain", cwd=source)
    ok(str(first_path) not in worktree_list
       and str(second_generation["cwd"]) in worktree_list,
       "teardown prunes only the exact generation from git worktree metadata")


def test_legacy_workspace_failures_start_no_process(root):
    remote, sha = action_engine_remote(root)
    source = root / "sources" / "ActionEngine"
    git("remote", "add", "origin", remote, cwd=source)
    wake = connect_wake(context(sha), execution_id="execlease-unsafe-root")
    wake["policy"].pop("execution_context")
    wake["policy"]["execution_assignment"] = build_execution_assignment(
        task_id="ADAPTER-28", assignment=wake["policy"]["assignment"],
        lifecycle=wake["policy"]["lifecycle"])
    wake["policy"].pop("account_binding", None)
    inventory = host_inventory()
    inventory["repo_root"] = str(source)

    saved_root = os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"]
    os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"] = str(source / "workspaces")
    try:
        with Launcher(remote) as launcher:
            refused = agent_host.launch(
                wake, inventory, runner_session_id="run_unsafe_root")
    finally:
        os.environ["PM_AGENT_HOST_WORKSPACE_ROOT"] = saved_root
    ok(refused.get("started") is False
       and refused.get("reason") == "connect_workspace_root_overlaps_repo",
       "a private root overlapping repo_root refuses with a named reason")
    ok(launcher.calls == [], "overlap refusal starts no supervisor process")

    inventory["repo_root"] = str(root / "not-a-git-repository")
    Path(inventory["repo_root"]).mkdir()
    with Launcher(remote) as launcher:
        refused = agent_host.launch(
            wake, inventory, runner_session_id="run_invalid_source")
    ok(refused.get("started") is False
       and refused.get("reason") == "legacy_source_repo_invalid",
       "an invalid host source checkout fails closed by name")
    ok(launcher.calls == [], "invalid source refusal starts no supervisor process")


def test_launch_has_no_repo_root_fallback_for_connect():
    source = (Path(__file__).parents[1] / "adapters" / "agent_host.py").read_text(
        encoding="utf-8")
    connect_block = source[source.index("def launch_command("):
                           source.index("def launch(")]
    ok("if not str(workspace_path or \"\").strip()" in connect_block
       and "connect launch requires a verified private workspace" in connect_block
       and "if not execution_context else" not in connect_block,
       "Connect launch requires a private workspace and has no repo_root fallback")


with tempfile.TemporaryDirectory(prefix="adapter28-") as temporary:
    base = Path(temporary)
    test_connect_launches_from_the_verified_workspace(base / "launch")
    test_review_launches_at_exact_pr_head_not_canonical_base(base / "review-head")
    test_downstream_launch_proves_current_tip_contains_upstream_merge(
        base / "downstream-tip")
    test_receipts_published_centrally_are_safe(base / "receipts")
    test_missing_workspace_refuses_before_any_process(base / "missing")
    test_rewound_repointed_and_revoked_workspaces_refuse(base / "drift")
    test_teardown_never_acts_outside_the_workspace_root(base / "containment")
    test_retries_dedupe_and_teardown_revokes(base / "retry")
    test_one_generation_owns_workspace_credential_and_identity(base / "generation")
    test_only_supported_provider_clis_launch(base / "runtimes")
    test_legacy_wake_without_context_launches_from_private_worktree(base / "legacy")
    test_legacy_worktrees_dedupe_isolate_and_teardown(base / "legacy-lifecycle")
    test_legacy_workspace_failures_start_no_process(base / "legacy-failures")
test_launch_has_no_repo_root_fallback_for_connect()

print(f"\nADAPTER-28 workspace launch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
