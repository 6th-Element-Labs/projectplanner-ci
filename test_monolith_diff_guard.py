#!/usr/bin/env python3
"""HARDEN-69 / ADR-0010 Lever 1 — per-PR monolith diff-guard.

Replaces the retired exact-match size ratchet (`test_size_ratchet.py`, deleted by
ARCH-MS-2 / #345). That ratchet stored a *shared global counter*: every PR that grew a
monolith had to compare-and-swap the same ceiling line against a moving `master`, so two
otherwise-independent PRs collided on it — the fleet's #1 stale-branch merge-war source
(ADR-0010 Context, NARRATE-13's 4 merge cycles).

This guard is **commutative**: it inspects only *this PR's own diff* against its merge
base. Two PRs that touch different files never conflict, because neither edits a shared
line. Extractions that move lines *out* of a monolith (net delta <= 0) always pass.

Rule (ADR-0010 Lever 1): CI fails if this PR's diff adds *net* lines above the dismantling
threshold (default 750 per monolith file, override via ``$MONOLITH_GROWTH_THRESHOLD``) to any
monolith — `store.py`, `app.py`, `mcp_server.py` — without a `MONOLITH-TOUCH:` justification.
Small shell touches during ARCH-MS extraction pass without a waiver; only large growth needs
acknowledgement in review.

Escape hatch — provide a `MONOLITH-TOUCH: <reason>` line via any of (checked in order):
  1. $MONOLITH_TOUCH_JUSTIFICATION  (explicit CI/operator override)
  2. $PR_BODY                       (the PR description, if the gate exports it)
  3. any commit message in this PR's own range (base..HEAD)
The commit-message path is the reliable one: it is the only source visible inside the
off-box CI mirror (the sandbox verifies a pushed SHA and never sees GitHub PR metadata),
and it travels with the squash-merge into `master` history for a permanent audit trail.

Base resolution (no shared state; works in the CI mirror and in local dev):
  1. $MONOLITH_DIFF_BASE, if set and resolvable.
  2. HEAD^1 when HEAD is a merge commit. `scripts/switchboard_pr_gate.py` mirrors GitHub's
     `refs/pull/N/merge` commit, whose first parent is the base-branch tip, so HEAD^1 is
     exactly the pre-PR base (docs/EXTERNAL-CI-MIRROR-SPEC.md).
  3. merge-base against origin/master, master, origin/main, main (local feature branch).
If no base resolves (run on `master` itself, a shallow checkout with no base, or outside a
git tree) the guard is a *visible* no-op: it prints a NOTE and passes, because there is no
PR diff to judge. This is named, not a silent green — it never hides a real growth signal.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The application-shell monoliths ADR-0010 Lever 1 guards by name. Deliberately NOT the old
# ratchet's static/app.js line ceiling or repo-root .py *count* — ADR-0010 drops those as
# shared counters; frontend split is tracked by ARCH-MS-21, root growth by review.
MONOLITH_FILES = ("store.py", "app.py", "mcp_server.py")
JUSTIFICATION_MARKER = "MONOLITH-TOUCH:"
BASE_REF_CANDIDATES = ("origin/master", "master", "origin/main", "main")
# High bar during ARCH-MS dismantling — only flag runaway shell growth.
DEFAULT_GROWTH_THRESHOLD = 750


def growth_threshold():
    raw = (os.environ.get("MONOLITH_GROWTH_THRESHOLD") or "").strip()
    if not raw:
        return DEFAULT_GROWTH_THRESHOLD
    try:
        return max(0, int(raw))
    except ValueError:
        note(f"$MONOLITH_GROWTH_THRESHOLD={raw!r} invalid; using default {DEFAULT_GROWTH_THRESHOLD}.")
        return DEFAULT_GROWTH_THRESHOLD

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def note(message):
    print("  NOTE  " + message)


def _git(*args):
    """Run a git command rooted at the repo; return (returncode, stdout.strip())."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "").strip()


def _rev(ref):
    """Resolve ref to a commit SHA, or None if it does not exist."""
    code, out = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out or None if code == 0 else None


def resolve_base():
    """Return (base_sha, human_reason) or (None, human_reason)."""
    env_base = (os.environ.get("MONOLITH_DIFF_BASE") or "").strip()
    if env_base:
        sha = _rev(env_base)
        if sha:
            return sha, f"$MONOLITH_DIFF_BASE ({env_base})"
        note(f"$MONOLITH_DIFF_BASE={env_base!r} did not resolve; trying other bases.")

    if _rev("HEAD") is None:
        return None, "no HEAD (not a git checkout)"

    # Merge commit -> first parent is the base branch tip (the CI-mirror path).
    if _rev("HEAD^2") is not None:
        parent = _rev("HEAD^1")
        if parent:
            return parent, "HEAD^1 (merge-commit first parent)"

    # Local feature branch -> merge-base with a known base ref.
    for cand in BASE_REF_CANDIDATES:
        if _rev(cand) is None:
            continue
        code, mb = _git("merge-base", cand, "HEAD")
        if code == 0 and mb:
            return mb, f"merge-base with {cand}"

    return None, "no base ref resolved"


def monolith_net_deltas(base):
    """Return {path: net_added_lines} for touched monolith files, or None on git error."""
    code, out = _git("diff", "--numstat", base, "HEAD", "--", *MONOLITH_FILES)
    if code != 0:
        return None
    deltas = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == "-" or deleted == "-":  # binary; not applicable to text monoliths
            continue
        deltas[path] = int(added) - int(deleted)
    return deltas


def find_justification(base):
    """Return (reason_str, source_label) if a MONOLITH-TOUCH justification exists, else (None, None)."""
    def _scan(text):
        for raw in (text or "").splitlines():
            line = raw.strip()
            if line.startswith(JUSTIFICATION_MARKER):
                reason = line[len(JUSTIFICATION_MARKER):].strip()
                if reason:
                    return reason
        return None

    # A non-empty override env is treated as justification whether or not it carries the
    # marker (an operator who sets it has already made the acknowledgement explicit).
    override = (os.environ.get("MONOLITH_TOUCH_JUSTIFICATION") or "").strip()
    if override:
        return (_scan(override) or override), "$MONOLITH_TOUCH_JUSTIFICATION"

    reason = _scan(os.environ.get("PR_BODY", ""))
    if reason:
        return reason, "$PR_BODY"

    code, log = _git("log", "--format=%B", f"{base}..HEAD")
    if code == 0:
        reason = _scan(log)
        if reason:
            return reason, "commit message"

    return None, None


def main():
    base, base_reason = resolve_base()
    if base is None:
        note(f"monolith diff-guard: {base_reason}; no PR diff to evaluate — guard is a no-op.")
        note("Expected on master or outside a PR checkout; the guard only fires on a real diff.")
        print("\n%d passed, %d failed" % (passed, failed))
        raise SystemExit(0)

    note(f"diff base = {base[:12]} via {base_reason}")

    deltas = monolith_net_deltas(base)
    if deltas is None:
        ok(False, f"git diff against base {base[:12]} failed; cannot evaluate monolith growth")
        print("\n%d passed, %d failed" % (passed, failed))
        raise SystemExit(1)

    threshold = growth_threshold()
    note(f"growth threshold = {threshold} net lines per monolith file "
         f"(override via $MONOLITH_GROWTH_THRESHOLD)")

    grew_all = [(path, deltas.get(path, 0)) for path in MONOLITH_FILES
                if (delta := deltas.get(path, 0)) > 0]
    grew_over = [(path, delta) for path, delta in grew_all if delta > threshold]
    grew_under = [(path, delta) for path, delta in grew_all if delta <= threshold]

    if not grew_all:
        touched = ", ".join(f"{p} ({d:+d})" for p, d in sorted(deltas.items())) or "none touched"
        ok(True, f"no monolith grew in this PR's diff [{touched}]")
    elif not grew_over:
        summary = "; ".join(f"{path} (+{delta} net lines)" for path, delta in grew_under)
        ok(True, f"monolith grew within dismantling threshold (≤{threshold}) — {summary}")
    else:
        reason, source = find_justification(base)
        summary = "; ".join(f"{path} (+{delta} net lines)" for path, delta in grew_over)
        if reason:
            ok(True, f"monolith grew above threshold but justified — {summary} "
               f"[MONOLITH-TOUCH via {source}: {reason[:120]}]")
        else:
            ok(False,
               f"monolith grew above threshold ({threshold}) without MONOLITH-TOUCH: {summary}. "
               f"Extract the growth into a module, or add a 'MONOLITH-TOUCH: <reason>' line to a "
               f"commit message (or the PR body) to acknowledge the shell growth (ADR-0010 Lever 1).")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
