#!/usr/bin/env python3
"""Every server-side GitHub caller resolves its credential through github_app_auth.

Guards the actual regression risk: the module can be perfect and the fleet still
burns a personal PAT's 5,000/hr because one call site never got wired. Each check
configures a fake App, asserts the caller uses the minted token, and asserts the
PAT-only path is unchanged.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import github_app_auth as ga  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def use_fake_app(minted="ghs_wired"):
    """Configure an App whose mint is stubbed — no key material, no network."""
    for name in (ga.APP_ID_ENV, ga.PRIVATE_KEY_PATH_ENV, ga.PRIVATE_KEY_ENV,
                 ga.INSTALLATION_ID_ENV, ga.INSTALLATION_MAP_ENV,
                 "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN",
                 "SWITCHBOARD_CI_DISPATCH_TOKEN"):
        os.environ.pop(name, None)
    ga.reset_cache()
    seen = []
    ga.app_configured = lambda: True                      # type: ignore[assignment]
    ga.installation_token = (                             # type: ignore[assignment]
        lambda repo="", **kw: (seen.append(repo), minted)[1])
    return seen


def restore_app():
    ga.app_configured = _real_configured                  # type: ignore[assignment]
    ga.installation_token = _real_mint                    # type: ignore[assignment]
    ga.reset_cache()


_real_configured = ga.app_configured
_real_mint = ga.installation_token

print("\n-- open_prs (the Fleet dock's own feed) --")
import open_prs  # noqa: E402
seen = use_fake_app()
ok(open_prs._token("6th-Element-Labs/projectplanner") == "ghs_wired",
   "open_prs._token routes through github_app_auth")
ok(seen and seen[-1] == "6th-Element-Labs/projectplanner",
   "open_prs passes the repo so the right installation is used")
restore_app()
os.environ["PM_GITHUB_TOKEN"] = "tok-pat"
ok(open_prs._token("o/r") == "tok-pat", "with no App configured open_prs still uses the PAT")
os.environ.pop("PM_GITHUB_TOKEN", None)

print("\n-- the claim / merge-authorization gate (the heaviest consumer) --")
import switchboard_pr_gate as gate  # noqa: E402
seen = use_fake_app()
ok(gate._token("StevenRidder/Helm") == "ghs_wired", "gate._token routes through github_app_auth")
ok(seen and seen[-1] == "StevenRidder/Helm",
   "the gate resolves per repo — Helm is a different installation than the org repos")
restore_app()
os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "tok-ci"
os.environ["PM_GITHUB_TOKEN"] = "tok-pm"
ok(gate._token("o/r") == "tok-ci", "the gate keeps its own PAT precedence (CI token first)")
os.environ.pop("SWITCHBOARD_CI_GITHUB_TOKEN", None)
os.environ.pop("PM_GITHUB_TOKEN", None)

print("\n-- the gate fetches each PR's file list once, not once per gate --")
calls = []
gate.list_pr_files = lambda repo, number, token="": (  # type: ignore[assignment]
    calls.append((repo, number)), ["a.py"])[1]
gate.post_status = lambda *a, **k: None                 # type: ignore[assignment]
pr = {"number": 7, "head": {"sha": "a" * 40}, "html_url": "https://github.com/o/r/pull/7",
      "title": "TASK-1 thing", "base": {"ref": "master"}, "user": {"login": "bot"}}
gate.run_claim_gate_for_pr(pr, repo="o/r", token="t", context="c", mode="enforce",
                           changed_paths=["a.py"])
ok(calls == [], "a supplied changed_paths list means no extra GET")
gate.run_claim_gate_for_pr(pr, repo="o/r", token="t", context="c", mode="enforce")
ok(len(calls) == 1, "and it still fetches for itself when none is supplied")

print("\n-- push verification --")
import push_verification as pv  # noqa: E402
seen = use_fake_app()
ok(pv.github_token_from_env(repo="o/r") == "ghs_wired",
   "push_verification routes through github_app_auth")
ok(pv.github_token_from_env({"PM_GITHUB_TOKEN": "explicit-env"}) == "explicit-env",
   "an explicit env mapping stays pure env-only")
restore_app()

print("\n-- CI verify dispatch --")
import ci_verify_dispatch as cvd  # noqa: E402
seen = use_fake_app()
ok(cvd._token(repo="o/r") == "ghs_wired", "dispatch routes through github_app_auth")
os.environ["SWITCHBOARD_CI_DISPATCH_TOKEN"] = "tok-dispatch"
ok(cvd._token(repo="o/r") == "tok-dispatch",
   "a dedicated dispatch PAT still wins — it may be scoped to a repo the App is not on")
os.environ.pop("SWITCHBOARD_CI_DISPATCH_TOKEN", None)
restore_app()

print("\n-- reconcile / provenance --")
import store  # noqa: E402
seen = use_fake_app()
ok(store._github_token("o/r") == "ghs_wired", "store._github_token routes through github_app_auth")
restore_app()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
