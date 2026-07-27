#!/usr/bin/env python3
"""The public CI trust boundary keeps agent code and callback credentials apart."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

WORKFLOW = Path(__file__).resolve().parent / ".github" / "workflows" / "verify.yml"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


workflow_source = WORKFLOW.read_text(encoding="utf-8")
workflow = yaml.safe_load(workflow_source)
jobs = workflow["jobs"]

print("\n-- workflow authority is trusted and manually dispatched --")
ok("workflow_dispatch:" in workflow_source and '"ci/**"' not in workflow_source,
   "mirrored branch pushes cannot select or execute their own workflow")
ok("source_ref:" in workflow_source and "source_sha:" in workflow_source
   and "refs/tags/ci/" in workflow_source,
   "trusted workflow accepts only an exact scratchpad tag and canonical SHA")

print("\n-- untrusted code executes in a secret-free job --")
suite = jobs["suite"]
suite_source = str(suite)
ok("secrets." not in suite_source and "GH_TOKEN" not in suite_source,
   "suite job has no secret or callback-token expression")
checkout = next(s for s in suite["steps"] if s.get("id") == "checkout")
ok(checkout.get("with", {}).get("persist-credentials") is False,
   "scratchpad checkout persists no credential")
ok(any(s.get("name") == "Verify exact mirrored SHA" for s in suite["steps"]),
   "suite verifies checkout HEAD equals the requested canonical SHA")

print("\n-- only isolated callback jobs mint the dedicated App token --")
for job_name in ("announce", "report"):
    steps = jobs[job_name]["steps"]
    mint = next((s for s in steps if s.get("id") == "app_token"), None)
    ok(mint is not None, f"{job_name} has an App token minting step")
    ok(mint and str(mint.get("uses") or "").startswith(
        "actions/create-github-app-token"), f"{job_name} uses the official App action")
    with_block = (mint or {}).get("with") or {}
    ok("SWITCHBOARD_APP_CLIENT_ID" in str(with_block.get("client-id")),
       f"{job_name} reads the dedicated App client id")
    ok("app-id" not in with_block,
       f"{job_name} does not use the deprecated App id input")
    ok("SWITCHBOARD_APP_PRIVATE_KEY" in str(with_block.get("private-key")),
       f"{job_name} reads the dedicated App private key")

callbacks = []
for job_name in ("announce", "report"):
    callbacks.extend(
        (job_name, step) for step in jobs[job_name]["steps"]
        if "GH_TOKEN" in (step.get("env") or {})
    )
ok(len(callbacks) == 2, "one pending and one terminal callback remain")
for job_name, step in callbacks:
    ok(str(step["env"]["GH_TOKEN"]) == "${{ steps.app_token.outputs.token }}",
       f"{job_name} callback uses only its job-local App token")

ok("PRIVATE_READ_TOKEN" not in workflow_source,
   "the retired PAT secret is absent from the trusted workflow")

print("\n-- the mirror dispatcher also prefers GitHub App identity --")
import external_ci_mirror  # noqa: E402

source = Path(external_ci_mirror.__file__).read_text(encoding="utf-8")
default_run = source[source.index("def _default_run"):source.index("def _run(")]
ok("github_app_auth" in default_run,
   "_default_run resolves GH_TOKEN through github_app_auth")
ok(default_run.index("github_app_auth") < default_run.index(
    "SWITCHBOARD_CI_GITHUB_TOKEN"), "it tries the App before the PAT chain")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
