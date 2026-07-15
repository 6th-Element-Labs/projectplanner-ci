#!/usr/bin/env python3
"""Self-contained test for the reusable adapter conformance fixture."""
import os
import sys

from adapters.conformance import LocalStoreClient, print_result, run_p0_conformance


original_push_verification = os.environ.get("PM_VERIFY_COMPLETION_PUSH")
os.environ["PM_VERIFY_COMPLETION_PUSH"] = "1"
try:
    with LocalStoreClient(adapter="local-store-test", runtime="test-runtime") as client:
        push_verification_isolated = "PM_VERIFY_COMPLETION_PUSH" not in os.environ
        result = run_p0_conformance(client)
        print_result(result)
    push_verification_restored = os.environ.get("PM_VERIFY_COMPLETION_PUSH") == "1"
finally:
    if original_push_verification is None:
        os.environ.pop("PM_VERIFY_COMPLETION_PUSH", None)
    else:
        os.environ["PM_VERIFY_COMPLETION_PUSH"] = original_push_verification

if not push_verification_isolated:
    print("FAIL fixture isolates PM_VERIFY_COMPLETION_PUSH during local conformance")
if not push_verification_restored:
    print("FAIL fixture restores PM_VERIFY_COMPLETION_PUSH after local conformance")
sys.exit(0 if result.ok and push_verification_isolated and push_verification_restored else 1)
