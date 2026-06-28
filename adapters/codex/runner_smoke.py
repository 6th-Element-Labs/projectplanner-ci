#!/usr/bin/env python3
"""Managed-runner smoke for the Codex Switchboard adapter.

This does not prove a private/native Codex product hook. It proves the thing Switchboard can
own today: a launcher/runner invokes codex_adapter.on_pre_tool before execution and refuses to
run the candidate when the adapter returns decision=deny.
"""
import argparse
import json
import os
import sys

import codex_adapter


SELF_DONE_CANDIDATE = {
    "toolCall": {
        "name": "mcp__taikun_plan__update_task",
        "arguments": {"status": "Done"},
    }
}


def _read_candidate(raw):
    if not raw.strip():
        return SELF_DONE_CANDIDATE
    return json.loads(raw)


def evaluate_candidate(candidate, offline=False):
    """Return the runner action for a candidate tool call."""
    old_mode = os.environ.get("PM_CODEX_PRETOOL_MODE")
    os.environ["PM_CODEX_PRETOOL_MODE"] = "deny"
    if offline:
        codex_adapter.sb._consume_interrupt = lambda *args, **kwargs: None
        codex_adapter.sb._lease_holder = lambda *args, **kwargs: None
    try:
        verdict = codex_adapter.on_pre_tool(candidate)
    finally:
        if old_mode is None:
            os.environ.pop("PM_CODEX_PRETOOL_MODE", None)
        else:
            os.environ["PM_CODEX_PRETOOL_MODE"] = old_mode

    blocked = verdict.get("decision") == "deny"
    return {
        "runner": "codex-managed-smoke",
        "native_codex_hook_proven": False,
        "runner_honors_deny": True,
        "would_execute": not blocked,
        "runner_action": "blocked_before_execution" if blocked else "would_execute",
        "verdict": verdict,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prove a managed runner honors Codex deny verdicts")
    parser.add_argument("--offline", action="store_true",
                        help="stub board lookups for deterministic local smoke")
    parser.add_argument("--deny-exit-code", type=int, default=20,
                        help="exit code when the runner blocks execution")
    parser.add_argument("--candidate-json", default="",
                        help="candidate tool call JSON; stdin is used when omitted")
    args = parser.parse_args(argv)

    raw = args.candidate_json if args.candidate_json else sys.stdin.read()
    result = evaluate_candidate(_read_candidate(raw), offline=args.offline)
    print(json.dumps(result, indent=2, sort_keys=True))
    return args.deny_exit_code if not result["would_execute"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
