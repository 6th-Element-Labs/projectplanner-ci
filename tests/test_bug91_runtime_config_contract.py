#!/usr/bin/env python3
"""BUG-91: the CO-fleet runtime config contract is enforced by the repo.

The AWS workers build their whole command line from a single SSM SecureString
that is not in version control. Nothing described its shape, so a wrong value
sat in production undetected: PM_AGENT_WORK_MODULE_CODEX pointed at
claude_personal_worker, and PM_AUTO_WORK_SESSION was missing -- which is why
80/84 claim_next runner rows carried no claim_id and no work_session_id.

PM_AUTO_WORK_SESSION was not even in the bootstrap allowlist, so the fix was not
expressible in config at all. These assertions keep both facts from regressing,
and keep the documented schema honest about what the bootstrap really accepts.
"""
from __future__ import annotations

import re
from pathlib import Path

from path_setup import ROOT  # noqa: F401

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


CO_FLEET = (Path(ROOT) / "co_fleet.py").read_text(encoding="utf-8")
DOC_PATH = Path(ROOT) / "deploy" / "co-fleet-runtime-config.md"

ok(DOC_PATH.exists(),
   "the SSM runtime-config schema is recorded in the repo, not in tribal knowledge")
DOC = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""

# The allowlist inside the bootstrap's embedded python heredoc.
match = re.search(r"allowed = \{\{(.*?)\}\}", CO_FLEET, re.S)
ok(match is not None, "the bootstrap allowlist is still parseable from co_fleet.py")
allowed = set(re.findall(r'"([A-Z0-9_]+)"', match.group(1) if match else ""))

ok("PM_AUTO_WORK_SESSION" in allowed,
   "PM_AUTO_WORK_SESSION is allowlisted — without it a code_strict task is never "
   "claimed and its runner can never satisfy the Watch bind contract")
ok("PM_AGENT_WORK_MODULE_CODEX" in allowed,
   "the codex work module remains configurable per runtime")

# Anything the bootstrap accepts should be discoverable by whoever edits the
# parameter; an undocumented key is how the original defect stayed invisible.
undocumented = sorted(key for key in allowed if key not in DOC)
ok(not undocumented,
   f"every allowlisted key is documented in the schema (undocumented: {undocumented})")

# The doc must name the correct module, and must not present the broken one as
# the value to use.
ok("adapters.codex_local_worker:run" in DOC,
   "the schema records the correct codex work module")
correct_block = DOC.split("## Correct values", 1)[-1].split("##", 1)[0] if "## Correct values" in DOC else ""
ok("claude_personal_worker" not in correct_block,
   "the broken module never appears as a prescribed value")
ok("claude_personal_worker" in DOC,
   "the broken module is still recorded as the observed defect, so the history is not lost")

# Rollback must be a recorded procedure, not an investigation.
for needle, label in (
        ("Parameter.Version", "reading the current version before overwriting"),
        ("Rolling back", "a rollback procedure"),
        ("Deployment record", "a deployment record table")):
    ok(needle in DOC, f"the schema documents {label}")

ok("do **not** re-read this parameter" in DOC.lower()
   or "not** re-read" in DOC.lower() or "re-read this parameter" in DOC,
   "the doc warns that running instances do not re-read the parameter, so a "
   "rollback alone does not fix already-booted hosts")

# The forbidden-credential guard is a security property; keep it asserted.
for forbidden in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"):
    ok(forbidden in CO_FLEET and forbidden in DOC,
       f"{forbidden} stays rejected by the bootstrap and documented as rejected")

print(f"\nBUG-91 runtime config contract: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
