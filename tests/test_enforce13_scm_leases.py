"""ENFORCE-13 — short-lived SCM lease broker (adversarial, script-style).

Run:  PYTHONPATH="<wt>/src:<wt>" python tests/test_enforce13_scm_leases.py

The broker issues exact-binding leases for repository operations only after an
exact host/wake claim, materializes a GitHub-App token ONLY through a trusted
runtime bridge, separates clone/fetch from push/PR/merge phases, and fences on
replay, expiry, host loss, cancellation, revocation, and context drift. Raw
tokens must never reach a lease row, an event row, a receipt, or a log.
"""
from __future__ import annotations

import os
import tempfile

from path_setup import ROOT  # noqa: F401  (sets sys.path to repo root + src)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def _isolate_registry():
    """Point every registry/DB path at a throwaway dir so tests never touch dev data."""
    tmp = tempfile.mkdtemp(prefix="enforce13-")
    for var in (
        "PM_DB_PATH", "PM_SWITCHBOARD_DB_PATH", "PM_HELM_DB_PATH",
        "PM_PROJECT_REGISTRY_DB_PATH",
    ):
        os.environ[var] = os.path.join(tmp, f"{var.lower()}.db")
    os.environ.setdefault("PM_VAULT_KEY", "enforce13-test-vault-key-0123456789abcdef")
    return tmp


CANONICAL = "6th-element-labs/projectplanner"
ORG = "6th-element-labs"
ALL_OPS = ["clone", "fetch", "read", "push", "create_pr", "merge"]


def _scm_repo(canonical=CANONICAL):
    """A real ACCESS-28 SCM connection repository with an injected fake topology."""
    from switchboard.storage.repositories.scm_connections import SCMConnectionRepository

    def topology(project):
        return {"valid": True, "roles": {"canonical": {"repo": canonical}}}

    return SCMConnectionRepository(topology_provider=topology)


def _make_connection(scm, *, project="switchboard", repos=(CANONICAL,), orgs=(ORG,),
                     scopes=tuple(ALL_OPS), ref="github-app-installation:opaque-1"):
    return scm.create({
        "provider": "github_app",
        "installation_ref": ref,
        "org_allowlist": list(orgs),
        "project_allowlist": [project],
        "repository_allowlist": list(repos),
        "operation_scopes": list(scopes),
        "project": project,
    }, actor="operator")


def _principal(pid="agent/codex/enforce-13", kind="agent", scopes=("use:credentials",),
               admin=False):
    from switchboard.domain.scm_leases import SCMLeasePrincipal
    return SCMLeasePrincipal.from_mapping(
        {"principal_id": pid, "principal_kind": kind, "scopes": list(scopes), "admin": admin})


def _broker(scm):
    from switchboard.storage.repositories.scm_leases import SCMLeaseRepository
    return SCMLeaseRepository(scm_authorizer=scm.preflight)


def _binding(**overrides):
    binding = dict(
        project="switchboard", repository=CANONICAL, org_id=ORG,
        task_id="ENFORCE-13", generation="gen-1", context_digest="ctx-abc",
        host_id="host/steve", runner_session_id="run_1", work_session_id="ws_1",
        claim_id="claim_1", wake_id="wake_1", ttl_seconds=900, actor="agent/codex",
    )
    binding.update(overrides)
    return binding


def _acquire(broker, connection_id, *, operations=("clone", "fetch"), principal=None,
             **overrides):
    return broker.acquire_lease(
        connection_id=connection_id, operations=list(operations),
        principal=principal or _principal(), **_binding(**overrides))


# ---------------------------------------------------------------------------
# 1. Domain: operation → phase mapping and principal shape
# ---------------------------------------------------------------------------

def test_operation_phase_mapping():
    from switchboard.domain.scm_leases import (
        READ_OPERATIONS, WRITE_OPERATIONS, phase_for_operation, SCMLeaseError,
    )
    ok(phase_for_operation("clone") == "read", "clone is a read-phase operation")
    ok(phase_for_operation("fetch") == "read", "fetch is a read-phase operation")
    ok(phase_for_operation("read") == "read", "read is a read-phase operation")
    ok(phase_for_operation("push") == "write", "push is a write-phase operation")
    ok(phase_for_operation("create_pr") == "write", "create_pr is a write-phase operation")
    ok(phase_for_operation("merge") == "write", "merge is a write-phase operation")
    ok(READ_OPERATIONS.isdisjoint(WRITE_OPERATIONS), "read and write phases are disjoint")
    try:
        phase_for_operation("rm-rf")
        ok(False, "unknown operation raises SCMLeaseError")
    except SCMLeaseError as exc:
        ok(exc.code == "invalid_scm_operation", "unknown operation -> invalid_scm_operation")


def test_principal_from_mapping():
    from switchboard.domain.scm_leases import SCMLeasePrincipal
    principal = SCMLeasePrincipal.from_mapping({
        "principal_id": "agent/codex/enforce-13", "principal_kind": "Agent",
        "scopes": ["use:credentials"], "admin": False,
    })
    ok(principal.principal_id == "agent/codex/enforce-13", "principal id preserved")
    ok(principal.principal_kind == "agent", "principal kind normalized to lowercase")
    ok(principal.can_use_credentials(), "agent with use:credentials can use credentials")
    anon = SCMLeasePrincipal.from_mapping({"principal_id": "", "principal_kind": "agent"})
    ok(not anon.can_use_credentials(), "principal without scope cannot use credentials")


# ---------------------------------------------------------------------------
# 2. acquire_lease: exact binding, ACCESS-28 authorization, concurrency, replay
# ---------------------------------------------------------------------------

def test_acquire_read_and_write_leases():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)

    read_lease = _acquire(broker, conn["connection_id"], operations=["clone", "fetch"])
    ok(read_lease["state"] == "issued", "read lease is issued")
    ok(read_lease["phase"] == "read", "read lease carries the read phase")
    ok(read_lease["operations"] == ["clone", "fetch"], "read lease records its operations")
    ok(read_lease["repository"] == CANONICAL, "lease binds the exact repository")
    ok(read_lease["generation"] == "gen-1", "lease binds the execution generation")
    ok(read_lease["org_id"] == ORG, "lease binds the org")
    ok(read_lease["installation_version"] == 1, "lease pins the installation version")
    ok(float(read_lease["expires_at"]) > 0, "lease has an expiry")
    ok(bool(read_lease.get("lease_id")), "lease has an id")

    write_lease = _acquire(broker, conn["connection_id"], operations=["push", "merge"],
                           claim_id="claim_2", wake_id="wake_2", runner_session_id="run_2")
    ok(write_lease["phase"] == "write", "push/merge produce a write-phase lease")


def test_acquire_requires_exact_host_wake_claim():
    """A lease can only be issued after an exact host/wake/runner/claim bind exists."""
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    for field in ("host_id", "runner_session_id", "claim_id", "wake_id",
                  "work_session_id", "generation", "context_digest", "task_id"):
        try:
            _acquire(broker, conn["connection_id"], **{field: ""})
            ok(False, f"missing {field} denies lease issuance")
        except SCMLeaseError as exc:
            ok(exc.code == "scm_lease_binding_incomplete",
               f"missing {field} -> scm_lease_binding_incomplete")


def test_acquire_denies_unauthorized_scope():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)  # authorizes only project=switchboard, repo=CANONICAL, org=ORG
    broker = _broker(scm)

    checks = [
        ("cross-project", dict(project="maxwell")),
        ("cross-repository", dict(repository="evil-org/rogue")),
        ("cross-org", dict(org_id="evil-org")),
    ]
    for label, overrides in checks:
        try:
            _acquire(broker, conn["connection_id"], **overrides)
            ok(False, f"{label} acquire is denied")
        except SCMLeaseError as exc:
            ok(exc.status_code in (403, 404),
               f"{label} acquire -> repository_not_authorized ({exc.code})")


def test_acquire_denies_operation_outside_connection_scope():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    # Connection is authorized for reads only; a push lease must be refused.
    conn = _make_connection(scm, scopes=("clone", "fetch", "read"))
    broker = _broker(scm)
    try:
        _acquire(broker, conn["connection_id"], operations=["push"],
                 claim_id="claim_9", wake_id="wake_9", runner_session_id="run_9")
        ok(False, "push denied when connection scopes are read-only")
    except SCMLeaseError as exc:
        ok(exc.status_code in (403, 404),
           f"unauthorized operation -> denied ({exc.code})")


def test_acquire_rejects_mixed_phase_operations():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    try:
        _acquire(broker, conn["connection_id"], operations=["clone", "push"])
        ok(False, "a lease may not mix read and write operations")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_operation_phase_conflict",
           "mixed-phase lease -> scm_operation_phase_conflict")


def test_acquire_is_idempotent_for_same_principal():
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    first = _acquire(broker, conn["connection_id"])
    again = _acquire(broker, conn["connection_id"])
    ok(first["lease_id"] == again["lease_id"],
       "re-acquiring the exact binding returns the same issued lease")


def test_acquire_conflicts_for_different_principal():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    _acquire(broker, conn["connection_id"], principal=_principal(pid="agent/one"))
    try:
        _acquire(broker, conn["connection_id"], principal=_principal(pid="agent/two"))
        ok(False, "a different principal cannot steal an issued lease")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_lease_already_consumed",
           "different principal on same binding -> scm_lease_already_consumed")


def test_lease_projection_carries_no_secret():
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    lease = _acquire(broker, conn["connection_id"])
    blob = repr(lease).lower()
    ok("installation_ref" not in lease, "lease projection omits the installation ref")
    ok("token" not in blob, "lease projection contains no token field")
    ok("opaque-1" not in blob, "lease projection does not leak the opaque installation ref")
    ok("connection_id" in lease and lease["connection_id"] == conn["connection_id"],
       "lease references the SCM connection by id only")


# ---------------------------------------------------------------------------
# 3. materialize_for_runtime: trusted bridge, phase separation, fencing, redaction
# ---------------------------------------------------------------------------

SENTINEL_TOKEN = "ghs_ENFORCE13_SECRET_do_not_leak_0123456789"


class _FakeMinter:
    """Stand-in trusted-runtime minter: records its calls, returns a sentinel token."""

    def __init__(self):
        self.calls = []

    def mint(self, *, installation_ref, repository, phase, operations, ttl_seconds):
        import time as _t
        from switchboard.domain.scm_leases import MintedSCMToken
        self.calls.append({
            "installation_ref": installation_ref, "repository": repository,
            "phase": phase, "operations": tuple(operations), "ttl_seconds": ttl_seconds,
        })
        return MintedSCMToken(token=SENTINEL_TOKEN, expires_at=_t.time() + ttl_seconds)


def _broker_with_minter(scm, minter):
    from switchboard.storage.repositories.scm_leases import SCMLeaseRepository
    return SCMLeaseRepository(scm_authorizer=scm.preflight, minter=minter)


def _materialize(broker, lease, *, operation="clone", principal=None, **overrides):
    b = _binding(**overrides)
    b.pop("ttl_seconds", None)
    b.pop("org_id", None)  # materialize re-derives org from the repository
    return broker.materialize_for_runtime(
        lease["lease_id"], operation=operation,
        principal=principal or _principal(), **b)


def _registry_text():
    """Every stored byte of the SCM lease tables, for leak scanning."""
    import sqlite3
    conn = sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"])
    try:
        chunks = []
        for table in ("scm_leases", "scm_lease_events"):
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                chunks.append("|".join("" if v is None else str(v) for v in row))
        return "\n".join(chunks)
    finally:
        conn.close()


def test_materialize_mints_once_and_hides_the_token():
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone", "fetch"])

    minted = _materialize(broker, lease, operation="clone")
    ok(minted.token == SENTINEL_TOKEN, "trusted bridge returns the minted token")
    ok(len(minter.calls) == 1, "minter is invoked exactly once")
    ok(minter.calls[0]["installation_ref"], "minter receives the opaque installation ref")
    ok(SENTINEL_TOKEN not in _registry_text(),
       "the minted token is never persisted to any lease or event row")


def test_materialize_is_single_consumer_replay_fenced():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    _materialize(broker, lease, operation="clone")
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "a lease cannot be materialized twice")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_lease_already_consumed",
           "second materialize -> scm_lease_already_consumed (replay fenced)")
    ok(len(minter.calls) == 1, "the token is minted only once across a replay")


def test_materialize_enforces_phase_separation():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    read_lease = _acquire(broker, conn["connection_id"], operations=["clone", "fetch"])
    try:
        _materialize(broker, read_lease, operation="push")
        ok(False, "a read lease cannot perform a write operation")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_operation_not_leased",
           "push on a read lease -> scm_operation_not_leased (phase separated)")
    ok(len(minter.calls) == 0, "no token is minted for an out-of-phase operation")

    # An operation inside the connection scope but not this lease's grant is refused too.
    read_lease2 = _acquire(broker, conn["connection_id"], operations=["clone"],
                           claim_id="c2", wake_id="w2", runner_session_id="r2")
    try:
        _materialize(broker, read_lease2, operation="fetch",
                     claim_id="c2", wake_id="w2", runner_session_id="r2")
        ok(False, "an operation outside the lease grant is refused")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_operation_not_leased",
           "fetch on a clone-only lease -> scm_operation_not_leased")


def test_materialize_requires_exact_binding():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    for field, value in (("generation", "gen-forged"), ("context_digest", "ctx-forged"),
                         ("host_id", "host/evil"), ("wake_id", "wake-forged")):
        try:
            _materialize(broker, lease, operation="clone", **{field: value})
            ok(False, f"materialize with forged {field} is denied")
        except SCMLeaseError as exc:
            ok(exc.code == "scm_lease_binding_mismatch",
               f"forged {field} at materialize -> scm_lease_binding_mismatch")
    ok(len(minter.calls) == 0, "no token minted for any mismatched binding")


def test_materialize_fences_on_installation_drift():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    # Rotate the connection: installation_version moves 1 -> 2, the lease is pinned at 1.
    scm.rotate(conn["connection_id"], "github-app-installation:opaque-rotated",
               actor="operator", project="switchboard")
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "a lease pinned to a stale installation version cannot materialize")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_installation_drift",
           "rotated connection -> scm_installation_drift")
    ok(len(minter.calls) == 0, "no token minted after installation drift")
    reloaded = broker.get_lease(lease["lease_id"], project="switchboard")
    ok(reloaded["state"] == "fenced", "the drifted lease is fenced, not left issued")


def test_materialize_denied_after_connection_revoked():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["push"],
                     claim_id="cw", wake_id="ww", runner_session_id="rw")
    scm.revoke(conn["connection_id"], "compromised", actor="operator", project="switchboard")
    try:
        _materialize(broker, lease, operation="push",
                     claim_id="cw", wake_id="ww", runner_session_id="rw")
        ok(False, "a revoked connection cannot materialize an outstanding lease")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_connection_not_authorized",
           "revoked connection -> scm_connection_not_authorized (immediate revocation)")
    ok(len(minter.calls) == 0, "no token minted after connection revocation")


def test_materialize_denied_after_expiry():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"], ttl_seconds=0)
    import time as _t
    _t.sleep(0.01)
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "an expired lease cannot materialize")
    except SCMLeaseError as exc:
        ok(exc.code in ("scm_lease_expired", "scm_lease_not_usable"),
           f"expired lease -> not usable ({exc.code})")
    ok(len(minter.calls) == 0, "no token minted for an expired lease")


def test_default_minter_is_unavailable_and_fences():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)  # no minter injected -> UnconfiguredSCMTokenMinter
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "the default minter refuses to mint")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_minter_unavailable",
           "default minter -> scm_minter_unavailable")
    reloaded = broker.get_lease(lease["lease_id"], project="switchboard")
    ok(reloaded["state"] == "fenced",
       "a lease whose materialization failed is fenced, not reusable")


# ---------------------------------------------------------------------------
# 4. lifecycle: concurrency across executions, release, host-loss/cancel/connection fencing
# ---------------------------------------------------------------------------

def test_distinct_executions_each_get_a_lease():
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    one = _acquire(broker, conn["connection_id"], generation="gen-1",
                   claim_id="c1", wake_id="w1", runner_session_id="r1")
    two = _acquire(broker, conn["connection_id"], generation="gen-2",
                   claim_id="c2", wake_id="w2", runner_session_id="r2")
    ok(one["lease_id"] != two["lease_id"],
       "two distinct executions each receive their own live lease (concurrency allowed)")


def test_release_lease_is_terminal():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    released = broker.release_lease(lease["lease_id"], project="switchboard",
                                    actor="agent/codex", reason="drained",
                                    principal=_principal())
    ok(released["state"] == "released", "release moves a live lease to released")
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "a released lease cannot materialize")
    except SCMLeaseError as exc:
        ok(exc.code in ("scm_lease_already_consumed", "scm_lease_not_usable"),
           f"released lease cannot materialize ({exc.code})")
    ok(len(minter.calls) == 0, "no token minted from a released lease")


def test_fence_leases_for_execution_on_host_loss():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["push"],
                     claim_id="ch", wake_id="wh", runner_session_id="rh")
    # A sibling execution on the same connection must NOT be fenced by this call.
    other = _acquire(broker, conn["connection_id"], operations=["push"],
                     generation="gen-2", claim_id="c2", wake_id="w2",
                     runner_session_id="r2")
    fenced = broker.fence_leases_for_execution(
        project="switchboard", task_id="ENFORCE-13", generation="gen-1",
        host_id="host/steve", runner_session_id="rh", claim_id="ch", wake_id="wh",
        actor="switchboard/host-loss", reason="host_loss")
    ok(fenced == 1, "host-loss fences exactly the lost execution's lease")
    ok(broker.get_lease(lease["lease_id"], project="switchboard")["state"] == "fenced",
       "the lost execution's lease is fenced")
    ok(broker.get_lease(other["lease_id"], project="switchboard")["state"] == "issued",
       "a sibling execution's lease survives the host-loss fence")
    try:
        _materialize(broker, lease, operation="push",
                     claim_id="ch", wake_id="wh", runner_session_id="rh")
        ok(False, "a fenced lease cannot materialize")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_lease_already_consumed",
           "materializing a host-loss-fenced lease -> already_consumed")


def test_fence_leases_for_connection_on_revocation():
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    a = _acquire(broker, conn["connection_id"], generation="gen-1",
                 claim_id="c1", wake_id="w1", runner_session_id="r1")
    b = _acquire(broker, conn["connection_id"], generation="gen-2",
                 claim_id="c2", wake_id="w2", runner_session_id="r2")
    fenced = broker.fence_leases_for_connection(
        conn["connection_id"], actor="switchboard/scm-revoke",
        reason="scm_connection_revoked")
    ok(fenced == 2, "revoking a connection fences every live lease bound to it")
    ok(broker.get_lease(a["lease_id"], project="switchboard")["state"] == "fenced",
       "first lease fenced on connection revocation")
    ok(broker.get_lease(b["lease_id"], project="switchboard")["state"] == "fenced",
       "second lease fenced on connection revocation")


def test_cleanup_expired_leases_sweeps():
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker(scm)
    _acquire(broker, conn["connection_id"], operations=["clone"], ttl_seconds=0)
    import time as _t
    _t.sleep(0.01)
    swept = broker.cleanup_expired_leases()
    ok(swept >= 1, "cleanup sweeps at least the elapsed lease")


# ---------------------------------------------------------------------------
# 5. MCP surface: auth census declaration + registration wiring
# ---------------------------------------------------------------------------

def test_mcp_tools_declared_in_auth_census():
    from switchboard.mcp import authorization
    ok("acquire_scm_lease" in authorization.WRITE_TOOLS,
       "acquire_scm_lease is declared a write tool")
    ok("release_scm_lease" in authorization.WRITE_TOOLS,
       "release_scm_lease is declared a write tool")
    ok("get_scm_lease" in authorization.READ_TOOLS,
       "get_scm_lease is declared a read tool")
    for name in ("acquire_scm_lease", "release_scm_lease", "get_scm_lease"):
        decl = authorization.declaration_for(name)  # raises if unclassified
        ok(decl.project_argument == "project", f"{name} authorizes on the project argument")


def test_mcp_registration_exposes_lease_tools():
    from switchboard.mcp.tools import scm_leases as tools
    import inspect
    registered = {}

    class _MCP:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    services = tools.SCMLeaseToolServices(
        dumps=lambda v: v, require_read=lambda *a, **k: {},
        require_write=lambda *a, **k: {}, principal_actor=lambda p: "actor")
    result = tools.register_scm_lease_tools(_MCP(), services)
    ok(set(result) == {"acquire_scm_lease", "release_scm_lease", "get_scm_lease"},
       "register_scm_lease_tools registers exactly the three lease tools")
    for name in ("acquire_scm_lease", "release_scm_lease", "get_scm_lease"):
        params = inspect.signature(result[name]).parameters
        ok("project" in params, f"{name} declares a project argument (census guard)")
        ok("materialize_scm_lease" not in result,
           "materialization is never exposed as an MCP tool")


# ---------------------------------------------------------------------------
# 6. fail-closed against an unknown/deleted connection (authorizer raises)
# ---------------------------------------------------------------------------

def test_acquire_unknown_connection_is_denied_not_crashed():
    from switchboard.domain.scm_leases import SCMLeaseError
    scm = _scm_repo()
    broker = _broker(scm)  # no connection created
    try:
        _acquire(broker, "scm-does-not-exist")
        ok(False, "acquiring against an unknown connection is denied")
    except SCMLeaseError as exc:
        ok(exc.status_code in (403, 404),
           f"unknown connection acquire -> fail-closed deny ({exc.code})")


def test_materialize_denied_after_connection_deleted():
    from switchboard.domain.scm_leases import SCMLeaseError
    minter = _FakeMinter()
    scm = _scm_repo()
    conn = _make_connection(scm)
    broker = _broker_with_minter(scm, minter)
    lease = _acquire(broker, conn["connection_id"], operations=["clone"])
    scm.delete(conn["connection_id"], "decommissioned", actor="operator", project="switchboard")
    try:
        _materialize(broker, lease, operation="clone")
        ok(False, "a deleted connection cannot materialize an outstanding lease")
    except SCMLeaseError as exc:
        ok(exc.code == "scm_connection_not_authorized",
           "deleted connection -> scm_connection_not_authorized")
    ok(len(minter.calls) == 0, "no token minted against a deleted connection")
    ok(broker.get_lease(lease["lease_id"], project="switchboard")["state"] == "fenced",
       "the lease is fenced after its connection is deleted")


# ---------------------------------------------------------------------------
# 7. REST surface: acquire/release/get parity (no materialization endpoint)
# ---------------------------------------------------------------------------

def _rest_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from switchboard.api.routers.scm_leases import create_router

    def resolve_project(project):
        return str(project or "").strip().lower()

    def resolve_principal(request, project, scopes=(), dev_actor="", **kw):
        return {"id": "agent/host-rest", "kind": "host",
                "scopes": ["use:credentials", "read:credentials"],
                "effective_scopes": ["use:credentials", "read:credentials"]}

    app = FastAPI()
    app.include_router(create_router(
        resolve_project=resolve_project, resolve_principal=resolve_principal))
    return app, TestClient(app)


def test_rest_routes_present_and_no_materialize_endpoint():
    from switchboard.api.routers.scm_leases import create_router
    router = create_router(resolve_project=lambda p: p,
                           resolve_principal=lambda *a, **k: {})
    paths = {getattr(r, "path", "") for r in router.routes}
    ok("/api/projects/{project}/scm-leases" in paths, "acquire route is exposed")
    ok("/api/projects/{project}/scm-leases/{lease_id}/release" in paths,
       "release route is exposed")
    ok("/api/projects/{project}/scm-leases/{lease_id}" in paths, "get route is exposed")
    ok(not any("materialize" in p for p in paths),
       "no REST endpoint ever materializes a token")


def test_rest_acquire_unknown_connection_denied_and_get_404():
    _app, client = _rest_client()
    resp = client.post("/api/projects/switchboard/scm-leases", json={
        "connection_id": "scm-missing", "repository": CANONICAL, "org_id": ORG,
        "operations": ["clone"], "task_id": "ENFORCE-13", "generation": "gen-1",
        "context_digest": "ctx", "host_id": "h", "runner_session_id": "r",
        "work_session_id": "ws", "claim_id": "c", "wake_id": "w", "ttl_seconds": 900,
    })
    ok(resp.status_code in (403, 404),
       f"REST acquire against a missing connection is denied ({resp.status_code})")
    missing = client.get("/api/projects/switchboard/scm-leases/scm-lease-missing")
    ok(missing.status_code == 404, "REST get of a missing lease returns 404")


def main() -> int:
    _isolate_registry()
    print("ENFORCE-13 SCM lease broker")
    test_operation_phase_mapping()
    test_principal_from_mapping()
    test_acquire_read_and_write_leases()
    test_acquire_requires_exact_host_wake_claim()
    test_acquire_denies_unauthorized_scope()
    test_acquire_denies_operation_outside_connection_scope()
    test_acquire_rejects_mixed_phase_operations()
    test_acquire_is_idempotent_for_same_principal()
    test_acquire_conflicts_for_different_principal()
    test_lease_projection_carries_no_secret()
    test_materialize_mints_once_and_hides_the_token()
    test_materialize_is_single_consumer_replay_fenced()
    test_materialize_enforces_phase_separation()
    test_materialize_requires_exact_binding()
    test_materialize_fences_on_installation_drift()
    test_materialize_denied_after_connection_revoked()
    test_materialize_denied_after_expiry()
    test_default_minter_is_unavailable_and_fences()
    test_distinct_executions_each_get_a_lease()
    test_release_lease_is_terminal()
    test_fence_leases_for_execution_on_host_loss()
    test_fence_leases_for_connection_on_revocation()
    test_cleanup_expired_leases_sweeps()
    test_mcp_tools_declared_in_auth_census()
    test_mcp_registration_exposes_lease_tools()
    test_acquire_unknown_connection_is_denied_not_crashed()
    test_materialize_denied_after_connection_deleted()
    test_rest_routes_present_and_no_materialize_endpoint()
    test_rest_acquire_unknown_connection_denied_and_get_404()
    print(f"\nENFORCE-13 SCM lease broker: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
