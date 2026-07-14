#!/usr/bin/env python3
"""BUG-65: repeated least-privilege setup uses the CI checkout's owner for Git."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT

SCRIPT = ROOT / "deploy" / "apply-least-privilege.sh"
passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


def executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


with tempfile.TemporaryDirectory(prefix="bug65-redeploy-") as tmp:
    base = Path(tmp)
    fake_bin = base / "bin"
    code_root = base / "code"
    data_root = base / "data"
    marker = base / "data-owner-ready"
    calls = base / "calls.log"
    fake_bin.mkdir()
    code_root.mkdir()

    executable(fake_bin / "id", "#!/bin/sh\necho 0\n")
    executable(fake_bin / "getent", "#!/bin/sh\nexit 0\n")
    executable(fake_bin / "groupadd", "#!/bin/sh\nexit 90\n")
    executable(fake_bin / "useradd", "#!/bin/sh\nexit 90\n")
    executable(
        fake_bin / "chown",
        """#!/bin/sh
printf 'chown %s\\n' "$*" >> "$BUG65_CALLS"
case "$*" in
  *"$PM_DATA_ROOT") : > "$BUG65_OWNER_MARKER" ;;
esac
""",
    )
    executable(fake_bin / "chmod", "#!/bin/sh\nexit 0\n")
    executable(
        fake_bin / "runuser",
        """#!/bin/sh
[ "$1" = "--user" ] && [ "$2" = "$PM_SERVICE_USER" ] && [ "$3" = "--" ] || exit 93
[ -f "$BUG65_OWNER_MARKER" ] || exit 94
shift 3
printf 'runuser %s\\n' "$*" >> "$BUG65_CALLS"
BUG65_AS_SERVICE_USER=1 exec "$@"
""",
    )
    executable(
        fake_bin / "git",
        """#!/bin/sh
[ "${BUG65_AS_SERVICE_USER:-}" = "1" ] || {
  echo "root Git touched the service-owned checkout" >&2
  exit 95
}
printf 'git %s\\n' "$*" >> "$BUG65_CALLS"
if [ "$1" = "clone" ]; then
  destination=""
  for argument in "$@"; do destination="$argument"; done
  mkdir -p "$destination/.git"
fi
""",
    )

    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "PM_SERVICE_USER": "projectplanner",
        "PM_SERVICE_GROUP": "projectplanner",
        "PM_CODE_ROOT": str(code_root),
        "PM_DATA_ROOT": str(data_root),
        "SWITCHBOARD_CI_SOURCE_PATH": str(data_root / "ci-source"),
        "SWITCHBOARD_CI_SOURCE_REMOTE": "git@example.invalid/projectplanner.git",
        "BUG65_CALLS": str(calls),
        "BUG65_OWNER_MARKER": str(marker),
    })

    runs = []
    for _ in range(2):
        marker.unlink(missing_ok=True)
        runs.append(subprocess.run(
            ["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True,
            capture_output=True, timeout=10, check=False,
        ))

    log = calls.read_text(encoding="utf-8")
    log_lines = log.splitlines()
    git_calls = [line for line in log_lines if line.startswith("git ")]
    runuser_calls = [line for line in log_lines if line.startswith("runuser ")]
    ok(all(run.returncode == 0 for run in runs),
       "fresh provisioning and the second redeploy both succeed")
    ok(sum("clone --no-checkout" in line for line in git_calls) == 1,
       "the coordination checkout is cloned only on the first run")
    ok(len(git_calls) == 5 and len(runuser_calls) == 5,
       "all checkout Git operations run through the service identity")
    ok(log.count(f"chown -R projectplanner:projectplanner {data_root}") == 2,
       "data ownership is re-asserted before Git on every run")
    ok("safe.directory" not in SCRIPT.read_text(encoding="utf-8"),
       "the fix does not weaken Git ownership checks with a safe.directory exception")

print(f"\nBUG-65 redeploy idempotency: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
