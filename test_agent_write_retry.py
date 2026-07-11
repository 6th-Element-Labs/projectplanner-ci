"""BUG-32: high-frequency agent writes retry transient SQLite lock failures."""
import sqlite3

import store


CASES = [
    ("create_deliverable", {"data": {"title": "Outcome"}}),
    ("link_task_to_deliverable", {
        "deliverable_id": "outcome", "task_project": "switchboard", "task_id": "BUG-32"}),
    ("create_task", {"data": {"workstream_id": "BUG", "title": "Retry writes"}}),
    ("update_task", {"task_id": "BUG-32", "fields": {"status": "In Progress"}}),
    ("register_agent", {"agent_id": "codex/BUG-32", "runtime": "codex"}),
    ("claim_task", {"task_id": "BUG-32", "agent_id": "codex/BUG-32"}),
    ("complete_claim", {"claim_id": "claim-BUG-32"}),
]

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


real_sleep = store.time.sleep
real_get_task = store.get_task
real_get_deliverable = store.get_deliverable
real_enqueue_narration = store.enqueue_narration
real_finalize_complete_claim = store._finalize_complete_claim_response
store.time.sleep = lambda _seconds: None
store.get_task = lambda task_id, project=None: {"task_id": task_id}
store.get_deliverable = lambda deliverable_id, project=None: {"id": deliverable_id}
store.enqueue_narration = lambda *_args, **_kwargs: None
store._finalize_complete_claim_response = lambda response, *_args: response
try:
    for public_name, kwargs in CASES:
        impl_name = f"_{public_name}_impl"
        real_impl = getattr(store, impl_name)
        calls = []

        def flaky(*_args, **_kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            if public_name == "create_task":
                return "BUG-32"
            if public_name == "update_task":
                return {"task_id": "BUG-32", "changed": {}}
            if public_name == "complete_claim":
                return {"completed": True, "claim_id": "claim-BUG-32",
                        "task_id": "BUG-32", "status": "In Review"}
            return {"ok": True}

        setattr(store, impl_name, flaky)
        try:
            result = getattr(store, public_name)(**kwargs)
            check(f"{public_name} retries locks and succeeds", result is not None)
            check(f"{public_name} opens a fresh attempt", len(calls) == 3)
        finally:
            setattr(store, impl_name, real_impl)
finally:
    store.time.sleep = real_sleep
    store.get_task = real_get_task
    store.get_deliverable = real_get_deliverable
    store.enqueue_narration = real_enqueue_narration
    store._finalize_complete_claim_response = real_finalize_complete_claim


real_create_task_impl = store._create_task_impl
calls = []


def invalid(*_args, **_kwargs):
    calls.append(1)
    raise sqlite3.IntegrityError("constraint failed")


store._create_task_impl = invalid
try:
    store.create_task({"workstream_id": "BUG", "title": "No retry"})
    check("non-lock database errors propagate", False)
except sqlite3.IntegrityError as exc:
    check("non-lock database errors propagate", str(exc) == "constraint failed")
finally:
    store._create_task_impl = real_create_task_impl
check("non-lock database errors are not retried", len(calls) == 1)


# A lock after the atomic write returned must not replay that write.
real_create_task_impl = store._create_task_impl
real_get_task = store.get_task
real_enqueue_narration = store.enqueue_narration
calls = []
store._create_task_impl = lambda *_args, **_kwargs: calls.append(1) or "BUG-32"
store.enqueue_narration = lambda *_args, **_kwargs: None
store.get_task = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    sqlite3.OperationalError("database is locked"))
try:
    store.create_task({"workstream_id": "BUG", "title": "Committed once"})
    check("post-commit task read lock propagates", False)
except sqlite3.OperationalError:
    check("post-commit task read lock propagates", True)
finally:
    store._create_task_impl = real_create_task_impl
    store.get_task = real_get_task
    store.enqueue_narration = real_enqueue_narration
check("post-commit task read lock does not replay creation", len(calls) == 1)


real_complete_claim_impl = store._complete_claim_impl
real_finalize_complete_claim = store._finalize_complete_claim_response
calls = []
store._complete_claim_impl = lambda *_args, **_kwargs: calls.append(1) or {
    "completed": True, "claim_id": "claim-BUG-32", "task_id": "BUG-32",
    "status": "In Review"}
store._finalize_complete_claim_response = lambda *_args, **_kwargs: (
    (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
try:
    store.complete_claim("claim-BUG-32")
    check("post-commit mission lock propagates", False)
except sqlite3.OperationalError:
    check("post-commit mission lock propagates", True)
finally:
    store._complete_claim_impl = real_complete_claim_impl
    store._finalize_complete_claim_response = real_finalize_complete_claim
check("post-commit mission lock does not replay completion", len(calls) == 1)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
