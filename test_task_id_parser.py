#!/usr/bin/env python3
"""Task-id extraction — shared by the webhook (Done-stamping), reconcile orphan
discovery, and the SESSION-12 provenance gate. A miss here means a merged task
never gets stamped Done, so hyphenated-workstream ids must parse."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import task_id_parser as t  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# Hyphenated workstreams (Helm North-Star: QA-L, OFFLINE-L) must parse whole.
ok(t.extract_task_ids("QA-L-2: depth-on-sat proof") == ["QA-L-2"],
   "hyphenated-workstream id QA-L-2 is extracted whole")
ok(t.extract_task_ids("OFFLINE-L-1 region bundle") == ["OFFLINE-L-1"],
   "hyphenated-workstream id OFFLINE-L-1 is extracted whole")
ok(t.extract_task_ids("ship LAYER-1 and SESSION-12") == ["LAYER-1", "SESSION-12"],
   "single-hyphen ids still parse (no regression)")

# Unit/spec tokens with a 1-char first segment must NOT be mistaken for task ids.
ok(t.extract_task_ids("render the S-52 palette at A-1 scale") == [],
   "S-52 / A-1 are not task ids (first segment needs 2+ chars)")

# Closing keywords honor the same shape.
ok(t.closing_task_ids("Closes QA-L-2 and fixes OFFLINE-L-1") == ["QA-L-2", "OFFLINE-L-1"],
   "closes/fixes recognize hyphenated-workstream ids")
ok(t.closing_task_ids("mentions QA-L-2 but does not close it") == [],
   "a bare mention is not a closing reference")

# End-to-end PR parse: branch + title, deduped, uppercased.
pr = {"title": "QA-L-2: screenshot proof",
      "head": {"ref": "cursor/QA-L-2-depth-on-sat-screenshot"},
      "body": "Closes QA-L-2"}
ok(t.task_ids_for_pr(pr) == ["QA-L-2"],
   "task_ids_for_pr resolves a hyphenated-workstream PR (webhook can now stamp it)")

pr2 = {"title": "LAYER-5: opencpn s-52 aids symbols on satellite",
       "head": {"ref": "cursor/LAYER-5-aids"}, "body": ""}
ok(t.task_ids_for_pr(pr2) == ["LAYER-5"],
   "an S-52 substring inside a valid PR does not add a phantom task id")

print(f"\ntask id parser: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
