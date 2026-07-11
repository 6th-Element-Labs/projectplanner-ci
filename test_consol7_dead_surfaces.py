#!/usr/bin/env python3
"""CONSOL-7/ARCH-MS-11: retired surfaces stay deleted."""
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

DELETED_AUTH = (
    "static/login.html",
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

for rel in DELETED_AUTH:
    ok(not (ROOT / rel).exists(), f"{rel} removed (global auth is the only browser login)")

auth_runtime_paths = [
    ROOT / "app.py",
    ROOT / "auth.py",
    *(ROOT / "src/switchboard/api/routers/auth").glob("*.py"),
    *(path for path in (ROOT / "deploy").rglob("*")
      if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"),
]
for path in auth_runtime_paths:
    text = path.read_text(encoding="utf-8")
    ok("PM_GLOBAL_AUTH" not in text,
       f"{path.relative_to(ROOT)} cannot restore the retired global-auth feature gate")

app_source = (ROOT / "app.py").read_text(encoding="utf-8")
ok("app.include_router(_global_auth_router)" in app_source,
   "global auth router remains mounted unconditionally")
ok("/api/auth/bootstrap" not in app_source,
   "legacy per-project auth bootstrap route remains deleted")
ok((ROOT / "static/login-global.html").exists(),
   "the single global browser login remains present")

ok(not (ROOT / "Maxwell-Pitch-Deck.pptx").exists(),
   "pitch deck moved out of repo root")
ok((ROOT / "assets/Maxwell-Pitch-Deck.pptx").exists(),
   "pitch deck lives under assets/")

ok(not (ROOT / "gmail_source.py").exists(),
   "ARCH-MS-11 retired the Gmail-specific source module")
ok((ROOT / "inbox_source.py").exists(),
   "the source-independent IMAP adapter remains live")
ok((ROOT / "src/switchboard/integrations/inbox_routing.py").exists(),
   "inbox routing policy lives in the integrations package")

for rel in SUPERSEDED_MARKERS:
    text = (ROOT / rel).read_text(encoding="utf-8")
    ok("SUPERSEDED" in text, f"{rel} carries a SUPERSEDED banner")

caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
ok("rebrand.html" not in caddy or "CONSOL-7" in caddy,
   "Caddyfile documents retired rebrand/ocr surfaces")

print("CONSOL-7 dead-surface checks passed")
