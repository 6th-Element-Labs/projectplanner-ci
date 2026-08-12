#!/usr/bin/env python3
"""format.taikunai.com download filenames must be HTTP-header safe.

Uploads with Unicode names (em dash, smart quotes, accents) used to 500
after a successful rebrand/OCR: Starlette encodes Content-Disposition as
latin-1, and a raw `filename="…—….pptx"` raises UnicodeEncodeError.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401 -- installs repo and src on sys.path

from fastapi.responses import Response

from switchboard.api.routers.ops_export import attachment_content_disposition


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


def assert_header_safe(filename: str) -> None:
    header = attachment_content_disposition(filename)
    ok("attachment;" in header, f"disposition starts with attachment for {filename!r}")
    try:
        header.encode("latin-1")
        ok(True, f"disposition is latin-1 safe for {filename!r}")
    except UnicodeEncodeError as exc:
        ok(False, f"disposition latin-1 encode failed for {filename!r}: {exc}")
    # Must not raise when Starlette builds the response headers.
    try:
        Response(content=b"x", headers={"Content-Disposition": header})
        ok(True, f"Response accepts disposition for {filename!r}")
    except UnicodeEncodeError as exc:
        ok(False, f"Response rejected disposition for {filename!r}: {exc}")


# The production failure: em dash in the uploaded deck name.
assert_header_safe("Deck — Title-Taikun.pptx")
assert_header_safe("Présentation-searchable.pdf")
assert_header_safe("plain-ascii.pptx")

dash = attachment_content_disposition("Deck — Title-Taikun.pptx")
ok("filename*=utf-8''" in dash.lower() or "filename*=UTF-8''" in dash,
   "unicode names include RFC 5987 filename*")
ok("—" not in dash.split("filename*=", 1)[0],
   "ASCII filename= fallback has no raw em dash")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
