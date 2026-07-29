#!/usr/bin/env python3
"""Select and validate the trusted Switchboard CI lane.

Lane selection is deliberately fail-closed:

* ``head`` runs the bounded admission lane.
* ``merge_group`` runs the docs lane only when an exact, reachable base SHA is
  supplied and every changed path ends in ``.md``.
* every missing, malformed, empty, or mixed diff runs the full lane.
* ``ci_repair`` always runs the full lane.

The merge-group lane remains the landing authority. This module only avoids
running application dependencies and Playwright when the exact landing diff is
provably Markdown-only and does not alter a protected or test-consumed document.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
REFERENCE_LINK_RE = re.compile(r"^\s{0,3}\[[^\]]+]:\s*(\S+)", re.MULTILINE)
CONFLICT_MARKER_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
REMOTE_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")


class LaneError(RuntimeError):
    """A lane validation failure that should produce a red required status."""


@dataclass(frozen=True)
class LaneDecision:
    lane: str
    reason: str
    purpose: str
    source_sha: str
    base_sha: str
    changed_files: tuple[str, ...] = ()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise LaneError(detail or f"git {' '.join(args)} failed")
    return result


def _commit_exists(repo: Path, sha: str) -> bool:
    if not SHA_RE.fullmatch(sha):
        return False
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _changed_files(repo: Path, base_sha: str, source_sha: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", base_sha, source_sha, "--"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise LaneError(detail or "could not calculate exact changed paths")
    return tuple(
        item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _direct_test_paths(repo: Path) -> tuple[Path, ...]:
    candidates = set(repo.glob("test_*.py")) | set(repo.glob("*_test.py"))
    tests_root = repo / "tests"
    if tests_root.is_dir():
        candidates |= set(tests_root.rglob("test_*.py"))
        candidates |= set(tests_root.rglob("*_test.py"))
    return tuple(sorted(path for path in candidates if path.is_file()))


def _markdown_requires_full(repo: Path, changed: tuple[str, ...]) -> bool:
    """Return true when Markdown participates in policy or an executable test contract."""
    normative = {
        "AGENTS.md",
        "docs/CI-STRATEGY.md",
        "docs/SWITCHBOARD-RUNBOOK.md",
    }
    for relative in changed:
        normalized = relative.replace("\\", "/")
        if (
            normalized in normative
            or normalized.startswith(("docs/decisions/", "docs/runbooks/"))
            or normalized.endswith("-SPEC.md")
        ):
            return True

    test_sources = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in _direct_test_paths(repo)
    ]
    for relative in changed:
        normalized = relative.replace("\\", "/")
        basename = Path(normalized).name
        if any(normalized in source or basename in source for source in test_sources):
            return True
    return False


def _require_exact_checkout(repo: Path, source_sha: str) -> None:
    current = _git(repo, "rev-parse", "HEAD").stdout.strip().lower()
    if current != source_sha:
        raise LaneError(f"checkout is {current}, expected exact source {source_sha}")


def select_lane(
    purpose: str,
    source_sha: str,
    base_sha: str = "",
    *,
    repo: Path | str = Path("."),
) -> LaneDecision:
    root = Path(repo).resolve()
    normalized_purpose = (purpose or "").strip().lower()
    source = (source_sha or "").strip().lower()
    base = (base_sha or "").strip().lower()

    if normalized_purpose == "head":
        return LaneDecision(
            lane="admission",
            reason="pr_head_uses_bounded_admission",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if normalized_purpose != "merge_group":
        return LaneDecision(
            lane="full",
            reason="non_merge_group_requires_full",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if not _commit_exists(root, source):
        return LaneDecision(
            lane="full",
            reason="source_commit_missing_or_invalid",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if not _commit_exists(root, base):
        return LaneDecision(
            lane="full",
            reason="base_commit_missing_or_invalid",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if _git(root, "merge-base", "--is-ancestor", base, source, check=False).returncode:
        return LaneDecision(
            lane="full",
            reason="base_is_not_source_ancestor",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )

    try:
        changed = _changed_files(root, base, source)
    except LaneError:
        return LaneDecision(
            lane="full",
            reason="changed_path_resolution_failed",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if not changed:
        return LaneDecision(
            lane="full",
            reason="empty_diff_requires_full",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
        )
    if not all(Path(path).suffix.lower() == ".md" for path in changed):
        return LaneDecision(
            lane="full",
            reason="non_markdown_path_requires_full",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
            changed_files=changed,
        )
    if _markdown_requires_full(root, changed):
        return LaneDecision(
            lane="full",
            reason="markdown_contract_requires_full",
            purpose=normalized_purpose,
            source_sha=source,
            base_sha=base,
            changed_files=changed,
        )
    return LaneDecision(
        lane="docs",
        reason="exact_merge_group_diff_is_markdown_only",
        purpose=normalized_purpose,
        source_sha=source,
        base_sha=base,
        changed_files=changed,
    )


def apply_policy(candidate: LaneDecision, policy_mode: str) -> tuple[LaneDecision, str]:
    """Apply the operator-controlled rollout mode to a mechanical candidate."""
    mode = (policy_mode or "full").strip().lower()
    if mode not in {"full", "shadow", "enforce"}:
        mode = "full"
    if mode == "enforce" or candidate.lane == "full":
        return candidate, mode
    return (
        replace(
            candidate,
            lane="full",
            reason=f"{mode}_mode_candidate_{candidate.lane}",
        ),
        mode,
    )


def _link_targets(markdown: str) -> Iterable[str]:
    for match in INLINE_LINK_RE.finditer(markdown):
        raw = match.group(1).strip()
        if raw.startswith("<") and ">" in raw:
            yield raw[1 : raw.index(">")]
        else:
            yield raw.split(maxsplit=1)[0]
    for match in REFERENCE_LINK_RE.finditer(markdown):
        yield match.group(1).strip("<>")


def _validate_local_links(repo: Path, markdown_path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for raw_target in _link_targets(text):
        target = unquote(raw_target).split("#", 1)[0].split("?", 1)[0]
        if (
            not target
            or target.startswith(("#", "/"))
            or target.lower().startswith(REMOTE_SCHEMES)
        ):
            continue
        resolved = (markdown_path.parent / target).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            errors.append(f"{markdown_path.relative_to(repo)}: link escapes repository: {raw_target}")
            continue
        if not resolved.exists():
            errors.append(f"{markdown_path.relative_to(repo)}: missing local link target: {raw_target}")
    return errors


def verify_docs(decision: LaneDecision, *, repo: Path | str = Path(".")) -> dict[str, object]:
    root = Path(repo).resolve()
    if decision.lane != "docs":
        raise LaneError(f"docs validation requires docs lane, got {decision.lane}")
    _require_exact_checkout(root, decision.source_sha)
    diff_check = _git(
        root,
        "diff",
        "--check",
        decision.base_sha,
        decision.source_sha,
        "--",
    )
    if diff_check.returncode:
        raise LaneError((diff_check.stderr or diff_check.stdout).strip())

    errors: list[str] = []
    checked: list[str] = []
    for relative in decision.changed_files:
        path = root / relative
        if not path.exists():
            continue
        if path.is_symlink():
            errors.append(f"{relative}: Markdown symlinks are not eligible for the docs lane")
            continue
        text = path.read_text(encoding="utf-8")
        checked.append(relative)
        if CONFLICT_MARKER_RE.search(text):
            errors.append(f"{relative}: unresolved conflict marker")
        errors.extend(_validate_local_links(root, path, text))
    if errors:
        raise LaneError("\n".join(errors))
    return {
        "lane": "docs",
        "checks": ["exact_diff", "diff_check", "conflict_markers", "local_links"],
        "checked_files": checked,
    }


def verify_admission(source_sha: str, *, repo: Path | str = Path(".")) -> dict[str, object]:
    root = Path(repo).resolve()
    source = (source_sha or "").strip().lower()
    if not _commit_exists(root, source):
        raise LaneError("admission source SHA is missing or invalid")
    _require_exact_checkout(root, source)
    head_check = _git(root, "show", "--check", "--format=", source)
    if head_check.returncode:
        raise LaneError((head_check.stderr or head_check.stdout).strip())
    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "."],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if compile_result.returncode:
        raise LaneError((compile_result.stderr or compile_result.stdout).strip())
    return {
        "lane": "admission",
        "checks": ["exact_source", "head_commit_diff_check", "python_compileall"],
    }


def _write_receipt(path: str, decision: LaneDecision, verification: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": "switchboard.ci_lane_result.v1",
                "decision": asdict(decision),
                "verification": verification,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--purpose", required=True, choices=("head", "merge_group", "ci_repair"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--github-output", default="")
    parser.add_argument("--receipt", default=".artifacts/ci-lane-result.json")
    parser.add_argument("--policy-mode", default="enforce")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)

    candidate = select_lane(
        args.purpose,
        args.source_sha,
        args.base_sha,
        repo=Path(args.repo),
    )
    decision, policy_mode = apply_policy(candidate, args.policy_mode)
    output = {
        "lane": decision.lane,
        "candidate_lane": candidate.lane,
        "policy_mode": policy_mode,
        "reason": decision.reason,
        "changed_count": str(len(decision.changed_files)),
    }
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            for key, value in output.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(asdict(decision), sort_keys=True))

    verification: dict[str, object] = {
        "lane": decision.lane,
        "candidate_lane": candidate.lane,
        "policy_mode": policy_mode,
        "checks": ["selection_only"],
    }
    if args.verify:
        if decision.lane == "admission":
            verification = verify_admission(decision.source_sha, repo=Path(args.repo))
        elif decision.lane == "docs":
            verification = verify_docs(decision, repo=Path(args.repo))
        else:
            raise LaneError("full lane verification is owned by scripts/switchboard_ci.sh")
    _write_receipt(args.receipt, decision, verification)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LaneError as exc:
        print(f"ci lane validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
