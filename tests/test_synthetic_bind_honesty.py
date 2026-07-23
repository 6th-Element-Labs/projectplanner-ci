#!/usr/bin/env python3
"""A transport placeholder must never read as ownership evidence.

SIMPLIFY-18 follow-up (ADR-0008 C1: claims, Work Sessions and messages may not be
impersonated). Relay tickets for native/relay-attached runners substitute a
`direct/<runner_session_id>` placeholder where no real claim, Work Session,
execution connection, or source SHA exists — otherwise the ticket bind shape
cannot be satisfied and Watch breaks.

That substitution is legitimate transport plumbing, but it previously looked
identical to a real record. Anything auditing "which claim was this watch session
under?" got a fiction it could not detect. This pins the placeholder as
explicitly labelled and separable.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from switchboard.domain import execution_liveness as live  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("synthetic bind honesty")

ok(live.SYNTHETIC_BIND_PREFIX == "direct/",
   "the historical placeholder prefix is stable (already serialized in tickets)")
ok(live.is_synthetic_bind_ref("direct/run_abc") is True,
   "a placeholder ref is recognised as synthetic")
ok(live.is_synthetic_bind_ref("taskclaim-1234") is False
   and live.is_synthetic_bind_ref("") is False
   and live.is_synthetic_bind_ref(None) is False,
   "real claim ids, empty and None are not synthetic")

real = {
    "task_id": "SIMPLIFY-18",
    "claim_id": "taskclaim-real",
    "work_session_id": "worksession-real",
    "execution_connection_id": "execconn-real",
    "source_sha": "a" * 40,
}
ok(live.synthetic_bind_fields(real) == [],
   "a fully real binding reports no synthetic fields")

mixed = dict(real, claim_id="direct/run_abc", work_session_id="direct/run_abc")
ok(live.synthetic_bind_fields(mixed) == ["claim_id", "work_session_id"],
   "exactly the substituted fields are named, not the whole binding")

# The point of the label: a placeholder must never be mistaken for ownership.
ok(not any(live.is_synthetic_bind_ref(v) for v in real.values()),
   "a real binding carries no placeholder in any field")
ok(live.is_synthetic_bind_ref(mixed["claim_id"]),
   "a substituted claim_id is detectable as fiction rather than a claim")

# The relay response must surface it, not just compute it internally.
runner_src = (ROOT / "src/switchboard/storage/repositories/runner.py").read_text(
    encoding="utf-8")
ok('"synthetic_bind": bool(substituted)' in runner_src
   and '"synthetic_bind_fields": substituted' in runner_src,
   "the relay ticket response declares whether its bind was synthetic")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
