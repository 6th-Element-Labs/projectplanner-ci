"""HARDEN-72 (CI-5 / Lever 7) — per-PR CI attribution with a direct failing-test link.

When the Switchboard PR gate posts a red commit status, an operator (or the agent
that opened the PR) must be able to click straight through to *what failed* — the
CI run and, where we can name them, the specific failing test(s). Before this the
gate linked a red status back at the PR itself (``target_url=pr_url``) and buried
the run URL inside the exception text, so "why is my PR red?" meant hunting.

This module is intentionally FastAPI-free (like ``pr_provenance_gate`` /
``task_id_parser``) so it can be unit-tested without the web app and imported by
the CI runner in ``scripts/switchboard_pr_gate.py``. It:

* parses failing tests out of a gate log — both the script-style Switchboard suite
  (``== test_foo.py ==`` section headers + tracebacks, aborts on first failure) and
  pytest ``FAILED``/``ERROR`` short-summary lines;
* extracts the external CI run / logs URLs the mirror recorded into the log;
* builds a GitHub-safe status ``description`` + a **direct** ``target_url`` that
  prefers the CI run over the PR; and
* records a ``ci.attribution`` activity on the board so the red is auditable and
  attributable after the ephemeral worktree is gone.

See docs/CI-STRATEGY.md and the ``deliverable-ci-concurrency`` deliverable (L6/L7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

SCHEMA = "switchboard.ci_attribution.v1"

# Strip terminal colour codes before parsing — the external mirror scrapes GitHub Actions /
# pytest output, which is commonly ANSI-coloured and would otherwise defeat the anchors.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# pytest short-test-summary lines: "FAILED path::test - AssertionError: ..." /
# "ERROR path::test - ...". The reason (after " - ") is optional.
_PYTEST_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", re.MULTILINE)
# Switchboard suite section header written by scripts/switchboard_ci.sh: "== test_foo.py ==".
_SECTION_RE = re.compile(r"^==\s+(\S+\.py)\s+==\s*$", re.MULTILINE)
# Error/exception line for the description reason (drops a loose "assert" substring match).
_ERROR_LINE_RE = re.compile(r"^[\w.]*(?:Error|Exception)\b")
# The mirror dumps its result JSON into the gate log; pull the run/logs URLs back out. Accept
# both the JSON form and a plain ``run_url=<url>`` line the gate now writes ahead of the
# (truncated) JSON dump, so the link survives even if the JSON is cut off.
_RUN_URL_RE = re.compile(r'"run_url"\s*:\s*"([^"]+)"|^run_url=(\S+)$', re.MULTILINE)
_LOGS_URL_RE = re.compile(r'"logs_url"\s*:\s*"([^"]+)"|^logs_url=(\S+)$', re.MULTILINE)
# Definitive "a test actually raised" signal for the script-style suite: each test raises
# (AssertionError -> traceback). Kept tight so benign output containing "Error:"/"assert " in a
# PASS line can't trigger a false section attribution.
_FAILURE_TRACEBACK = "Traceback (most recent call last)"


def _looks_like_nodeid(token: str) -> bool:
    """A real pytest nodeid always names a test file. Requiring ``.py`` (or ``::``) keeps a
    passing test that merely prints ``FAILED case_7`` — or a ``FAILED 0`` summary-table cell —
    from being misread as the failing test."""
    return ".py" in token or "::" in token


@dataclass
class FailingTest:
    """One failing test the gate could name. ``nodeid`` is the most specific id we
    have (``file::test`` when known, else the file); ``file`` is always the file."""
    nodeid: str
    file: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.file:
            self.file = self.nodeid.split("::", 1)[0]

    def to_dict(self) -> Dict[str, str]:
        return {"nodeid": self.nodeid, "file": self.file, "reason": self.reason}


def parse_failing_tests(log_text: str, *, limit: int = 25) -> List[FailingTest]:
    """Best-effort failing tests from a gate log, newest signal first.

    Handles two shapes: pytest ``FAILED``/``ERROR`` summary lines (used verbatim),
    and the script-style Switchboard suite, where ``scripts/switchboard_ci.sh`` runs
    each ``test_*.py`` in turn under ``set -e`` and aborts on the first one that
    raises — so the *last* ``== file ==`` section header before a traceback is the
    culprit. Returns ``[]`` when the log shows no failure (e.g. an infra error with
    no test verdict); callers fall back to a generic link then."""
    if not log_text:
        return []
    log_text = _ANSI_RE.sub("", log_text)
    seen = set()
    out: List[FailingTest] = []
    for match in _PYTEST_RE.finditer(log_text):
        nodeid = match.group(1).strip()
        # Require a test-nodeid shape so a passing test that prints "FAILED case_7" (or a
        # "FAILED 0" summary cell) can't hijack attribution and hide the real culprit.
        if not nodeid or nodeid in seen or not _looks_like_nodeid(nodeid):
            continue
        seen.add(nodeid)
        out.append(FailingTest(nodeid=nodeid, reason=(match.group(2) or "").strip()))
        if len(out) >= limit:
            return out
    if out:
        return out
    # No pytest summary — fall back to the script-suite section that was running when the run
    # died. Only attribute when the log carries a real Python traceback, so benign output that
    # merely contains "Error:"/"assert " in a PASS line can't be mislabelled as a test failure.
    if _FAILURE_TRACEBACK not in log_text:
        return []
    sections = _SECTION_RE.findall(log_text)
    # Skip the shell's own scaffolding sections (runtime/version/dep headers) — only
    # sections that name a test file are attributable to a test.
    test_sections = [s for s in sections if s.split("/")[-1].startswith("test_")
                     or s.endswith("_test.py")]
    if test_sections:
        culprit = test_sections[-1]
        out.append(FailingTest(nodeid=culprit, file=culprit,
                               reason=_first_error_line(log_text)))
    return out


def _first_error_line(log_text: str) -> str:
    """The most specific error line we can quote (the exception message). Scans bottom-up so a
    traceback's final ``SomeError: message`` line wins; matches an exception-class line, not a
    loose ``assert`` substring."""
    for line in reversed(log_text.splitlines()):
        s = line.strip()
        if _ERROR_LINE_RE.match(s):
            return s[:200]
    return ""


def extract_run_links(log_text: str) -> Dict[str, str]:
    """The external-CI run/logs URLs the mirror recorded into the gate log, if any (JSON or
    the plain ``run_url=`` line form)."""
    links: Dict[str, str] = {}
    if not log_text:
        return links
    log_text = _ANSI_RE.sub("", log_text)
    for key, pattern in (("run_url", _RUN_URL_RE), ("logs_url", _LOGS_URL_RE)):
        match = pattern.search(log_text)
        if not match:
            continue
        value = (match.group(1) or match.group(2) or "").strip()
        if value and value != "null":
            links[key] = value
    return links


def summarize_failures(tests: Sequence[FailingTest]) -> str:
    """Short, human "test_x::y (+2 more)" for a status description."""
    if not tests:
        return ""
    head = tests[0].nodeid
    extra = len(tests) - 1
    return head if extra <= 0 else f"{head} (+{extra} more)"


def commit_checks_url(repo: str, sha: str) -> str:
    """The PR-independent GitHub page that lists a commit's checks — the fallback
    'where did it fail' link when there is no external run URL (e.g. local suite)."""
    return f"https://github.com/{repo}/commit/{sha}/checks" if repo and sha else ""


@dataclass
class Attribution:
    """A structured, auditable record of one gate outcome for one PR head SHA."""
    repo: str
    sha: str
    state: str                     # "success" | "failure"
    pr_number: Optional[int] = None
    run_url: str = ""
    logs_url: str = ""
    target_url: str = ""
    description: str = ""
    failure_class: str = ""
    failing_tests: List[FailingTest] = field(default_factory=list)
    merge_group: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "repo": self.repo,
            "head_sha": self.sha,
            "state": self.state,
            "pr_number": self.pr_number,
            "merge_group": self.merge_group or None,
            "run_url": self.run_url or None,
            "logs_url": self.logs_url or None,
            "target_url": self.target_url or None,
            "failure_class": self.failure_class or None,
            "failing_tests": [t.to_dict() for t in self.failing_tests],
        }


def build_success_attribution(*, repo: str, sha: str, pr_number: Optional[int] = None,
                              pr_url: str = "", run_url: str = "", logs_url: str = "",
                              merge_group: str = "", queue: bool = False) -> Attribution:
    """Green-gate attribution: still link the *run* (not the PR) so every PR's CI is
    traceable, pass or fail. Falls back to the PR/commit link with no external run."""
    where = "(merge queue) " if queue else ""
    target = run_url or pr_url or commit_checks_url(repo, sha)
    if run_url:
        desc = f"Switchboard VM gate passed {where}— CI run linked".strip()
    else:
        desc = f"Switchboard VM gate passed {where}".strip()
    return Attribution(repo=repo, sha=sha, state="success", pr_number=pr_number,
                       run_url=run_url, logs_url=logs_url, target_url=target,
                       description=desc, merge_group=merge_group)


def build_failure_attribution(*, repo: str, sha: str, pr_number: Optional[int] = None,
                              pr_url: str = "", log_text: str = "", error_text: str = "",
                              run_url: str = "", logs_url: str = "",
                              failure_class: str = "", merge_group: str = "",
                              queue: bool = False) -> Attribution:
    """Red-gate attribution with a **direct failing-test link**.

    ``target_url`` priority: an explicit external run URL (workflow that ran the
    suite red) > a run URL scraped from the log > the commit's checks page > the PR.
    The description names the failing test(s) when we could parse them, so the red
    status itself says *what* broke, and the click goes to *where* it broke — not
    back to the PR page."""
    links = extract_run_links(log_text)
    run_url = run_url or links.get("run_url", "")
    logs_url = logs_url or links.get("logs_url", "")
    tests = parse_failing_tests(log_text)
    target = run_url or logs_url or commit_checks_url(repo, sha) or pr_url
    where = "(merge queue) " if queue else ""
    summary = summarize_failures(tests)
    if summary:
        desc = f"Switchboard VM gate failed {where}— {summary}".strip()
    else:
        # No test names parsed (infra/dispatch red or an opaque log): keep the reason
        # visible instead of a bare "failed", and still point at the run/checks.
        reason = " ".join((error_text or "").split())[:80]
        desc = f"Switchboard VM gate failed {where}{('— ' + reason) if reason else ''}".strip()
    return Attribution(repo=repo, sha=sha, state="failure", pr_number=pr_number,
                       run_url=run_url, logs_url=logs_url, target_url=target,
                       description=desc, failure_class=failure_class,
                       failing_tests=tests, merge_group=merge_group)


def record_attribution(attribution: Attribution, *, project: str,
                       task_id: Optional[str] = None,
                       actor: str = "switchboard-ci/attribution") -> Optional[int]:
    """Append a ``ci.attribution`` activity on the board, deduped on
    ``(pr_number|merge_group, head_sha, state)`` so the 5-minute timer doesn't flood
    the log. Best-effort: store is imported lazily and every failure is swallowed —
    attribution must never break the gate. Returns the activity id, or None."""
    try:
        import json
        import store
    except Exception:
        return None
    payload = attribution.to_payload()
    key = (attribution.pr_number, attribution.merge_group or "",
           attribution.sha, attribution.state)
    try:
        with store._conn(project) as c:
            rows = c.execute(
                "SELECT payload FROM activity WHERE kind='ci.attribution' "
                "ORDER BY id DESC LIMIT 200").fetchall()
        for row in rows:
            try:
                prev = json.loads(row["payload"])
            except Exception:
                continue
            prev_key = (prev.get("pr_number"), prev.get("merge_group") or "",
                        prev.get("head_sha"), prev.get("state"))
            if prev_key == key:
                return None
    except Exception:
        pass
    try:
        import store
        return store.append_activity("ci.attribution", actor, payload,
                                     task_id=task_id, project=project)
    except Exception:
        return None
