"""COORD-54: an exception-derived status must preserve the exception message."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from path_setup import ROOT


STATUS_KEYS = {"error", "reason", "status"}
WAIVER_KEY = "diagnostic_cause_waiver"
SCAN_ROOTS = (Path(ROOT) / "src" / "switchboard", Path(ROOT) / "scripts")


def _references(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _renders_message(node: ast.AST, name: str) -> bool:
    """True when an expression includes the exception text, not only its class."""
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(item, ast.FormattedValue) and _references(item.value, name)
            and not (
                isinstance(item.value, ast.Call)
                and isinstance(item.value.func, ast.Name)
                and item.value.func.id == "type"
            )
            for item in node.values
        )
    for item in ast.walk(node):
        if not isinstance(item, ast.Call) or not item.args:
            continue
        if (
            isinstance(item.func, ast.Name)
            and item.func.id in {"str", "repr"}
            and isinstance(item.args[0], ast.Name)
            and item.args[0].id == name
        ):
            return True
    return False


def _literal_key(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def diagnostic_discards(source: str, filename: str = "<source>") -> list[str]:
    """Find except-block dicts that expose exception status but discard its message.

    A trust-boundary waiver is deliberately data, not a magic noqa comment: use a
    non-empty ``diagnostic_cause_waiver`` field so the omission survives into the
    operator-facing result and can be audited.
    """
    tree = ast.parse(source, filename=filename)
    failures: list[str] = []
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
        if not handler.name:
            continue
        exception_name = handler.name
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if not isinstance(node, ast.Dict):
                continue
            fields = {
                _literal_key(key): value
                for key, value in zip(node.keys, node.values)
                if _literal_key(key)
            }
            derived_status = any(
                key in STATUS_KEYS and _references(value, exception_name)
                for key, value in fields.items()
            )
            if not derived_status:
                continue
            waiver = fields.get(WAIVER_KEY)
            if waiver is not None and isinstance(waiver, ast.Constant) and waiver.value:
                continue
            if any(_renders_message(value, exception_name) for value in fields.values()):
                continue
            failures.append(f"{filename}:{node.lineno}")
    return failures


class DiagnosticCauseLintTest(unittest.TestCase):
    def test_deliberately_introduced_discard_fails_the_lint(self):
        bad = """
try:
    operation()
except Exception as exc:
    result = {"status": "degraded", "error": type(exc).__name__}
"""
        self.assertEqual(diagnostic_discards(bad), ["<source>:5"])

    def test_message_or_named_trust_boundary_waiver_passes(self):
        with_message = """
try:
    operation()
except Exception as exc:
    result = {"error": type(exc).__name__, "message": str(exc)}
"""
        waived = """
try:
    operation()
except SecretError as exc:
    result = {
        "error": type(exc).__name__,
        "diagnostic_cause_waiver": "secret may contain credential material",
    }
"""
        self.assertEqual(diagnostic_discards(with_message), [])
        self.assertEqual(diagnostic_discards(waived), [])

    def test_repository_exception_statuses_preserve_their_cause(self):
        failures: list[str] = []
        for root in SCAN_ROOTS:
            for path in root.rglob("*.py"):
                failures.extend(
                    diagnostic_discards(
                        path.read_text(encoding="utf-8"),
                        str(path.relative_to(ROOT)),
                    )
                )
        self.assertEqual(
            failures,
            [],
            "exception-derived error/reason/status dicts must include str(exc), "
            "repr(exc), an f-string rendering of the exception, or a named "
            "diagnostic_cause_waiver: " + ", ".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
