#!/usr/bin/env python3
"""BUG-99: the installed macOS service carries a PATH that resolves gh/codex.

launchd's default PATH omits Homebrew, so runner sessions could not resolve gh
by name. Completion became nondeterministic: SEG-2's session hand-rolled a
urllib call against the GitHub pulls API to open PR #653 while SEG-5 honestly
blocked on "gh is not installed in this runtime" with pushed, test-green work.
The service environment must make the finishing step boring and identical for
every session.
"""
from __future__ import annotations

import plistlib
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from adapters import agent_host_enrollment as enrollment

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


with tempfile.TemporaryDirectory(prefix="bug99-") as tmp_raw:
    tmp = Path(tmp_raw)
    service_path = tmp / "com.6thelement.switchboard-agent-host.plist"
    enrollment.render_service(
        "darwin", python="/usr/bin/python3",
        entrypoint=tmp / "entry.py",
        identity_path=tmp / "identity.json",
        config_path=tmp / "config.json",
        service_path=service_path,
        log_root=tmp / "logs",
    )
    payload = plistlib.loads(service_path.read_bytes())
    env = payload.get("EnvironmentVariables") or {}
    path = str(env.get("PATH") or "")
    ok(bool(path), "the rendered macOS service sets an explicit PATH")
    ok("/opt/homebrew/bin" in path.split(":"),
       "Homebrew (apple silicon) is on the service PATH so gh/codex resolve by name")
    ok("/usr/local/bin" in path.split(":"),
       "Homebrew (intel) / local installs are on the service PATH")
    ok(path.split(":")[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"],
       "system directories remain, after the local tool directories")
    ok(payload.get("ProgramArguments", [None])[0] == "/usr/bin/python3"
       and payload.get("KeepAlive") == {"SuccessfulExit": False},
       "the rest of the service definition is unchanged")

    template = (Path(ROOT) / "deploy" / "agent-host" / "launchd.plist.in").read_text()
    ok("EnvironmentVariables" in template and "/opt/homebrew/bin" in template,
       "the reference template matches what the installer actually renders")

print(f"\nBUG-99 runner PATH: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
