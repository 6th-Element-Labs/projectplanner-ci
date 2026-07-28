#!/usr/bin/env python3
"""Queue dwell: the dock must show how long a PR has sat in the merge queue.

The dock previously showed the PR's updated_at ("1h ago"), which moves for
unrelated reasons and told the operator nothing about being stuck in the queue.
GitHub's mergeQueueEntry.enqueuedAt is the only honest source.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: E402

import open_prs  # noqa: E402

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


REPO = "acme/widgets"


def _graphql(query, token):
    ok("enqueuedAt" in query, "the queue query must request enqueuedAt")
    return {"data": {"repository": {"mergeQueue": {"entries": {"nodes": [
        {"position": 1, "enqueuedAt": "2026-07-28T06:00:00Z",
         "pullRequest": {"number": 1019}},
        {"position": 2, "enqueuedAt": "2026-07-28T06:30:00Z",
         "pullRequest": {"number": 1026}},
    ]}}}}}


entries = open_prs.fetch_merge_queue_entries(REPO, "tok", graphql_fn=_graphql)
ok(set(entries) == {1019, 1026}, f"both queued PRs returned: {entries}")
ok(entries[1019]["position"] == 1, "position preserved")
ok(entries[1019]["enqueued_at"] > 0, "enqueuedAt parsed to epoch seconds")
ok(entries[1026]["enqueued_at"] > entries[1019]["enqueued_at"],
   "later enqueue sorts later in time")

# The legacy positions helper keeps its exact old shape: int -> int.
positions = open_prs.fetch_merge_queue_positions(REPO, "tok", graphql_fn=_graphql)
ok(positions == {1019: 1, 1026: 2}, f"legacy position map unchanged: {positions}")

# A failing GraphQL call degrades to empty, never raises.
ok(open_prs.fetch_merge_queue_entries(
    REPO, "tok", graphql_fn=lambda q, t: (_ for _ in ()).throw(RuntimeError("x"))) == {},
   "a GitHub failure yields no queue data instead of an exception")

# queue_fn may still hand back bare ints (legacy callers and tests).
ok(open_prs._queue_entry(3) == (3, 0), "a bare position still works")
ok(open_prs._queue_entry({"position": 2, "enqueued_at": 99}) == (2, 99),
   "the rich entry shape is unpacked")
ok(open_prs._queue_entry(None) == (0, 0), "a missing entry is not queued")

# The dock renders dwell, and refuses to invent a number when it has none.
app = (ROOT / "static/app.js").read_text()
ok("queue_enqueued_at" in app, "the dock must read queue_enqueued_at")
ok("_dockQueueDwell" in app, "the dock must render queue dwell")
ok("time unknown" in app, "a missing enqueuedAt must read as unknown, not 0s")
ok("in queue" in app, "dwell must be labelled as queue time, not edit age")

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_queue_dwell")
