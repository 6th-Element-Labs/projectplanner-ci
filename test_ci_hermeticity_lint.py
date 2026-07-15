#!/usr/bin/env python3
"""CI hermeticity lint — scan_source rule logic (pure, no filesystem)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import ci_hermeticity_lint as lint  # noqa: E402


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


def flags(src):
    return lint.scan_source(src, "test_sample.py")


# Injected host reads are fine ---------------------------------------------------------
ok(flags("x = compute_saturation_signals('p', psi_provider=lambda: CALM)") == [],
   "compute_saturation_signals WITH psi_provider= is hermetic")
ok(flags("y = read_psi('cpu', proc_root='/fixture')") == [],
   "read_psi WITH proc_root= is hermetic")
ok(flags("p = read_psi(\n    'cpu',\n    proc_root='/fixture',\n)") == [],
   "multi-line call with the injection kwarg is hermetic")
ok(flags("path = '/fixture/pressure'; missing = '/definitely-missing-proc-root'") == [],
   "fixture-root path literals are fine")

# Live host reads are flagged ----------------------------------------------------------
ok(len(flags("s = compute_saturation_signals('switchboard')")) == 1,
   "compute_saturation_signals WITHOUT psi_provider= is flagged")
ok(len(flags("s = compute_saturation_signals(\n    'switchboard',\n)")) == 1,
   "multi-line call missing the injection kwarg is still flagged")
ok(len(flags("import os\nl = os.getloadavg()")) == 1, "os.getloadavg() is flagged")
ok(len(flags("import psutil\nc = psutil.cpu_percent()")) == 1, "psutil.* is flagged")
ok(len(flags("open('/proc/pressure/cpu')")) == 1, "a real /proc path literal is flagged")
ok(len(flags("import requests\nrequests.get('http://x')")) == 1, "real requests.* network is flagged")
ok(len(flags("import httpx\nhttpx.get('http://x')")) == 1, "module-level httpx.get() is flagged")
ok(flags("c = httpx.AsyncClient(transport=t, base_url='http://t')") == [],
   "httpx.AsyncClient over a transport (in-process ASGI test) is NOT flagged")

# Escape hatch -------------------------------------------------------------------------
ok(flags("s = compute_saturation_signals('p')  # ci-hermetic: allow -- covered by outer stub") == [],
   "the inline escape suppresses a flagged call")
ok(len(flags("s = compute_saturation_signals(  # ci-hermetic: allow -- reason\n    'p',\n)")) == 0,
   "escape on the opening line of a multi-line call suppresses it")

# Comments / strings mentioning banned things are NOT flagged (AST, not grep) ----------
ok(flags("# this test used to call os.getloadavg() live\nx = 1") == [],
   "a banned name inside a comment is not flagged")
ok(flags("DOC = 'we no longer read psutil here'") == [],
   "a banned name inside a normal string is not flagged")

# Syntax error surfaces as a violation (fail loudly, don't skip) -----------------------
ok(len(flags("def broken(:\n  pass")) == 1, "a syntax error is reported, not silently skipped")

print("\nAll ci_hermeticity_lint tests passed.")
raise SystemExit(0)
