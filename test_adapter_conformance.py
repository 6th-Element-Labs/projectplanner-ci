#!/usr/bin/env python3
"""Self-contained test for the reusable adapter conformance fixture."""
import sys

from adapters.conformance import LocalStoreClient, print_result, run_p0_conformance


with LocalStoreClient(adapter="local-store-test", runtime="test-runtime") as client:
    result = run_p0_conformance(client)
    print_result(result)
    sys.exit(0 if result.ok else 1)
