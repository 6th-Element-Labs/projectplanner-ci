"""CO-23: the execution context must carry the runtime's OWN provider.

A project with several provider selectors (one per vendor) must give a
claude-code task the anthropic-claude connection and a codex task the
openai-codex one. Handing a task another vendor's credential is not a
degraded mode: the receiving host refuses it (`provider_not_allowed`) and the
task never runs, so a selector that matches nothing must fail loudly instead
of silently falling back to the first entry in the list.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401  (import side effect: sys.path)

from switchboard.application.commands import execution_context


def ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


BASE_SHA = "0" * 40
TOPOLOGY = {
    "valid": True,
    "roles": {"canonical": {"repo": "acme/widgets", "default_branch": "master"}},
}


def _policy(selectors):
    return {
        "valid": True,
        "readiness": {"passed": True},
        "runtimes": {"allowed": ["codex", "claude_code"], "default": "codex"},
        "workspace": {"repo_role": "canonical", "isolation": "worktree"},
        "placement": {"host_classes": ["personal"], "trust_zones": ["org_shared"],
                      "burst": {"enabled": False, "max_concurrent_ephemeral": 0}},
        "providers": {"selectors": selectors},
        "scm": {"connection_reference": "scm-test", "provider": "github_app"},
    }


BOTH_SELECTORS = [
    {"provider": "openai-codex", "connection_reference": "cred-codex", "priority": 0},
    {"provider": "anthropic-claude", "connection_reference": "cred-claude", "priority": 1},
]


def _resolve(runtime, selectors=BOTH_SELECTORS):
    return execution_context.resolve(
        project="switchboard", task_id="CO-26", runtime=runtime,
        topology_provider=lambda _project: TOPOLOGY,
        policy_provider=lambda _project: _policy(selectors),
        provider_metadata=lambda reference, _project: {
            "credential_reference": reference,
            "provider": ("anthropic-claude" if "claude" in reference else "openai-codex"),
            "connection_kind": "personal_subscription",
            "lifecycle_state": "active",
            "revocation_state": "not_revoked",
            "credential_version": 1,
            "project_allowlist": ["switchboard"],
        },
        scm_metadata=lambda _reference: {
            "connection_reference": "scm-test", "provider": "github_app",
            "lifecycle_state": "active", "installation_version": 1,
            "operation_scopes": ["clone", "fetch", "push", "read", "create_pr", "merge"],
            "project_allowlist": ["switchboard"],
            "repository_allowlist": ["acme/widgets"],
        },
        base_sha_provider=lambda _project: BASE_SHA,
    )


print("CO-23 runtime provider selection")

for requested in ("claude-code", "claude", "anthropic", "claude_code"):
    context = _resolve(requested)
    provider = dict(context.get("provider") or {})
    ok(provider.get("provider") == "anthropic-claude"
       and provider.get("connection_reference") == "cred-claude",
       f"runtime {requested!r} resolves to the anthropic-claude connection")

for requested in ("codex", "openai"):
    context = _resolve(requested)
    provider = dict(context.get("provider") or {})
    ok(provider.get("provider") == "openai-codex"
       and provider.get("connection_reference") == "cred-codex",
       f"runtime {requested!r} still resolves to the openai-codex connection")

# Selector order must not decide the vendor — only the runtime does.
reversed_selectors = list(reversed(BOTH_SELECTORS))
context = _resolve("codex", reversed_selectors)
ok(dict(context.get("provider") or {}).get("provider") == "openai-codex",
   "selector list order does not change which vendor a runtime receives")

# No selector for the runtime's provider is a refusal, never another vendor.
codex_only = [BOTH_SELECTORS[0]]
try:
    _resolve("claude-code", codex_only)
    refused = ""
except execution_context.ExecutionContextError as exc:
    refused = exc.code
ok(refused == "provider_connection_not_ready",
   "a runtime with no matching selector is refused, not given another vendor's credential")

print("\nall CO-23 provider-selection checks passed")
