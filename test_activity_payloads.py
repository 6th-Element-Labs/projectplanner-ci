#!/usr/bin/env python3
"""Regression test for legacy/scalar task activity payloads.

Run:
    python3 test_activity_payloads.py
"""
import json
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="activity-payloads-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import agent  # noqa: E402
import store  # noqa: E402

P = "maxwell"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task({"workstream_id": "PAY", "title": "payload task"},
                             actor="test", project=P)
    with store._conn(P) as c:
        rows = [
            ("comment", json.dumps({"text": "normal object"})),
            ("comment", json.dumps("legacy scalar note")),
            ("comment", json.dumps(["legacy", "list"])),
            ("comment", "not-json legacy text"),
        ]
        for kind, payload in rows:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                      "VALUES (?,?,?,?,?)",
                      (task["task_id"], "test", kind, payload, 1.0))

    loaded = store.get_task(task["task_id"], project=P)
    payloads = [a["payload"] for a in loaded["activity"]]
    ok(payloads[-4] == {"text": "normal object"}, "object activity payload stays an object")
    ok(payloads[-3] == "legacy scalar note", "JSON string activity payload is preserved")
    ok(payloads[-2] == ["legacy", "list"], "JSON list activity payload is preserved")
    ok(payloads[-1] == {"text": "not-json legacy text"}, "malformed payload is returned as text")

    brief = agent._task_brief(loaded, full=True)
    texts = [a["text"] for a in brief["recent_activity"]]
    ok("legacy scalar note" in texts, "task brief renders scalar payload text")
    ok(["legacy", "list"] in texts, "task brief renders list payload without crashing")
    ok("not-json legacy text" in texts, "task brief renders malformed payload text")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
