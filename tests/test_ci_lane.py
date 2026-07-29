#!/usr/bin/env python3
"""Direct-execution tests for the trusted CI lane selector."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from scripts.ci_lane import LaneError, apply_policy, select_lane, verify_docs


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "ci-lane@example.test")
    git(repo, "config", "user.name", "CI Lane Test")

    (repo / "README.md").write_text("# Start\n", encoding="utf-8")
    base = commit(repo, "base")

    (repo / "guide.md").write_text("[Start](README.md)\n", encoding="utf-8")
    docs_head = commit(repo, "docs")

    admission = select_lane("head", docs_head, "", repo=repo)
    check(admission.lane == "admission", "PR heads must use bounded admission")

    docs = select_lane("merge_group", docs_head, base, repo=repo)
    check(docs.lane == "docs", "an exact Markdown-only merge diff must use docs lane")
    shadowed, mode = apply_policy(docs, "shadow")
    check(
        shadowed.lane == "full" and mode == "shadow",
        "shadow mode must observe a docs candidate while still running full CI",
    )
    enforced, mode = apply_policy(docs, "enforce")
    check(
        enforced.lane == "docs" and mode == "enforce",
        "enforce mode must select the mechanically proven candidate",
    )
    invalid_mode, mode = apply_policy(docs, "surprise")
    check(
        invalid_mode.lane == "full" and mode == "full",
        "unknown rollout modes must fail closed to full CI",
    )
    receipt = verify_docs(docs, repo=repo)
    check(receipt["lane"] == "docs", "docs verification must return a docs receipt")

    (repo / "docs").mkdir()
    (repo / "docs" / "CI-STRATEGY.md").write_text("# Contract\n", encoding="utf-8")
    protected_docs_head = commit(repo, "protected docs")
    protected_docs = select_lane(
        "merge_group", protected_docs_head, docs_head, repo=repo
    )
    check(
        protected_docs.lane == "full"
        and protected_docs.reason == "markdown_contract_requires_full",
        "normative Markdown must run the full suite",
    )

    (repo / "guide.md").write_text("[Start](README.md)\n\nChanged\n", encoding="utf-8")
    (repo / "test_guide_contract.py").write_text(
        'from pathlib import Path\nPath("guide.md").read_text()\n',
        encoding="utf-8",
    )
    contract_base = commit(repo, "add guide contract")
    (repo / "guide.md").write_text(
        "[Start](README.md)\n\nChanged again\n", encoding="utf-8"
    )
    contract_head = commit(repo, "change tested guide")
    tested_docs = select_lane("merge_group", contract_head, contract_base, repo=repo)
    check(
        tested_docs.lane == "full"
        and tested_docs.reason == "markdown_contract_requires_full",
        "Markdown referenced by a direct test must run the full suite",
    )

    missing_base = select_lane("merge_group", docs_head, "f" * 40, repo=repo)
    check(missing_base.lane == "full", "missing base evidence must fall back to full")

    non_ancestor = select_lane("merge_group", base, docs_head, repo=repo)
    check(non_ancestor.lane == "full", "non-ancestor base evidence must fall back to full")

    (repo / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    mixed_head = commit(repo, "code")
    mixed = select_lane("merge_group", mixed_head, docs_head, repo=repo)
    check(mixed.lane == "full", "any non-Markdown path must force full CI")

    git(repo, "mv", "example.py", "renamed.md")
    renamed_head = commit(repo, "rename code to markdown")
    renamed = select_lane("merge_group", renamed_head, mixed_head, repo=repo)
    check(
        renamed.lane == "full",
        "a code-to-Markdown rename must expose the removed code path and force full CI",
    )

    repair = select_lane("ci_repair", mixed_head, docs_head, repo=repo)
    check(repair.lane == "full", "CI repair must always run full CI")

    (repo / "broken.md").write_text("[Missing](does-not-exist.md)\n", encoding="utf-8")
    broken_head = commit(repo, "broken docs")
    broken = select_lane("merge_group", broken_head, renamed_head, repo=repo)
    check(broken.lane == "docs", "broken Markdown is still classified as docs")
    try:
        verify_docs(broken, repo=repo)
    except LaneError as exc:
        check("missing local link target" in str(exc), "broken link must be explicit")
    else:
        raise AssertionError("broken local Markdown link must fail docs validation")

print("PASS: trusted CI lanes fail closed and validate Markdown-only merge groups")
