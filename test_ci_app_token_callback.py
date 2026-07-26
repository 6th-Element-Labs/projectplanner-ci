#!/usr/bin/env python3
"""CI's status callback must not depend on a shared personal access token.

On 2026-07-26 the fleet spent the whole 5,000/hr budget of the account that owns
`PRIVATE_READ_TOKEN`, and verify.yml's very first step — posting the pending
status — started returning `403 API rate limit exceeded`. Runs died in 60s having
executed no tests, so no PR in the fleet could go green, including the PR that
fixed the rate limit. These checks keep the callback on an App installation token
(its own budget) and keep the PAT strictly as a fallback.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".github", "workflows", "verify.yml")
APP_TOKEN_EXPR = "steps.app_token.outputs.token"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


with open(WORKFLOW, "r", encoding="utf-8") as handle:
    workflow = yaml.safe_load(handle)

steps = workflow["jobs"]["verify"]["steps"]
by_name = {str(s.get("name") or ""): s for s in steps}

print("\n-- the callback token is minted from the GitHub App --")
mint = next((s for s in steps if s.get("id") == "app_token"), None)
ok(mint is not None, "verify.yml has an app_token minting step")
ok(mint and str(mint.get("uses") or "").startswith("actions/create-github-app-token"),
   "it uses the official create-github-app-token action")
ok(mint and mint.get("continue-on-error") is True,
   "minting is continue-on-error, so a repo without the App secrets still runs on the PAT")
with_block = (mint or {}).get("with") or {}
ok("SWITCHBOARD_APP_ID" in str(with_block.get("app-id")),
   "app id comes from the SWITCHBOARD_APP_ID secret")
ok("SWITCHBOARD_APP_PRIVATE_KEY" in str(with_block.get("private-key")),
   "private key comes from the SWITCHBOARD_APP_PRIVATE_KEY secret")

print("\n-- the App token is minted BEFORE the first callback --")
mint_index = next(i for i, s in enumerate(steps) if s.get("id") == "app_token")
first_callback = next(i for i, s in enumerate(steps)
                      if "GH_TOKEN" in ((s.get("env") or {})))
ok(mint_index < first_callback,
   "the mint step precedes every step that posts a status")

print("\n-- every status callback prefers the App and only falls back to the PAT --")
callbacks = [(name, s) for name, s in by_name.items()
             if "GH_TOKEN" in ((s.get("env") or {}))]
ok(len(callbacks) >= 4,
   f"all four callback steps are covered (found {len(callbacks)})")
for name, step in callbacks:
    expr = str((step["env"])["GH_TOKEN"])
    ok(APP_TOKEN_EXPR in expr, f"{name!r} uses the App token")
    ok(expr.index(APP_TOKEN_EXPR) < expr.index("PRIVATE_READ_TOKEN")
       if "PRIVATE_READ_TOKEN" in expr else True,
       f"{name!r} prefers the App token over the PAT")

print("\n-- a run with no credential at all fails loudly, not silently --")
assertion = by_name.get("Assert a status callback credential is available")
ok(assertion is not None, "there is an explicit credential preflight")
body = str((assertion or {}).get("run") or "")
ok("exit 1" in body, "it exits nonzero when neither credential is present")
ok("SWITCHBOARD_APP_ID" in body and "PRIVATE_READ_TOKEN" in body,
   "its message names both credentials so the operator knows what to set")

print("\n-- the checkout still carries no credential --")
checkout = by_name.get("Check out scratchpad branch")
ok((checkout or {}).get("with", {}).get("persist-credentials") is False,
   "the scratchpad checkout stays credential-free (public mirror, nothing to read)")

print("\n-- the mirror's subprocess credential prefers the App too --")
import external_ci_mirror  # noqa: E402
source = open(external_ci_mirror.__file__, "r", encoding="utf-8").read()
default_run = source[source.index("def _default_run"):source.index("def _run(")]
ok("github_app_auth" in default_run,
   "_default_run resolves GH_TOKEN through github_app_auth")
ok(default_run.index("github_app_auth") < default_run.index("SWITCHBOARD_CI_GITHUB_TOKEN"),
   "it tries the App before the PAT chain")
hint = external_ci_mirror._canonical_repo_hint()
ok("/" in hint, f"the installation hint is a real owner/repo slug ({hint})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
