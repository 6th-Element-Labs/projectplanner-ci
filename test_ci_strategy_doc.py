#!/usr/bin/env python3
"""CI-12 — guard docs/CI-STRATEGY.md scratchpad routing narrative."""
from pathlib import Path

passed = failed = 0
doc = Path("docs/CI-STRATEGY.md").read_text(encoding="utf-8")


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


INVARIANT = """## The provenance invariant (non-negotiable)

1. `canonical` is the **only** repo that can mark a task Done / carry merge-provenance.
2. Every other route posts **verification evidence only** (a commit status / `external_ci_run`), never Done.
3. The merge webhook + reconcile stamp Done **only** from the canonical default-branch merge.
4. `external_ci_mirror` verifies the **exact source SHA** on the mirror — the tested code *is* the code that merges.

This is why Route A is safe for private code: the public mirror is a disposable test runner that can never speak for "Done."
"""

ok(INVARIANT in doc, "provenance invariant section is preserved verbatim")
ok("push-triggered scratchpad" in doc and "verify.yml" in doc and "PRIVATE_READ_TOKEN" in doc,
   "projectplanner scratchpad route is documented")
ok("Helm routing is **unchanged**" in doc or "Helm keeps Route A-push unchanged" in doc,
   "Helm push-path routing called out as unchanged")
ok("0010-ci-concurrency.md" in doc and "2026-07-12" in doc and "bare-mirror" in doc.lower(),
   "2026-07-12 post-mortem context cross-linked")
ok("push_triggered=True" in doc and "Rollback bridge" in doc and "Heartbeat" in doc,
   "trigger decision records push-primary, rollback bridge, and heartbeat")
ok("bare mirror" not in doc.lower() or "retired" in doc.lower(),
   "push-path/bare-mirror is retired narrative, not active instructions")
ok("Switchboard / merge authorization" in doc,
   "Plan VM projects the exact-head merge gate as a PR status")
ok("external_ci_mirror` (Helm" in doc or "A-push" in doc,
   "push-path engine retained for Helm")
ok("SWITCHBOARD_CI_PULL_MODEL` is no longer the primary route" in doc,
   "pull-model feature flag is explicitly retired as projectplanner primary")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
