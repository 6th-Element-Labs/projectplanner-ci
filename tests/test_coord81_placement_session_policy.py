#!/usr/bin/env python3
"""COORD-81: a host advertises capacity, not policy.

On 2026-07-27 COORD-79 and COORD-80 held pending wakes with
``eligible_host_count: 0`` while ``host/steve-mbp-co16`` was online, accepting,
and holding 16 free session slots. Every physical requirement matched — codex
and git present, canonical repo, right provider, right trust zone, worktree
isolation. The single refusal was::

    reason_codes: ["session_policy_not_supported"]

The wake asked for ``docs_review``; the host advertised ``code_strict``.

Two things were wrong with that check:

  * it was plain set membership, with no notion of one profile subsuming
    another — so the STRICTEST profile produced the NARROWEST eligibility, and
    ``code_strict`` (the ``PM_HOST_SESSION_POLICIES`` default) was refused work
    it was more than qualified to run;
  * the host does not enforce session policy at all. It echoed an env var at
    registration and nothing read it back. ``work_session_required``,
    ``requires_executed_tests``, ``requires_diff_check``, ``requires_upstream``
    and ``requires_branch_task_scope`` are enforced server-side in
    ``merge_gate`` / ``pre_tool_check`` / ``claims`` / ``work_sessions``,
    identically regardless of which host ran the job.

So the dimension is gone. This file pins both halves of that decision: the
refusal must never come back, AND the dimensions that encode real physical or
security facts about the box must still refuse. Deleting a filter is only safe
while the filters that matter still bite.
"""
from __future__ import annotations

from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

from switchboard.domain.coordination import placement as placement_mod  # noqa: E402
from switchboard.domain.coordination.placement import (  # noqa: E402
    HOST_PLACEMENT_SCHEMA,
    evaluate_host,
)

PROJECT = "switchboard"
REPO = "6th-Element-Labs/projectplanner"

passed = 0
failed = 0


def ok(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


def host(**overrides):
    """The steve-mbp-co16 shape: healthy, idle, code_strict-only."""
    placement = {
        "schema": HOST_PLACEMENT_SCHEMA,
        "host_class": "persistent",
        "cost_class": "already_paid",
        "wakeable": True,
        "drain_state": "accepting",
        "projects": [PROJECT],
        "trust_zone": "org_shared",
        "tenant_ids": ["org-6th-element-labs"],
        "providers": ["openai-codex"],
        "account_affinity_ids": [],
        "supports_credential_leases": False,
        "supports_scm_materialization": True,
        "scm_providers": ["github_app", "github"],
        "repositories": [REPO],
        "isolation_modes": ["task_worktree"],
        "workspace_backends": ["worktree"],
        "runtime_binaries": ["codex", "gh", "git", "python3"],
        "provider_capacity_mode": "external_account_admission",
        # The field the dead filter used to read. Left in the fixture on
        # purpose: a real host may still advertise it from an older bundle, and
        # placement must simply not care.
        "session_policies": ["code_strict"],
        "concurrency": {"max_sessions": 16},
        "resources": {
            "cpu_available": 12, "memory_mb_available": 24576,
            "disk_gb_available": 26.69,
        },
    }
    placement.update(overrides.pop("placement", {}))
    value = {
        "host_id": "host/steve-mbp-co16",
        "status": "online",
        "stale": False,
        "limits": {"max_sessions": 16},
        "capacity": {"active_sessions": 0, "placement": placement},
        "runtimes": [{
            "runtime": "codex",
            "provider": "openai",
            "lanes": [],
            "capabilities": [
                "python", "tests", "github",
                "execution_lease_v2", "runner_lease_enforcement",
            ],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
    }
    value.update(overrides)
    return value


def policy(**request_overrides):
    """The COORD-79 wake's placement request."""
    request = {
        "canonical_repo": REPO,
        "repo_role": "canonical",
        "host_classes": ["ephemeral", "persistent"],
        "trust_zones": ["org_shared"],
        "isolation": "task_worktree",
        "workspace_backend": "worktree",
        "runtime_binaries": ["codex", "git"],
        "provider": "openai-codex",
        "scm_provider": "github_app",
    }
    request.update(request_overrides)
    return {
        "mode": "connect",
        "scheduler": {
            "mode": "hybrid", "prefer_persistent": True,
            "allow_persistent": True, "allow_ephemeral": True,
            "burst_enabled": True, "fair_share_key": f"project:{PROJECT}",
        },
        "placement": request,
    }


SELECTOR = {
    "runtime": "codex", "lane": "COORD",
    "agent_id": "agent/codex/coord-79",
    "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
}


def evaluate(host_value, policy_value):
    return evaluate_host(host_value, SELECTOR, policy_value, project=PROJECT)


# ---------------------------------------------------------------------------
# 1. The exact COORD-79 refusal, and it must not come back.
# ---------------------------------------------------------------------------

result = evaluate(host(), policy(session_policy="docs_review"))
ok(result["eligible"] is True,
   "the COORD-79 shape is eligible: a code_strict host takes a docs_review job")
ok("session_policy_not_supported" not in result["reason_codes"],
   "session_policy_not_supported is never emitted")

for requested in ("docs_review", "code_strict", "offline_evidence", "unknown_profile"):
    verdict = evaluate(host(), policy(session_policy=requested))
    ok(verdict["eligible"] is True,
       f"session_policy={requested!r} does not affect eligibility")

no_advertisement = host()
no_advertisement["capacity"]["placement"].pop("session_policies")
ok(evaluate(no_advertisement, policy(session_policy="docs_review"))["eligible"] is True,
   "a host that advertises no session_policies at all is still eligible")

source = Path(placement_mod.__file__).read_text(encoding="utf-8")
ok("session_policy_not_supported" not in source,
   "the reason code does not exist anywhere in placement.py")
ok('request.get("session_policy")' not in source,
   "placement no longer reads a session_policy request field")


# ---------------------------------------------------------------------------
# 2. The filters that encode real facts about the box must still bite.
#
# This is the other half of the change. Removing a filter is only safe while
# the ones that matter still refuse; without these, "we deleted a check" is
# indistinguishable from "we deleted the checks".
# ---------------------------------------------------------------------------

REAL_REFUSALS = [
    ("runtime_binary_missing", host(placement={"runtime_binaries": ["git"]}),
     policy(), "a host without the codex binary cannot run a codex job"),
    ("repository_not_available",
     host(placement={"repositories": ["other/repo"],
                     "supports_scm_materialization": False}),
     policy(), "a host that cannot reach the canonical repo is refused"),
    ("provider_not_allowed", host(placement={"providers": ["anthropic"]}),
     policy(), "a host without the requested provider is refused"),
    ("trust_zone_not_allowed", host(placement={"trust_zone": "public"}),
     policy(), "a host outside the requested trust zone is refused"),
    ("workspace_backend_not_supported",
     host(placement={"workspace_backends": ["external"]}),
     policy(), "a host that cannot materialize the workspace backend is refused"),
    ("isolation_policy_not_supported",
     host(placement={"isolation_modes": ["shared"]}),
     policy(), "a host that cannot isolate the workspace is refused"),
    ("host_unavailable", host(status="offline"), policy(),
     "an offline host is refused"),
]
for code, unfit, request, label in REAL_REFUSALS:
    verdict = evaluate(unfit, request)
    ok(code in verdict["reason_codes"] and verdict["eligible"] is False,
       f"{label} ({code})")

print(f"\ncoord81 placement session policy: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
