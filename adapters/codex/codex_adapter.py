#!/usr/bin/env python3
"""Codex adapter — Switchboard Tier-2-ready shim (ADR-0004 / ADAPTER-2).

SKELETON authored by claude-code (decision #2): the coordination logic is done and proven in
adapters/switchboard_core.py. This file shows where to wire it to Codex's runtime. The two
`TODO(codex)` blocks are the ONLY Codex-runtime-specific parts — only Codex knows its own hook
lifecycle + how its pre-tool hook receives the pending call and signals a deny.

Contract (must do, per ADR-0004):
  1. On session start: surface the working agreement as first-turn context + register_agent.
  2. On each tool call (if the runtime allows a pre-tool hook): deny self-Done, deny edits to a
     file another agent holds, and consume inbound stop/redirect signals (FR-14).
  3. Advertise control fidelity so the board knows how strongly this agent is governed.

Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID (e.g. 'codex/<task>').
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
RUNTIME = "codex"


def codex_agent_id(cwd=None):
    """Stable Codex id. PM_AGENT_ID wins; otherwise prefer codex/<git-branch>."""
    if os.environ.get("PM_AGENT_ID"):
        return os.environ["PM_AGENT_ID"]
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3, cwd=cwd or None)
        if b.returncode == 0 and b.stdout.strip():
            return f"codex/{b.stdout.strip()}"
    except Exception:
        pass
    return "codex/current"


def control_fidelity():
    """Advertise the real guarantee this process has.

    Default is honest advisory mode: Codex can read/register/poll, but native blocking hook
    support is still unverified. Set PM_CODEX_PRETOOL_MODE=deny only when a Codex launcher
    actually invokes this script before every tool and honors deny verdicts.
    """
    mode = os.environ.get("PM_CODEX_PRETOOL_MODE", "advisory").strip().lower()
    if mode in ("deny", "pretool", "pre-tool", "hook"):
        return {
            "tier": "T2",
            "discover": "mcp_or_rest",
            "interrupt": "tool_boundary",
            "deny": "adapter_cli_pre_tool",
            "kill": "runner",
            "verified": True,
        }
    return {
        "tier": "T1",
        "discover": "mcp_or_rest",
        "interrupt": "advisory_poll",
        "deny": "not_verified",
        "kill": "runner",
        "verified": False,
    }


def _agreement_text(agreement):
    if isinstance(agreement, dict):
        return agreement.get("text") or json.dumps(agreement, indent=2, sort_keys=True)
    if agreement:
        return str(agreement)
    return "(working agreement unavailable; fail-open)"


def drain_inbox(agent_id):
    """Return unacked messages for this agent. Read-only; the model/adapter acks after acting."""
    try:
        q = urllib.parse.quote(agent_id, safe="")
        r = sb._http("GET", f"/ixp/v1/inbox?project={PROJECT}&to_agent={q}&unacked=true")
        return r.get("messages") or []
    except Exception:
        return []


def _inbox_context(messages):
    if not messages:
        return "No unacked Switchboard messages were visible at session start."
    lines = ["Unacked Switchboard messages at session start:"]
    for m in messages:
        bits = [f"#{m.get('id')}", f"from {m.get('from_agent') or '?'}"]
        if m.get("task_id"):
            bits.append(f"task {m['task_id']}")
        if m.get("signal"):
            bits.append(f"signal {m['signal']}")
        lines.append(f"- {'; '.join(bits)}: {m.get('message') or ''}")
    return "\n".join(lines)


def on_session_start(cwd=None):
    """Call when a Codex session begins."""
    me = codex_agent_id(cwd)
    control = control_fidelity()
    agreement = sb.handshake(PROJECT, me, RUNTIME, lane=os.environ.get("PM_LANE", ""),
                             model=os.environ.get("PM_AGENT_MODEL", ""), control=control)
    inbox = drain_inbox(me)
    text = _agreement_text(agreement)
    context = (
        f"## Switchboard working agreement - project '{PROJECT}'\n\n"
        f"Registered as `{me}` with control fidelity `{control['tier']}` "
        f"(deny={control['deny']}, interrupt={control['interrupt']}).\n\n"
        f"{_inbox_context(inbox)}\n\n"
        f"{text}\n\n"
        f"Codex adapter note: if `PM_CODEX_PRETOOL_MODE` is not `deny`, this session is advisory "
        f"only for per-tool enforcement. A runner can still stop the process out-of-band."
    )
    return {
        "event": "session_start",
        "project": PROJECT,
        "agent_id": me,
        "runtime": RUNTIME,
        "control": control,
        "unacked_messages": inbox,
        "additional_context": context,
    }


def normalize_pending(pending):
    """Normalize likely Codex/runner hook payload shapes to the shared-core tuple."""
    pending = pending or {}
    call = pending.get("tool_call") or pending.get("toolCall") or pending.get("call") or {}
    tool_name = (
        pending.get("tool_name") or pending.get("name") or pending.get("tool") or
        call.get("tool_name") or call.get("name") or call.get("tool") or ""
    )
    tool_input = (
        pending.get("tool_input") or pending.get("input") or pending.get("arguments") or
        call.get("tool_input") or call.get("input") or call.get("arguments") or {}
    )
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except Exception:
            tool_input = {"raw": tool_input}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    cwd = pending.get("cwd") or call.get("cwd") or os.getcwd()
    return tool_name, tool_input, cwd


def on_pre_tool(pending):
    """Call before each tool the model wants to run.

    `pending` is whatever your runtime hands a pre-tool hook. Normalize it to (tool_name,
    tool_input, cwd), ask the shared core, then map the decision to your deny mechanism.
    """
    tool_name, tool_input, cwd = normalize_pending(pending)

    me = codex_agent_id(cwd)
    verdict = sb.evaluate_tool(PROJECT, me, tool_name, tool_input, cwd=cwd)
    verdict.update({
        "event": "pre_tool",
        "project": PROJECT,
        "agent_id": me,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "control": control_fidelity(),
    })
    return verdict


def _read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return {"_parse_error": str(e)}


def _emit_json(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Switchboard Codex adapter harness")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("session-start", help="register and print first-turn context JSON")
    sub.add_parser("pre-tool", help="read pending tool JSON on stdin and print allow/deny verdict")
    sub.add_parser("fidelity", help="print advertised control fidelity")
    smoke = sub.add_parser("smoke", help="run local normalization/deny smoke")
    smoke.add_argument("--skip-session", action="store_true",
                       help="do not call the live session-start handshake")
    parser.add_argument("--deny-exit-code", type=int, default=0,
                        help="exit with this code on deny; default keeps JSON-only behavior")
    args = parser.parse_args(argv)

    if args.command == "session-start":
        _emit_json(on_session_start())
        return 0
    if args.command == "pre-tool":
        verdict = on_pre_tool(_read_stdin_json())
        _emit_json(verdict)
        return args.deny_exit_code if verdict.get("decision") == "deny" else 0
    if args.command == "fidelity":
        _emit_json(control_fidelity())
        return 0

    sample = {"toolCall": {"name": "mcp__taikun_plan__update_task",
                           "arguments": {"status": "Done"}},
              "cwd": os.getcwd()}
    if args.command == "smoke" and not args.skip_session:
        _emit_json(on_session_start())
    _emit_json(on_pre_tool(sample))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
