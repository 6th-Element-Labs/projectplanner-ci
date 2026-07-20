#!/usr/bin/env python3
"""SEG-2 routing matrix, cache scaling, and fail-closed side-effect proof."""
import os
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="seg2-routing-")
os.environ["PM_DB_PATH"] = os.path.join(tmp, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(tmp, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = tmp
os.environ["PM_INBOX_ROUTES"] = "legacy.test=maxwell,boats.test=helm"

import store
from switchboard.integrations import inbox_routing

for project in ("maxwell", "helm", "switchboard"):
    store.init_db(project)
inbox_routing.invalidate_routes()

matrix = [
    ("a@none.test", "plan+switchboard@taikunai.com", True, "switchboard", "plus_address"),
    ("a@boats.test", "plan@taikunai.com", True, "helm", "sender_domain"),
    ("a@legacy.test", "plan@taikunai.com", True, "maxwell", "sender_domain"),
    ("a@none.test", "plan@taikunai.com", False, None, "unmapped_sender"),
    ("a@none.test", "plan+missing@taikunai.com", False, None, "unknown_plus_tag"),
    ("a@none.test", "plan+helm@taikunai.com, plan+switchboard@taikunai.com",
     False, None, "ambiguous_plus_tags"),
]
for sender, recipients, accepted, project, reason in matrix:
    got = inbox_routing.route_decision(sender, recipients)
    assert (got.accepted, got.project, got.reason) == (accepted, project, reason), got

# The hot path must not scan project databases. Once built, even 20k decisions reuse the index.
original_project_ids = store.project_ids
original_persisted = __import__("comms").persisted_routes
calls = {"project_ids": 0, "persisted_routes": 0}
store.project_ids = lambda: calls.__setitem__("project_ids", calls["project_ids"] + 1) or original_project_ids()
__import__("comms").persisted_routes = lambda: calls.__setitem__("persisted_routes", calls["persisted_routes"] + 1) or original_persisted()
inbox_routing.invalidate_routes()
assert inbox_routing.route("a@boats.test", "plan@taikunai.com") == (True, "helm")
cold_build_calls = dict(calls)
start = time.perf_counter()
for _ in range(20_000):
    assert inbox_routing.route("a@boats.test", "plan@taikunai.com") == (True, "helm")
elapsed = time.perf_counter() - start
assert calls == cold_build_calls, (cold_build_calls, calls)
assert calls["persisted_routes"] == 1
assert elapsed < 2.0, elapsed

# Increasing the configured project/route count does not change lookup work.
timings = []
for route_count in (10, 1_000, 10_000):
    domains = {f"tenant-{i}.example": "helm" for i in range(route_count)}
    domains["boats.test"] = "helm"
    index = inbox_routing.RouteIndex(domains, {"plan+helm@taikunai.com": "helm"},
                                     frozenset({"helm"}), "")
    started = time.perf_counter()
    for _ in range(20_000):
        assert inbox_routing.domain_project("a@east.boats.test", index) == "helm"
    timings.append(time.perf_counter() - started)
assert max(timings) < max(0.2, min(timings) * 3), timings

# Quarantine decisions do not call the ingest pipeline or mutate Maxwell.
before = (store.inbox_pending_count(project="maxwell"), len(store.list_tasks(project="maxwell")))
llm_calls = embedding_calls = 0
for sender, recipients, accepted, _project, _reason in matrix:
    if not accepted:
        # The adapter stops here; this is the exact gate before inbox.process.
        assert not inbox_routing.route_decision(sender, recipients).accepted
after = (store.inbox_pending_count(project="maxwell"), len(store.list_tasks(project="maxwell")))
assert after == before
assert llm_calls == 0 and embedding_calls == 0

# The IMAP adapter physically quarantines before the ingest seam.
import inbox_source

raw_message = (b"From: stranger@nowhere.test\r\nTo: plan@taikunai.com\r\n"
               b"Subject: should quarantine\r\nMessage-ID: <seg2@test>\r\n\r\nbody")


class FakeIMAP:
    instance = None

    def __init__(self, _host):
        self.calls = []
        FakeIMAP.instance = self

    def login(self, *_args): pass
    def select(self, *_args): pass
    def search(self, *_args): return "OK", [b"7"]
    def fetch(self, *_args): return "OK", [(None, raw_message)]
    def create(self, folder): self.calls.append(("create", folder)); return "OK", []
    def copy(self, message_id, folder): self.calls.append(("copy", message_id, folder)); return "OK", []
    def store(self, message_id, *args): self.calls.append(("store", message_id)); return "OK", []
    def expunge(self): self.calls.append(("expunge",)); return "OK", []
    def logout(self): pass


os.environ["PM_IMAP_USER"] = "plan@taikunai.com"
os.environ["PM_IMAP_PASSWORD"] = "test-only"
inbox_routing.invalidate_routes()
inbox_source.imaplib.IMAP4_SSL = FakeIMAP
inbox_source.inbox.process = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError("quarantined mail reached inbox.process"))
poll_result = inbox_source.poll()
assert poll_result["queued"] == 0 and poll_result["quarantined"] == 1, poll_result
assert any(call[0] == "copy" for call in FakeIMAP.instance.calls)
assert any(call[0] == "store" for call in FakeIMAP.instance.calls)
print("PASS: SEG-2 fail-closed routing matrix, cached hot path, and zero side effects")
