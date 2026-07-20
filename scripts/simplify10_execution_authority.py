#!/usr/bin/env python3
"""SIMPLIFY-10: one execution authority — the ratchet.

Outside the task-execution service (and the launcher, persistence, host
adapters, and host protocol routers it owns), no production path may:

  * assemble a wake intent,
  * select which host runs a task,
  * author an assignment payload as a source of truth,
  * resolve which runner is current.

Every scope is tighten-only: lowering a measured count must lower the ceiling in
the same change; raising one needs an explicit justification in the PR. Scopes
that are already at zero are the ones SIMPLIFY-10 migrated; the non-zero ones
name the task that owns driving them down.

Usage:
    python3 scripts/simplify10_execution_authority.py [--json] [--sites]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "perf" / "simplify10_execution_authority_baseline.json"

#: Directories that are never production execution paths.
EXCLUDED_PREFIXES = (
    "tests/", "docs/", "deploy/", "scripts/", "perf/", "adr/", ".github/",
    "node_modules/", ".venv/",
)
EXCLUDED_NAMES = ("conftest.py",)

PATTERNS: dict[str, re.Pattern[str]] = {
    # Building a wake intent, rather than asking the service to start a task.
    # Deliberately matches the bare attribute too: handing ``store.request_wake``
    # to something else as a callable leaks the same authority as calling it.
    "wake_assembly": re.compile(
        r"\b(?:store|store_mod|coordination|coordination_repo)\.request_wake\b",
    ),
    # Choosing which host will run the work.
    "host_selection": re.compile(
        r"\b(?:_personal_host_target|_work_hosts|select_pool|provision_wake)\s*\(",
    ),
    # Authoring the assignment payload the host executes from.
    "assignment_authoring": re.compile(
        r"(?:_write_assignment_toml\s*\(|assignment\.toml[\"']\s*\)?\s*\.\s*write|"
        r"open\s*\([^)]*assignment\.toml)",
    ),
    # Deciding which runner is current — the SIMPLIFY-1 projection's job.
    "runner_resolution": re.compile(
        r"\b(?:resolve_runner_watch|resolve_task_active_runner|latest_dispatch_outcome)\s*\(",
    ),
    # Reaching the launcher directly instead of through a command.
    "legacy_launcher": re.compile(r"\bdispatch(?:_mod)?\.(?:dispatch|start_task|"
                                  r"resume_review|dispatch_to_co_fleet|"
                                  r"latest_from_task_session)\s*\("),
}

#: Browser sites that use execution identity the server owns.
BROWSER_PATTERN = re.compile(
    r"/ixp/v1/request_wake|/ixp/v1/cancel_wake|/ixp/v1/request_runner_"
    r"|/ixp/v1/runner_sessions/watch|/pty/ticket|/ixp/v1/runner_controls"
)

SCOPE_PATTERNS = {
    "wake_assembly_outside_service": ("wake_assembly",),
    "host_selection_outside_service": ("host_selection",),
    "assignment_authoring_outside_service": ("assignment_authoring",),
    "runner_resolution_outside_service": ("runner_resolution",),
    "legacy_launcher_calls_outside_service": ("legacy_launcher",),
}


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _service_files(baseline: dict) -> set[str]:
    service = baseline.get("service") or {}
    files: set[str] = set()
    for key, value in service.items():
        if key == "description":
            continue
        if isinstance(value, str):
            files.add(value)
        elif isinstance(value, list):
            files.update(str(item) for item in value)
    return files


def _production_files(suffix: str) -> list[Path]:
    out: list[Path] = []
    for path in sorted(ROOT.rglob(f"*{suffix}")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(".git/") or any(rel.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        if path.name in EXCLUDED_NAMES or path.name.startswith("test_"):
            continue
        out.append(path)
    return out


def _prose_lines(path: Path, text: str) -> set[int]:
    """Line numbers that are only comment or string literal — never a call site.

    Docstrings that *name* a forbidden call are documentation, not authority, so
    a module explaining "this used to call dispatch.start_task()" must not read
    as a violation.
    """
    if path.suffix != ".py":
        return set()
    prose: set[int] = set()
    code: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    for token in tokens:
        start, end = token.start[0], token.end[0]
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            prose.update(range(start, end + 1))
        elif token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENDMARKER):
            code.update(range(start, end + 1))
    return prose - code


def _matches(path: Path, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return hits
    prose = _prose_lines(path, text)
    for number, line in enumerate(text.splitlines(), start=1):
        if number in prose:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
            continue  # a comment naming a forbidden call is documentation
        if pattern.search(line):
            hits.append((number, stripped[:160]))
    return hits


def measure() -> dict:
    baseline = _load_baseline()
    service = _service_files(baseline)
    scopes = baseline.get("scopes") or {}
    report: dict = {"schema": baseline.get("schema"), "task": baseline.get("task"),
                    "ok": True, "checks": {}, "scopes": {}}

    python_files = _production_files(".py")
    for scope_name, pattern_names in SCOPE_PATTERNS.items():
        declared = scopes.get(scope_name)
        if declared is None:
            continue
        sites: list[dict] = []
        for path in python_files:
            rel = path.relative_to(ROOT).as_posix()
            if rel in service:
                continue
            for pattern_name in pattern_names:
                for number, line in _matches(path, PATTERNS[pattern_name]):
                    sites.append({"file": rel, "line": number, "code": line})
        # One file may hold several call sites of the same defect; the ceiling
        # counts files so a refactor inside an already-known file is not a
        # false regression, and a NEW file always is.
        files = sorted({site["file"] for site in sites})
        report["scopes"][scope_name] = {
            "measured": len(files), "ceiling": int(declared.get("ceiling", 0)),
            "files": files, "sites": sites,
        }

    browser_declared = scopes.get("browser_execution_facts")
    if browser_declared is not None:
        sites = []
        for path in _production_files(".js"):
            rel = path.relative_to(ROOT).as_posix()
            if not rel.startswith("static/"):
                continue
            for number, line in _matches(path, BROWSER_PATTERN):
                sites.append({"file": rel, "line": number, "code": line})
        report["scopes"]["browser_execution_facts"] = {
            "measured": len(sites), "ceiling": int(browser_declared.get("ceiling", 0)),
            "files": sorted({site["file"] for site in sites}), "sites": sites,
        }

    surface = scopes.get("task_execution_surface")
    if surface is not None:
        sites = []
        for rel in surface.get("files") or []:
            path = ROOT / rel
            if not path.is_file():
                sites.append({"file": rel, "line": 0, "code": "missing file"})
                continue
            for name in ("wake_assembly", "host_selection",
                         "assignment_authoring", "runner_resolution"):
                for number, line in _matches(path, PATTERNS[name]):
                    sites.append({"file": rel, "line": number, "code": line,
                                  "pattern": name})
        report["scopes"]["task_execution_surface"] = {
            "measured": len(sites), "ceiling": int(surface.get("ceiling", 0)),
            "files": sorted({site["file"] for site in sites}), "sites": sites,
        }

    for name, scope in report["scopes"].items():
        within = scope["measured"] <= scope["ceiling"]
        report["checks"][name] = within
        if not within:
            report["ok"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the raw report")
    parser.add_argument("--sites", action="store_true", help="list every match")
    args = parser.parse_args()
    report = measure()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    for name, scope in sorted(report["scopes"].items()):
        mark = "ok  " if report["checks"][name] else "FAIL"
        print(f"[{mark}] {name}: {scope['measured']}/{scope['ceiling']}")
        if args.sites:
            for site in scope["sites"]:
                print(f"         {site['file']}:{site['line']}  {site['code']}")
        elif scope["measured"] > scope["ceiling"]:
            for site in scope["sites"]:
                print(f"         {site['file']}:{site['line']}  {site['code']}")
    print("\nexecution authority:", "OK" if report["ok"] else "REGRESSED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
