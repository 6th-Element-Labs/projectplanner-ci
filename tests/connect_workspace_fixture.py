"""Stub the Agent Host workspace layer for Connect launch tests.

Tests that only care about argv, environment, or the completion contract still
have to satisfy the launch path's workspace contract.  Give them one real
``MaterializedWorkspace`` rather than a partial namespace, so a change to the
teardown/verification contract shows up as a test failure instead of an
``AttributeError`` deep inside a revocation path that deletes directories.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from adapters.repository_workspace import MaterializedWorkspace


def stub_workspace(path, *, workspace_root=None, branch="agent/stub/g1",
                   head_sha="0" * 40) -> MaterializedWorkspace:
    path = Path(path)
    root = Path(workspace_root) if workspace_root else path.parent
    return MaterializedWorkspace(
        path=path,
        branch=branch,
        head_sha=head_sha,
        cache_path=root / "cache.git",
        receipt_path=root / ".receipts" / "stub.json",
        receipt={"schema": "switchboard.repository_workspace_receipt.v1"},
        workspace_root=root,
    )


@contextlib.contextmanager
def stubbed_workspace(agent_host, workspace: MaterializedWorkspace):
    """Make materialize/verify/revoke inert for one launch under test."""
    saved = (
        agent_host.materialize_repository_workspace,
        agent_host.verify_repository_workspace,
        agent_host.materialize_host_worktree,
        agent_host.verify_host_worktree,
        agent_host.revoke_repository_workspace,
        agent_host._record_workspace_binding,
    )
    agent_host.materialize_repository_workspace = (
        lambda *_args, **_kwargs: workspace)
    agent_host.verify_repository_workspace = lambda *_args, **_kwargs: workspace
    agent_host.materialize_host_worktree = lambda *_args, **_kwargs: workspace
    agent_host.verify_host_worktree = lambda *_args, **_kwargs: workspace
    agent_host.revoke_repository_workspace = (
        lambda *_args, **_kwargs: {"revoked": True, "stubbed": True})
    agent_host._record_workspace_binding = lambda *_args, **_kwargs: None
    try:
        yield workspace
    finally:
        (agent_host.materialize_repository_workspace,
         agent_host.verify_repository_workspace,
         agent_host.materialize_host_worktree,
         agent_host.verify_host_worktree,
         agent_host.revoke_repository_workspace,
         agent_host._record_workspace_binding) = saved
