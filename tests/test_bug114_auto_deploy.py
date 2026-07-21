#!/usr/bin/env python3
"""BUG-114/BUG-119: auto-deploy trigger + visible deploy-staleness signal.

Prod used to reach canonical master only when a human remembered to SSH and run
redeploy.sh, so merged/CI-green/Done work could still be absent from the running
system. This proves the trigger deploys iff master advanced, is concurrency-safe,
records failures visibly, and that /health/version surfaces commits-behind
without shelling git on the request path.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT

import deploy_staleness

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


def fake_git(mapping):
    """Build a GitRunner from {argv_tuple: (rc, stdout)}; missing keys -> (1,'')."""
    def run(args):
        return mapping.get(tuple(args), (1, ""))
    return run


# ---- pure payload / state round-trip ------------------------------------------
p = deploy_staleness.staleness_payload("aaa", "bbb", 3)
ok(p["deploy_stale"] is True and p["commits_behind"] == 3,
   "staleness_payload derives deploy_stale=True from a positive commits_behind")
ok(deploy_staleness.staleness_payload("a", "a", 0)["deploy_stale"] is False,
   "staleness_payload derives deploy_stale=False at commits_behind=0")
ok(deploy_staleness.staleness_payload("a", "b", -5)["commits_behind"] == 0,
   "a negative/unknown commits_behind is coerced to 0, never reported as up-to-date-by-accident")

with tempfile.TemporaryDirectory(prefix="bug114-state-") as tmp:
    state = os.path.join(tmp, "sub", "deploy-state.json")
    deploy_staleness.write_state(state, p)
    ok(deploy_staleness.read_state(state) == p,
       "write_state/read_state round-trips through a not-yet-existing directory")
    ok(oct(os.stat(state).st_mode)[-3:] == "644",
       "state file is readable by the web service while only its directory owner can replace it")
    ok(deploy_staleness.read_state(os.path.join(tmp, "missing.json")) is None,
       "read_state returns None for a missing file (never raises)")
    Path(state).write_text("{ not json", encoding="utf-8")
    ok(deploy_staleness.read_state(state) is None,
       "read_state returns None for a corrupt file (degrades, never raises)")

# ---- compute_staleness with an injected git runner ----------------------------
git = fake_git({
    ("rev-parse", "HEAD"): (0, "run111"),
    ("rev-parse", "origin/master"): (0, "canon99"),
    ("rev-list", "--count", "HEAD..origin/master"): (0, "4"),
})
running, canonical, behind, err = deploy_staleness.compute_staleness(git)
ok(running == "run111" and canonical == "canon99" and behind == 4 and err is None,
   "compute_staleness reports running SHA, canonical SHA, and commits-behind")

git_fetchfail = fake_git({
    ("fetch", "--quiet", "origin", "master"): (1, ""),
    ("rev-parse", "HEAD"): (0, "run111"),
    ("rev-parse", "origin/master"): (0, "canon99"),
    ("rev-list", "--count", "HEAD..origin/master"): (0, "2"),
})
r2, c2, b2, e2 = deploy_staleness.compute_staleness(git_fetchfail, fetch=True)
ok(e2 == "fetch_failed" and r2 == "run111" and b2 == 2,
   "a fetch failure is reported but the last-known SHAs still resolve (signal degrades, not erased)")

# ---- health_view mapping ------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="bug114-hv-") as tmp:
    missing = os.path.join(tmp, "none.json")
    ok(deploy_staleness.health_view(missing) == {
        "schema": deploy_staleness.SCHEMA, "deploy_signal": "unknown"},
       "health_view reports deploy_signal=unknown when no signal has been written yet")
    deploy_staleness.write_state(missing, deploy_staleness.staleness_payload(
        "run", "canon", 5, checked_at=123.0))
    hv = deploy_staleness.health_view(missing)
    ok(hv["deploy_signal"] == "stale" and hv["commits_behind"] == 5
       and hv["running_sha"] == "run" and "canonical_sha" in hv,
       "health_view reports deploy_signal=stale with commits_behind when prod is behind")
    deploy_staleness.write_state(missing, deploy_staleness.staleness_payload(
        "run", "run", 0))
    ok(deploy_staleness.health_view(missing)["deploy_signal"] == "current",
       "health_view reports deploy_signal=current when prod matches canonical master")

# ---- default_state_path resolution --------------------------------------------
old_env = {k: os.environ.get(k) for k in (
    "PM_DEPLOY_STATE_UNIT_FILE", "PM_DEPLOY_STATE_FILE", "PM_DB_PATH")}
try:
    os.environ["PM_DEPLOY_STATE_UNIT_FILE"] = "/unit-owned/deploy.json"
    os.environ["PM_DEPLOY_STATE_FILE"] = "/explicit/deploy.json"
    ok(deploy_staleness.default_state_path() == "/unit-owned/deploy.json",
       "default_state_path keeps the unit-owned path authoritative over a stale .env override")
    del os.environ["PM_DEPLOY_STATE_UNIT_FILE"]
    ok(deploy_staleness.default_state_path() == "/explicit/deploy.json",
       "default_state_path honours an explicit PM_DEPLOY_STATE_FILE")
    del os.environ["PM_DEPLOY_STATE_FILE"]
    os.environ["PM_DB_PATH"] = "/var/lib/projectplanner/switchboard.db"
    ok(deploy_staleness.default_state_path()
       == "/var/lib/projectplanner/deploy-state.json",
       "default_state_path falls back to the board db's data dir (service-owned on prod)")
finally:
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ---- /health/version route is public and reads the signal ---------------------
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from switchboard.api.routers.health import create_router

    with tempfile.TemporaryDirectory(prefix="bug114-route-") as tmp:
        state = os.path.join(tmp, "deploy-state.json")
        os.environ["PM_DEPLOY_STATE_FILE"] = state
        app = FastAPI()
        app.include_router(create_router(
            resolve_project=lambda p: p,
            resolve_principal=lambda *a, **k: {},
            saturation_snapshot=lambda p: {},
            project_init_failures=lambda: {}))
        client = TestClient(app)

        r = client.get("/health/version")
        ok(r.status_code == 200 and r.json()["deploy_signal"] == "unknown",
           "/health/version is reachable with no auth and reports unknown before any signal")

        deploy_staleness.write_state(state, deploy_staleness.staleness_payload(
            "runsha", "canonsha", 7, checked_at=1.0))
        body = client.get("/health/version").json()
        ok(body["deploy_signal"] == "stale" and body["commits_behind"] == 7,
           "/health/version surfaces 'prod is N behind master' from the timer's state file")
        ok("task_id" not in body and "projects_configured" not in body,
           "/health/version exposes only deployment metadata, never project/readiness data")
finally:
    os.environ.pop("PM_DEPLOY_STATE_FILE", None)


# ---- auto_deploy.sh end to end against a real local git repo ------------------
def git_run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_repos(base: Path):
    origin = base / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", str(origin)], check=True)
    seed = base / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    git_run(seed, "config", "user.email", "t@example.test")
    git_run(seed, "config", "user.name", "BUG-114")
    (seed / "f0").write_text("0", encoding="utf-8")
    git_run(seed, "add", "-A")
    git_run(seed, "commit", "-qm", "c0")
    git_run(seed, "push", "-q", "origin", "master")
    prod = base / "prod"
    subprocess.run(["git", "clone", "-q", str(origin), str(prod)], check=True)
    git_run(prod, "config", "user.email", "t@example.test")
    git_run(prod, "config", "user.name", "BUG-114")
    return origin, seed, prod


def advance_origin(seed: Path):
    n = subprocess.run(["git", "-C", str(seed), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True, check=True).stdout.strip()
    (seed / f"f{n}").write_text(n, encoding="utf-8")
    git_run(seed, "add", "-A")
    git_run(seed, "commit", "-qm", f"c{n}")
    git_run(seed, "push", "-q", "origin", "master")


AUTODEPLOY = ROOT / "deploy" / "auto_deploy.sh"


def run_auto_deploy(prod: Path, state: Path, redeploy_cmd: str, env_extra=None):
    env = dict(os.environ)
    env.update({
        "PLAN_ROOT": str(prod),
        "PM_DEPLOY_STATE_FILE": str(state),
        "AUTODEPLOY_PYTHON": "python3",
        "AUTODEPLOY_REDEPLOY_CMD": redeploy_cmd,
        # The test repo is user-owned, so the privileged fetch needs no sudo.
        "AUTODEPLOY_SUDO": "",
        # deploy_staleness lives at the real repo root; the fake prod checkout has
        # no copy, so point the script's imports/module path at the real ROOT.
        "PYTHONPATH": str(ROOT),
    })
    if env_extra:
        env.update(env_extra)
    # The script does `$PYTHON $ROOT/deploy_staleness.py ...` with ROOT=PLAN_ROOT,
    # so stage the module + script into the fake prod checkout's deploy/ tree.
    (prod / "deploy").mkdir(exist_ok=True)
    (prod / "deploy_staleness.py").write_bytes((ROOT / "deploy_staleness.py").read_bytes())
    return subprocess.run(["bash", str(AUTODEPLOY)], env=env,
                          capture_output=True, text=True)


with tempfile.TemporaryDirectory(prefix="bug114-deploy-") as raw:
    base = Path(raw)
    origin, seed, prod = build_repos(base)
    state = base / "deploy-state.json"
    marker = base / "redeployed"

    # A fake redeploy that (success) fast-forwards prod to origin/master, like the
    # real redeploy.sh's `git pull`, and records that it ran.
    ok_redeploy = base / "redeploy_ok.sh"
    ok_redeploy.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        f'echo ran >> "{marker}"\n'
        f'git -C "{prod}" merge --ff-only origin/master >/dev/null 2>&1\n',
        encoding="utf-8")
    ok_redeploy.chmod(0o755)

    # Case 1: up to date -> no deploy, signal current.
    r = run_auto_deploy(prod, state, f"bash {ok_redeploy}")
    sig = deploy_staleness.read_state(str(state)) or {}
    ok(r.returncode == 0 and not marker.exists() and sig.get("commits_behind") == 0,
       "auto_deploy: up-to-date tick refreshes the signal and does NOT redeploy")

    # BUG-119: a stale PM_DEPLOY_STATE_FILE inherited from .env must not override
    # the systemd-owned path, because EnvironmentFile wins over Environment.
    unit_state = base / "unit-owned" / "deploy-state.json"
    stale_operator_state = base / "stale-operator-state.json"
    r = run_auto_deploy(
        prod, stale_operator_state, f"bash {ok_redeploy}",
        env_extra={"PM_DEPLOY_STATE_UNIT_FILE": str(unit_state)})
    ok(r.returncode == 0 and unit_state.exists() and not stale_operator_state.exists(),
       "auto_deploy: the unit-owned state path wins over a stale operator .env path")

    # Case 2: origin advances -> deploy fires, signal records success + behind 0.
    advance_origin(seed)
    r = run_auto_deploy(prod, state, f"bash {ok_redeploy}")
    sig = deploy_staleness.read_state(str(state)) or {}
    ok(r.returncode == 0 and marker.exists()
       and sig.get("last_deploy_ok") is True and sig.get("commits_behind") == 0,
       "auto_deploy: when master advanced it runs redeploy and records a clean deploy (behind->0)")

    # Case 3: origin advances again but redeploy FAILS -> nonzero exit, failure recorded.
    advance_origin(seed)
    fail_redeploy = base / "redeploy_fail.sh"
    fail_redeploy.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 7\n", encoding="utf-8")
    fail_redeploy.chmod(0o755)
    marker.unlink(missing_ok=True)
    r = run_auto_deploy(prod, state, f"bash {fail_redeploy}")
    sig = deploy_staleness.read_state(str(state)) or {}
    ok(r.returncode == 7 and sig.get("last_deploy_ok") is False
       and (sig.get("commits_behind") or 0) > 0 and sig.get("last_deploy_error"),
       "auto_deploy: a redeploy failure exits non-zero and records the failure with prod still behind")

    # Case 4: locking — while the lock is held, a tick skips (exit 0, no deploy).
    # The script takes an atomic mkdir lock at "<lock file>.d"; hold it by
    # pre-creating that directory, then confirm the tick declines to deploy even
    # though master has advanced.
    lock = base / "auto-deploy.lock"
    lock_dir = base / "auto-deploy.lock.d"
    lock_dir.mkdir()
    try:
        advance_origin(seed)
        deploy_marker_before = marker.exists()
        r = run_auto_deploy(prod, state, f"bash {ok_redeploy}",
                            env_extra={"AUTODEPLOY_LOCK_FILE": str(lock)})
        ok(r.returncode == 0 and "skipping" in (r.stdout + r.stderr).lower()
           and marker.exists() == deploy_marker_before,
           "auto_deploy: a concurrent run holding the lock skips this tick (no second redeploy)")
    finally:
        lock_dir.rmdir()

    # Case 5 (BUG-119): mkdir failure without an extant lock is not contention.
    # A regular file in the parent position produces deterministic ENOTDIR even
    # when the test process happens to run as root.
    blocked_parent = base / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    r = run_auto_deploy(
        prod, state, f"bash {ok_redeploy}",
        env_extra={"AUTODEPLOY_LOCK_FILE": str(blocked_parent / "auto-deploy.lock")})
    ok(r.returncode != 0
       and "lock acquisition FAILED" in (r.stdout + r.stderr)
       and "holds the lock" not in (r.stdout + r.stderr),
       "auto_deploy: an unwritable/invalid lock path exits nonzero instead of masquerading as contention")


# ---- systemd unit sanity ------------------------------------------------------
svc = (ROOT / "deploy" / "projectplanner-autodeploy.service").read_text()
tmr = (ROOT / "deploy" / "projectplanner-autodeploy.timer").read_text()
ok("deploy/auto_deploy.sh" in svc, "autodeploy.service invokes deploy/auto_deploy.sh")
svc_directives = [ln.strip() for ln in svc.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
ok(not any(d.startswith(("NoNewPrivileges=yes", "ProtectSystem=strict"))
           for d in svc_directives),
   "autodeploy.service sets no escalation-blocking directive (it must escalate to deploy)")
ok(any(d == "User=ubuntu" for d in svc_directives),
   "autodeploy.service runs as the ubuntu login that holds sudo for redeploy.sh")
state_path = "/var/lib/projectplanner-autodeploy/deploy-state.json"
web_svc = (ROOT / "deploy" / "projectplanner.service").read_text()
ok(f"Environment=PM_DEPLOY_STATE_UNIT_FILE={state_path}" in svc
   and f"Environment=PM_DEPLOY_STATE_UNIT_FILE={state_path}" in web_svc,
   "autodeploy and /health/version share the least-privilege deployment signal path")
ok("StateDirectory=projectplanner-autodeploy" in svc
   and "StateDirectoryMode=0755" in svc
   and "RuntimeDirectory=projectplanner-autodeploy" in svc
   and "RuntimeDirectoryMode=0700" in svc
   and "Environment=AUTODEPLOY_LOCK_FILE=/run/projectplanner-autodeploy/" in svc,
   "autodeploy.service gives state and lock explicit systemd-managed least-privilege directories")
ok("OnUnitActiveSec" in tmr and "[Install]" in tmr and "timers.target" in tmr,
   "autodeploy.timer fires on an interval and installs into timers.target")

print(f"\nBUG-114 auto-deploy: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
