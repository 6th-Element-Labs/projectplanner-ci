#!/usr/bin/env python3
"""Closure-verification worker (DELIVERABLES-23).

Spawned by Agent Host (adapters/agent_host.py) for wakes whose policy declares
``kind: closure_verification`` — the "Verify & stamp closure" dispatch
(DELIVERABLES-17). Unlike the generic inbox-only stub, this actually does the
work: it calls the closure engine in-process and persists a real graded
report. No model is involved — the engine's gates are deterministic (store
queries plus, when ``run_scripts=True``, subprocess execution of the
registered script/pytest gates, each bounded by its own manifest
``timeout_s``/``env_allowlist``; see deliverable_gates/manifest.json). That is
what makes it safe to run unattended: bounded, deterministic checks, not an
open-ended agent loop.

Usage (normally invoked BY agent_host.py via supervisor.py):
    python3 adapters/closure_verifier.py --project switchboard \
        --deliverable-id <id> --host-id host/plan-vm-message-wake \
        [--wake-id wake-...]

Exit 0 once a report is produced and persisted (grade pass/hold/waive are all
a *successful run* of the verifier — only a missing deliverable or a
malformed closure input is a failure). Exit 1 on that failure so the runner
session records it as errored.
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

import deliverable_closure  # noqa: E402
import deliverable_gates  # noqa: E402
import store  # noqa: E402

#: This box is small and shared with the interactive web/MCP processes (see
#: docs/DELIVERABLE-CLOSURE-GATE.md dogfood notes: the 8-agent concurrent-load
#: gate is deliberately run off-box, never inline, for exactly this reason).
#: A gate registered with a timeout above this ceiling is left un-auto-run —
#: it stays "not_run" (holds the grade) instead of risking the shared box.
#: Override per-deployment via PM_CLOSURE_VERIFIER_AUTO_TIMEOUT_CEILING_S.
DEFAULT_AUTO_TIMEOUT_CEILING_S = 120


def _auto_timeout_ceiling():
    raw = os.environ.get("PM_CLOSURE_VERIFIER_AUTO_TIMEOUT_CEILING_S", "")
    try:
        return float(raw) if raw else float(DEFAULT_AUTO_TIMEOUT_CEILING_S)
    except ValueError:
        return float(DEFAULT_AUTO_TIMEOUT_CEILING_S)


def _safe_to_auto_run(deliverable_id, project, ceiling_s):
    """True if every command (script/pytest) gate this deliverable declares fits
    under the auto-run ceiling. A heavy gate (e.g. a concurrent-load harness)
    must still be run off-box and submitted — never fabricated, never risked
    inline on a shared box."""
    deliverable = store.get_deliverable(deliverable_id, project=project)
    if not deliverable:
        return True, []  # let verify_and_record_closure raise the real "not found" error
    proof_requirements = deliverable.get("proof_requirements") or {}
    try:
        resolved = deliverable_gates.resolve_gates(proof_requirements)
    except deliverable_gates.GateResolutionError:
        return True, []  # let the engine raise the real resolution error
    _, functional = deliverable_gates.partition_gates(resolved)
    heavy = [g for g in functional
             if g.get("kind") in ("script", "pytest")
             and float(g.get("timeout_s") or 0) > ceiling_s]
    return (not heavy), [g.get("id") for g in heavy]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run + persist a deliverable closure verification")
    ap.add_argument("--project", default=os.environ.get("PM_PROJECT", "switchboard"))
    ap.add_argument("--deliverable-id", required=True)
    ap.add_argument("--host-id", default=os.environ.get("PM_HOST_ID", "host/unknown"))
    ap.add_argument("--wake-id", default="")
    a = ap.parse_args(argv)

    ceiling = _auto_timeout_ceiling()
    safe, heavy_ids = _safe_to_auto_run(a.deliverable_id, a.project, ceiling)
    if not safe:
        print(json.dumps({"wake_id": a.wake_id, "deliverable_id": a.deliverable_id,
                          "project": a.project,
                          "note": ("declares gate(s) heavier than this host's "
                                   f"{ceiling:.0f}s auto-run ceiling; running scope-only "
                                   "and leaving those not_run rather than risking the "
                                   "shared box — submit their results manually (see the "
                                   "mcp-agent-path-performance dogfood pattern) or run "
                                   "'Verify & stamp closure' from a bigger host"),
                          "heavy_gate_ids": heavy_ids}), flush=True)

    actor = f"closure-verifier/{a.host_id}"
    generated_by = f"agent:{actor}"
    result = deliverable_closure.verify_and_record_closure(
        a.deliverable_id, a.project,
        actor=actor, generated_by=generated_by, run_scripts=safe,
    )
    print(json.dumps({"wake_id": a.wake_id, "deliverable_id": a.deliverable_id,
                      "project": a.project, **result}), flush=True)
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
