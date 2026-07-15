#!/usr/bin/env python3
"""CI hermeticity lint — keep unit tests off live host state so flakes can't own the queue.

A flaky test is far worse under a merge queue: one non-deterministic failure blocks the whole
train, not a single PR (this is the BUG-67 class — a test read live /proc PSI, and CI runners
under load tripped its saturation thresholds). This lint scans ``test_*.py`` / ``*_test.py`` with
Python's AST (so it ignores matches inside comments/strings and understands multi-line calls) and
fails CI when a test reaches for the real host:

  * ``compute_saturation_signals(...)`` / ``read_psi(...)`` / ``read_all_psi(...)`` WITHOUT the
    injection kwarg (``psi_provider=`` / ``proc_root=``) — i.e. it reads live /proc instead of a
    fixture. The same call WITH injection is fine and passes.
  * ``os.getloadavg``, any ``psutil.*`` call — live host load.
  * string literals under ``/proc/`` or ``/sys/`` — a real host path (fixture roots like
    ``/fixture`` or ``/definitely-missing-proc-root`` are fine).
  * ``requests.*`` / ``httpx.*`` / ``urllib.request.urlopen`` — real network in a unit test.

It is a HEURISTIC: it raises the floor against the known non-hermetic classes, it does not prove
hermeticity (it won't catch, e.g., subtle inter-test ordering). Escape hatch for a genuine,
handled exception: put ``# ci-hermetic: allow -- <reason>`` anywhere on the offending call's
line span, or add the file to ``FILE_ALLOWLIST`` below with a reason.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import Dict, List, Optional, Set, Tuple

# funcs that read live host state UNLESS given their injection kwarg
INJECTABLE: Dict[str, str] = {
    "compute_saturation_signals": "psi_provider",
    "read_psi": "proc_root",
    "read_all_psi": "proc_root",
}
# call names that are always a live host read in a unit test (any receiver)
BANNED_CALL_NAMES: Set[str] = {"getloadavg"}
# receiver roots whose calls are non-hermetic
HOST_ROOTS: Set[str] = {"psutil"}
NETWORK_ROOTS: Set[str] = {"requests", "httpx"}
# Only module-level REQUEST verbs hit the network. Client/AsyncClient/Session constructors do
# not (an httpx.AsyncClient over an ASGITransport(app=app) is the in-process FastAPI test
# pattern and is perfectly hermetic), so they are not flagged.
NETWORK_VERBS: Set[str] = {"get", "post", "put", "delete", "patch", "head",
                           "options", "request", "stream"}
LIVE_PATH_PREFIXES = ("/proc/", "/sys/")
ESCAPE = "ci-hermetic: allow"

# Documented, tracked exceptions (empty = none). Keep this list honest and short.
FILE_ALLOWLIST: Dict[str, str] = {}


def _call_target(node: ast.Call) -> Tuple[Optional[str], Optional[str]]:
    """(name, receiver_root) for a Call, e.g. os.getloadavg() -> ('getloadavg','os')."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        root = func.value.id if isinstance(func.value, ast.Name) else None
        return func.attr, root
    return None, None


def scan_source(source: str, filename: str = "<test>") -> List[Tuple[int, str]]:
    """Return sorted ``(lineno, message)`` hermeticity violations for one test source. Pure —
    no filesystem or network — so the rule logic is unit-tested directly."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [(getattr(exc, "lineno", 0) or 0, f"syntax error: {exc.msg}")]

    lines = source.splitlines()

    def escaped(node: ast.AST) -> bool:
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", getattr(node, "lineno", 1))
        return any(ESCAPE in lines[i] for i in range(max(0, start), min(len(lines), end)))

    out: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(LIVE_PATH_PREFIXES) and not escaped(node):
                out.append((node.lineno, f"live host path literal {node.value!r} — "
                                         f"read a fixture root, not real /proc or /sys"))
        elif isinstance(node, ast.Call):
            name, root = _call_target(node)
            if name is None:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if name in INJECTABLE and INJECTABLE[name] not in kwargs:
                if not escaped(node):
                    out.append((node.lineno, f"{name}() without {INJECTABLE[name]}= reads live "
                                             f"host state — inject a fixture (see test_saturation_signals.py)"))
            elif root in HOST_ROOTS or name in BANNED_CALL_NAMES:
                if not escaped(node):
                    label = f"{root + '.' if root else ''}{name}()"
                    out.append((node.lineno, f"{label} reads live host state in a unit test — mock/inject it"))
            elif root in NETWORK_ROOTS and name in NETWORK_VERBS:
                if not escaped(node):
                    out.append((node.lineno, f"{root}.{name}() does real network in a unit test — mock it "
                                             f"(an app-/ASGITransport-bound client is fine)"))
    return sorted(out)


def _discover_test_files(root: str) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "__pycache__")]
        for fn in filenames:
            if (fn.startswith("test_") or fn.endswith("_test.py")) and fn.endswith(".py"):
                found.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(found)


def main(argv: Optional[List[str]] = None) -> int:
    root = (argv or sys.argv[1:] or ["."])[0]
    violations = 0
    for rel in _discover_test_files(root):
        if rel in FILE_ALLOWLIST:
            print(f"ALLOW {rel} ({FILE_ALLOWLIST[rel]})")
            continue
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            print(f"WARN  cannot read {rel}: {exc}", file=sys.stderr)
            continue
        for lineno, msg in scan_source(source, rel):
            print(f"{rel}:{lineno}: {msg}", file=sys.stderr)
            violations += 1
    if violations:
        print(f"\nhermeticity: {violations} violation(s) — tests must not read live host "
              f"state/network. Inject a fixture, or annotate with '# {ESCAPE} -- <reason>'.",
              file=sys.stderr)
        return 1
    print("hermeticity: clean — no live host/network reads in tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
