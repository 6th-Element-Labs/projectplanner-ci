#!/usr/bin/env python3
"""Architecture guard for ARCH-MS-11 inbox routing extraction."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


ok(not (ROOT / "gmail_source.py").exists(), "gmail_source.py is retired")
ok((ROOT / "inbox_source.py").is_file(), "the IMAP source adapter remains available")

routing_path = ROOT / "src/switchboard/integrations/inbox_routing.py"
ok(routing_path.is_file(), "routing policy is extracted under switchboard.integrations")

source_text = (ROOT / "inbox_source.py").read_text(encoding="utf-8")
source_tree = ast.parse(source_text)
source_functions = {
    node.name for node in source_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
ok(not ({"route", "routes_map", "plus_project", "domain_project", "allow_sender"} & source_functions),
   "the IMAP adapter does not own routing policy")
ok("inbox_routing.route(sender, recipients)" in source_text,
   "the IMAP adapter delegates routing through the package seam")

for caller in ("app.py", "jobs.py"):
    text = (ROOT / caller).read_text(encoding="utf-8")
    ok("import inbox_source" in text, f"{caller} uses the renamed adapter")
    ok("gmail_source" not in text, f"{caller} has no retired module reference")

print("ARCH-MS-11 inbox routing architecture checks passed")
