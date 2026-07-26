#!/usr/bin/env python3
"""HARDEN-78: legacy execution defaults cannot return silently."""
from __future__ import annotations

from pathlib import Path

from path_setup import ROOT
from switchboard.application.commands import execution_context


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


connect = source("src/switchboard/application/commands/connect_dispatch.py")
host = source("adapters/agent_host.py")
sessions = source("src/switchboard/storage/repositories/work_sessions.py")
fleet = source("co_fleet.py")
merge_gate = source("src/switchboard/application/commands/merge_gate.py")
direct = source("adapters/direct_codex_session.py")

ok('"repo:canonical"' not in connect,
   "Connect cannot invent a canonical workspace reference")
ok("get_project_execution_policy(project).get(\"configured\")" not in connect,
   "Connect has no unconfigured-project scheduler branch")
ok("if context:" not in connect[connect.index("def enqueue_task("):],
   "Connect cannot make Execution Context optional")
ok("if not execution_context else" not in host
   and "if execution_context:" not in host[host.index("def launch_command("):
                                                host.index("def launch(")],
   "Agent Host cannot downgrade a Connect launch to its application checkout")
ok("PM_REPO_PATH_" not in sessions and 'os.environ.get("PM_REPO_PATH")' not in sessions,
   "managed Work Sessions cannot use project-specific repository environment authority")
ok('"GH_TOKEN", "GITHUB_TOKEN"' not in fleet,
   "fleet bootstrap cannot pass ambient GitHub tokens to task runners")
ok('default_branch") or "master"' not in merge_gate
   and 'default_branch") or "master"' not in direct,
   "merge and direct-runner paths cannot bypass topology with master")


future_repo = "ExampleOrg/future-repo-created-after-deploy"
future_sha = "a" * 40
context = execution_context.resolve(
    project="future-project",
    task_id="FUTURE-1",
    runtime="codex",
    topology_provider=lambda _project: {
        "valid": True,
        "roles": {
            "canonical": {
                "repo": future_repo,
                "default_branch": "trunk",
            },
        },
    },
    policy_provider=lambda _project: {
        "valid": True,
        "readiness": {"passed": True},
        "runtimes": {"allowed": ["codex"]},
        "workspace": {"repo_role": "canonical", "isolation": "worktree"},
        "placement": {
            "host_classes": ["personal", "ephemeral"],
            "trust_zones": ["org_shared"],
            "burst": {"enabled": True, "max_concurrent_ephemeral": 2},
        },
        "providers": {
            "selectors": [{
                "provider": "openai",
                "connection_reference": "provider-future",
                "priority": 1,
            }],
        },
        "scm": {
            "provider": "github",
            "connection_reference": "scm-future",
        },
    },
    provider_metadata=lambda _ref, _project: {
        "provider": "openai",
        "project_allowlist": ["future-project"],
        "lifecycle_state": "active",
        "connection_kind": "oauth",
        "credential_version": 7,
        "revocation_state": "active",
    },
    scm_metadata=lambda _ref: {
        "provider": "github",
        "project_allowlist": ["future-project"],
        "repository_allowlist": [future_repo],
        "operation_scopes": ["clone", "push", "pull_request"],
        "lifecycle_state": "active",
        "installation_version": 4,
    },
    base_sha_provider=lambda _project: future_sha,
)

ok(context["project_id"] == "future-project"
   and context["repository"] == future_repo,
   "an unknown-at-deploy project resolves entirely from configuration")
ok(context["default_branch"] == "trunk" and context["base_sha"] == future_sha,
   "future-project branch and base authority remain exact")
ok(context["workspace"]["isolation"] == "worktree"
   and context["placement"]["burst"]["enabled"] is True,
   "the same context supports persistent and ephemeral placement")
encoded = repr(context)
ok("token" not in encoded.lower() and "secret" not in encoded.lower(),
   "the immutable future-project context contains no raw secret material")

print(f"\nHARDEN-78 legacy-default ratchets: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
