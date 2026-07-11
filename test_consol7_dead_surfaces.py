#!/usr/bin/env python3
"""CONSOL-7: dead surfaces stay deleted; store-decomposition docs stay bannered.

gmail_source.py is intentionally live (UI-13/UI-14 inbox poll) and is retired in
ARCH-MS-11 after ARCH-MS-10's PM_* flag census — not part of this cut.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent

DELETED_STATIC = (
    "static/index-legacy.html",
    "static/rebrand.html",
    "static/ocr.html",
)

DELETED_RUNNER = (
    "runner/service.py",
    "runner/run_task.sh",
    "runner/README.md",
)

SUPERSEDED_MARKERS = (
    "docs/SWITCHBOARD-STORE-DECOMPOSITION.md",
    "docs/SWITCHBOARD-STORE-ENDSTATE.md",
)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


for rel in DELETED_STATIC:
    ok(not (ROOT / rel).exists(), f"{rel} removed (format.html supersedes rebrand/ocr)")

for rel in DELETED_RUNNER:
    ok(not (ROOT / rel).exists(), f"{rel} removed (wake substrate retired runner push-bridge)")

ok(not (ROOT / "Maxwell-Pitch-Deck.pptx").exists(),
   "pitch deck moved out of repo root")
ok((ROOT / "assets/Maxwell-Pitch-Deck.pptx").exists(),
   "pitch deck lives under assets/")

for rel in SUPERSEDED_MARKERS:
    text = (ROOT / rel).read_text(encoding="utf-8")
    ok("SUPERSEDED" in text, f"{rel} carries a SUPERSEDED banner")

caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
ok("rebrand.html" not in caddy or "CONSOL-7" in caddy,
   "Caddyfile documents retired rebrand/ocr surfaces")

print("CONSOL-7 dead-surface checks passed")
