#!/usr/bin/env python3
"""Self-contained test for the dependency-edge MCP tools (add/remove_dependency,
depends_on on update_task/create_task). Builds its own throwaway 'helm' DB in a temp
dir — never touches the real board files. Run:  python3 test_mcp_dependencies.py

`mcp` is only needed at runtime on the server; here we stub the heavy imports so the
REAL tool functions in mcp_server.py are exercised against the temp store."""
import os, sys, json, types, tempfile

_TMP = tempfile.mkdtemp(prefix="dep-test-")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")   # isolated throwaway board
os.environ.pop("PM_MCP_TOKEN", None)                            # writes open -> ctx may be None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_heavy_imports():
    class _FastMCP:
        def __init__(self, *a, **k): pass
        def tool(self, *a, **k):
            return lambda f: f                  # identity: keep tool functions callable
        def __getattr__(self, n): return lambda *a, **k: None

    def _mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

    _mk("mcp"); _mk("mcp.server")
    _mk("mcp.server.fastmcp", Context=object, FastMCP=_FastMCP)
    _mk("mcp.server.transport_security",
        TransportSecuritySettings=type("TSS", (), {"__init__": lambda self, *a, **k: None}))
    _mk("agent", _task_brief=lambda t: t, run=lambda *a, **k: {})
    for n in ("digest", "intake", "notify", "rag", "signals"):
        _mk(n)


_stub_heavy_imports()
import store                       # real (stdlib-only deps)
import mcp_server as M             # real tool functions, now importable

P = "helm"
store.init_db(P)
# seed three real tasks to wire edges among
store.create_task({"workstream_id": "SUBJ", "title": "subject"}, project=P)        # SUBJ-1
store.create_task({"workstream_id": "DEP", "title": "dep one"}, project=P)         # DEP-1
store.create_task({"workstream_id": "DEP", "title": "dep two"}, project=P)         # DEP-2

passed = failed = 0
def ok(c, m):
    global passed, failed
    print(("  PASS  " if c else "  FAIL  ") + m)
    passed += 1 if c else 0; failed += 0 if c else 1
def deps(t): return store.get_task(t, project=P).get("depends_on")

ok(M._dep_ids("DEP-1, dep-2  GHOST-9\nDEP-1") == ["DEP-1", "DEP-2", "GHOST-9"],
   "_dep_ids: split on comma/space/newline, upper-case, dedupe, order-preserving")

M.add_dependency("SUBJ-1", "DEP-1", None, project=P)
ok(deps("SUBJ-1") == ["DEP-1"], "add_dependency adds one edge")
M.add_dependency("SUBJ-1", "DEP-1", None, project=P)
ok(deps("SUBJ-1") == ["DEP-1"], "add_dependency is idempotent")
M.add_dependency("SUBJ-1", "DEP-2", None, project=P)
ok(deps("SUBJ-1") == ["DEP-1", "DEP-2"], "add_dependency appends without clobber")

# FAIL-FAST: a dependency to a non-existent task is a broken edge -> REJECTED, nothing written.
before = list(deps("SUBJ-1"))
r = json.loads(M.add_dependency("SUBJ-1", "GHOST-9", None, project=P))
ok("error" in r and "GHOST-9" in r["error"], "unknown dep id is REJECTED with an error (not warned-and-written)")
ok(deps("SUBJ-1") == before, "rejected unknown id leaves NO dangling edge in the graph")
r = json.loads(M.add_dependency("SUBJ-1", "DEP-1, GHOST-9", None, project=P))   # mixed valid+unknown
ok("error" in r and deps("SUBJ-1") == before, "mixed valid+unknown is ATOMICALLY rejected (no partial write)")

# remove_dependency surfaces a no-op instead of silently swallowing it
r = json.loads(M.remove_dependency("SUBJ-1", "GHOST-9", None, project=P))
ok("note" in r, "remove of a not-present id is SURFACED (no-op note), not silently swallowed")

M.update_task("SUBJ-1", None, depends_on="DEP-2", project=P)
ok(deps("SUBJ-1") == ["DEP-2"], "update_task depends_on REPLACES the list")
r = json.loads(M.update_task("SUBJ-1", None, depends_on="GHOST-9", project=P))
ok("error" in r and deps("SUBJ-1") == ["DEP-2"], "update_task REJECTS an unknown dep (task left untouched)")
M.update_task("SUBJ-1", None, depends_on="none", project=P)
ok(deps("SUBJ-1") == [], "update_task depends_on='none' clears")
M.update_task("SUBJ-1", None, status="In Progress", project=P)
ok(deps("SUBJ-1") == [], "update_task without depends_on leaves deps untouched")

nt = json.loads(M.create_task("SUBJ", "with deps", None, depends_on="DEP-1, DEP-2", project=P))["task_id"]
ok(set(deps(nt)) == {"DEP-1", "DEP-2"}, "create_task persists depends_on")
r = json.loads(M.create_task("SUBJ", "bad deps", None, depends_on="GHOST-9", project=P))
ok("error" in r, "create_task REJECTS a task carrying an unknown dep (not created)")

ed = [a for a in store.get_task("SUBJ-1", project=P)["activity"] if a["kind"] == "edit"]
ok(any("depends_on" in (a.get("payload") or {}) for a in ed), "edge edits recorded in the activity log")

print("\n%d passed, %d failed" % (passed, failed))
import shutil; shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
