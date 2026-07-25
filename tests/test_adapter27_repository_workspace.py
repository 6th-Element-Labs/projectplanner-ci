from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from adapters.repository_workspace import (
    WorkspaceMaterializationError,
    cleanup,
    materialize,
    repository_remote,
)


def raises(error_type, fn):
    try:
        fn()
    except error_type as exc:
        return exc
    raise AssertionError(f"expected {error_type.__name__}")


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def remote_fixture(tmp_path: Path, slug: str) -> tuple[str, str]:
    source = tmp_path / "sources" / slug
    source.mkdir(parents=True)
    git("init", "-b", "main", cwd=source)
    git("config", "user.email", "adapter27@example.test", cwd=source)
    git("config", "user.name", "ADAPTER-27", cwd=source)
    (source / "README.md").write_text(f"# {slug}\n", encoding="utf-8")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "fixture", cwd=source)
    sha = git("rev-parse", "HEAD", cwd=source)
    remote = tmp_path / "remotes" / f"{slug}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--bare", str(source), str(remote))
    return remote.as_uri(), sha


def context(slug: str, sha: str, task: str = "ADAPTER-27",
            generation: int = 1) -> dict:
    return {
        "schema": "switchboard.execution_context.v1",
        "project_id": "switchboard" if "projectplanner" in slug else "atlas",
        "task_id": task,
        "repository": slug,
        "default_branch": "main",
        "base_sha": sha,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"registry_name": "codex"},
        "generation": generation,
        "authority_digest": f"sha256:authority-{slug}",
        "digest": f"sha256:context-{slug}-{generation}",
    }


def test_previously_unseen_repository_materializes_exact_clean_checkout(
        tmp_path: Path, slug: str):
    remote, sha = remote_fixture(tmp_path, slug)
    ctx = context(slug, sha)
    result = materialize(
        ctx, task_id=ctx["task_id"], execution_id="execlease-first",
        branch=f"agent/{ctx['project_id']}/ADAPTER-27/execlease-first-g1",
        cache_root=tmp_path / "cache", workspace_root=tmp_path / "workspaces",
        remote_url=remote)

    assert result.head_sha == sha
    assert git("rev-parse", "HEAD", cwd=result.path) == sha
    assert git("status", "--porcelain", cwd=result.path) == ""
    assert result.receipt["repository"] == slug
    assert result.receipt["cache_created"] is True
    assert result.receipt_path.is_relative_to(
        (tmp_path / "workspaces" / ".receipts").resolve())


def test_retry_and_restart_reuse_only_exact_receipt(tmp_path: Path):
    slug = "6th-Element-Labs/projectplanner"
    remote, sha = remote_fixture(tmp_path, slug)
    kwargs = {
        "execution_context": context(slug, sha),
        "task_id": "ADAPTER-27",
        "execution_id": "execlease-retry",
        "branch": "agent/switchboard/ADAPTER-27/execlease-retry-g1",
        "cache_root": tmp_path / "cache",
        "workspace_root": tmp_path / "workspaces",
        "remote_url": remote,
    }
    first = materialize(**kwargs)
    second = materialize(**kwargs)
    assert first.path == second.path
    assert second.reused is True

    receipt = json.loads(second.receipt_path.read_text())
    receipt["authority_digest"] = "sha256:poisoned"
    second.receipt_path.write_text(json.dumps(receipt))
    repaired = materialize(**kwargs)
    assert repaired.reused is False
    assert repaired.receipt["authority_digest"] == kwargs[
        "execution_context"]["authority_digest"]
    assert list((tmp_path / "workspaces" / ".quarantine").iterdir())


def test_cache_origin_mismatch_and_poison_are_quarantined(tmp_path: Path):
    slug = "6th-Element-Labs/projectplanner"
    remote, sha = remote_fixture(tmp_path, slug)
    other, _ = remote_fixture(tmp_path, "6th-Element-Labs/ActionEngine")
    common = {
        "execution_context": context(slug, sha),
        "task_id": "ADAPTER-27",
        "branch": "agent/switchboard/ADAPTER-27/cache-g1",
        "cache_root": tmp_path / "cache",
        "workspace_root": tmp_path / "workspaces",
        "remote_url": remote,
    }
    first = materialize(execution_id="execlease-cache-a", **common)
    git("--git-dir", str(first.cache_path), "remote", "set-url", "origin", other)
    second = materialize(execution_id="execlease-cache-b", **common)
    assert second.receipt["cache_quarantined"]

    cleanup(second)
    shutil.rmtree(second.cache_path)
    second.cache_path.mkdir()
    (second.cache_path / "poison").write_text("not a repository")
    third = materialize(execution_id="execlease-cache-c", **common)
    assert third.receipt["cache_quarantined"]


def test_context_mismatch_unreachable_sha_and_path_escape_fail_closed(tmp_path: Path):
    slug = "6th-Element-Labs/projectplanner"
    remote, sha = remote_fixture(tmp_path, slug)
    ctx = context(slug, sha)
    mismatch = raises(
        WorkspaceMaterializationError,
        lambda: materialize(
            ctx, task_id="OTHER-1", execution_id="execlease-x", branch="agent/x",
            cache_root=tmp_path / "cache", workspace_root=tmp_path / "workspaces",
            remote_url=remote),
    )
    assert mismatch.code == "execution_context_task_mismatch"

    unreachable = {**ctx, "base_sha": "f" * 40}
    missing = raises(
        WorkspaceMaterializationError,
        lambda: materialize(
            unreachable, task_id="ADAPTER-27", execution_id="execlease-y",
            branch="agent/y", cache_root=tmp_path / "cache",
            workspace_root=tmp_path / "workspaces", remote_url=remote),
    )
    assert missing.code == "base_sha_unreachable"

    escaped = materialize(
        ctx, task_id="ADAPTER-27", execution_id="../../outside",
        branch="agent/safe", cache_root=tmp_path / "cache",
        workspace_root=tmp_path / "workspaces", remote_url=remote)
    assert escaped.path.is_relative_to((tmp_path / "workspaces").resolve())
    assert ".." not in escaped.path.parts

    credential = raises(
        WorkspaceMaterializationError,
        lambda: repository_remote(
            slug, "https://secret@example.test/6th-Element-Labs/projectplanner.git"),
    )
    assert credential.code == "repository_remote_contains_credential"


def test_concurrent_executions_are_isolated_and_cleanup_is_bounded(tmp_path: Path):
    slug = "6th-Element-Labs/ActionEngine"
    remote, sha = remote_fixture(tmp_path, slug)
    ctx = context(slug, sha)

    def create(index: int):
        return materialize(
            ctx, task_id="ADAPTER-27", execution_id=f"execlease-{index}",
            branch=f"agent/atlas/ADAPTER-27/execlease-{index}-g1",
            cache_root=tmp_path / "cache",
            workspace_root=tmp_path / "workspaces", remote_url=remote)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert len({row.path for row in results}) == 4
    assert len({row.branch for row in results}) == 4
    assert {row.head_sha for row in results} == {sha}

    removed = cleanup(results[0])
    quarantined = cleanup(results[1], quarantine=True, reason="failed-run")
    assert removed["cleaned"] is True and not results[0].path.exists()
    assert quarantined["quarantined"]
    assert results[2].path.exists() and results[3].path.exists()


def test_materializer_has_no_legacy_repo_authority_fallbacks():
    source = (
        Path(__file__).parents[1] / "adapters" / "repository_workspace.py"
    ).read_text(encoding="utf-8")
    assert "PM_REPO_PATH_" not in source
    assert "PM_REPO_ROOT" not in source
    assert "_git_root" not in source


with tempfile.TemporaryDirectory(prefix="adapter27-") as temporary:
    root = Path(temporary)
    for repository_slug in (
        "6th-Element-Labs/projectplanner",
        "6th-Element-Labs/ActionEngine",
    ):
        test_previously_unseen_repository_materializes_exact_clean_checkout(
            root / repository_slug.rsplit("/", 1)[-1], repository_slug)
    test_retry_and_restart_reuse_only_exact_receipt(root / "retry")
    test_cache_origin_mismatch_and_poison_are_quarantined(root / "cache")
    test_context_mismatch_unreachable_sha_and_path_escape_fail_closed(
        root / "fail-closed")
    test_concurrent_executions_are_isolated_and_cleanup_is_bounded(
        root / "concurrent")
test_materializer_has_no_legacy_repo_authority_fallbacks()
print("ADAPTER-27 repository workspace materializer: passed")
