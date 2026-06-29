#!/usr/bin/env python3
"""Supervisable agent entrypoint — the process a supervisor spawns to drive the autonomous loop.

The connective piece between Codex's supervisor (adapters/codex/supervisor.py, which spawns +
keeps-alive + can hard-kill a child) and the runtime-agnostic driver (switchboard_core.run_session).
The supervisor launches THIS; it runs handshake → claim_next → work_fn → complete_claim → repeat.

Modes:
  --dry           claim → log → ABANDON the claim (proves the supervised loop without fabricating
                  completions). Safe to run against the live board.
  (real)          work_fn must do the claimed task (run the runtime's model) and return evidence
                  {branch, head_sha}. Wire it via --work-module pkg.attr → callable(task)->evidence.

Usage (normally invoked BY the supervisor):
  python3 adapters/codex/supervisor.py start --agent-id claude/run-1 -- \
      python3 adapters/run_agent.py --dry --lanes ADAPTER --max-tasks 2
Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID (supervisor injects PM_AGENT_ID).
"""
import argparse
import importlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # put adapters/ on path
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
INBOX_ONLY_ACK_RESPONSE = (
    "received by inbox-only adapter; no model/action completion performed"
)


def dry_work_fn(task):
    print(f"[run_agent] would work {task.get('task_id')} — DRY, abandoning (no completion)", flush=True)
    raise RuntimeError("dry-run: not executing work")  # run_session abandons the claim on raise


def _load_work_fn(spec):
    """Load 'package.module:attr' → callable(task)->evidence dict, supplied by the runtime."""
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr)


def inbox_only(agent_id, runtime, idle_seconds, ack_inbox=False):
    """Register and read inbox without calling claim_next.

    This is used by Agent Host for message-only wakes. It proves the runtime adapter reached
    Switchboard and surfaced pending messages, but cannot accidentally take global work.
    """
    sb.handshake(PROJECT, agent_id, runtime, lane="")
    messages = sb.inbox(PROJECT, agent_id)
    acked_ids = []
    ack_errors = []
    if ack_inbox:
        for msg in messages:
            if msg.get("acked_at") or not msg.get("requires_ack", True):
                continue
            message_id = msg.get("id")
            if not message_id:
                continue
            try:
                sb.ack(PROJECT, message_id, response=INBOX_ONLY_ACK_RESPONSE)
                acked_ids.append(message_id)
            except Exception as e:
                ack_errors.append({"message_id": message_id, "error": str(e)})
    print(json.dumps({"agent_id": agent_id, "mode": "inbox_only",
                      "unacked_messages": messages,
                      "acked_message_ids": acked_ids,
                      "ack_errors": ack_errors}), flush=True)
    if idle_seconds > 0:
        time.sleep(idle_seconds)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Switchboard supervisable agent entrypoint")
    ap.add_argument("--lanes", default="", help="comma-separated lane filter (blank = any)")
    ap.add_argument("--max-tasks", type=int, default=3)
    ap.add_argument("--runtime", default=os.environ.get("PM_RUNTIME", "claude-code"))
    ap.add_argument("--dry", action="store_true", help="claim+abandon; never complete (safe)")
    ap.add_argument("--work-module", default="", help="pkg.mod:attr for the real work_fn")
    ap.add_argument("--inbox-only", action="store_true",
                    help="register and read inbox; never call claim_next")
    ap.add_argument("--ack-inbox", action="store_true",
                    help="ack unacked inbox messages as adapter receipt only")
    ap.add_argument("--idle-seconds", type=float, default=0.0,
                    help="keep the process alive briefly for supervisor readiness checks")
    a = ap.parse_args(argv)

    me = os.environ.get("PM_AGENT_ID") or sb.agent_id()
    if a.inbox_only:
        return inbox_only(me, a.runtime, max(0.0, a.idle_seconds),
                          ack_inbox=a.ack_inbox)
    if a.dry:
        work_fn = dry_work_fn
    elif a.work_module:
        work_fn = _load_work_fn(a.work_module)
    else:
        print(json.dumps({"error": "supply --dry or --work-module pkg.mod:attr"}), flush=True)
        return 2

    res = sb.run_session(PROJECT, me, a.runtime, work_fn,
                         lanes=a.lanes or None, max_tasks=a.max_tasks)
    print(json.dumps({"agent_id": me, "result": res}), flush=True)
    if a.idle_seconds > 0:
        time.sleep(a.idle_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
