#!/usr/bin/env python3
"""Execute test commands and emit Switchboard executed-test evidence JSON."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid


SCHEMA = "switchboard.executed_test_run.v1"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run test command(s) and emit switchboard.executed_test_run.v1 evidence.")
    parser.add_argument("--cwd", default=".", help="Directory where commands should run.")
    parser.add_argument("--work-session-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--claim-id", default="")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--runner", default="work_session_test_run.py")
    parser.add_argument("--command", action="append", default=[],
                        help="Shell command to execute. May be repeated.")
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument("--log-path", default="",
                        help="Optional path to write combined command output.")
    args = parser.parse_args()

    commands = [cmd for cmd in args.command if cmd.strip()]
    if not commands:
        print("At least one --command is required.", file=sys.stderr)
        return 2

    cwd = os.path.abspath(args.cwd)
    started_at = time.time()
    outputs = []
    command_results = []
    exit_code = 0
    for command in commands:
        before = time.time()
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=args.timeout_s,
        )
        after = time.time()
        output = proc.stdout or ""
        outputs.append(f"$ {command}\n{output}")
        command_results.append({
            "command": command,
            "exit_code": proc.returncode,
            "duration_s": round(after - before, 3),
        })
        if proc.returncode != 0:
            exit_code = proc.returncode
            break

    completed_at = time.time()
    combined = "\n".join(outputs)
    output_hash = "sha256:" + hashlib.sha256(combined.encode("utf-8")).hexdigest()
    log_path = args.log_path.strip()
    if log_path:
        log_path = os.path.abspath(log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(combined)

    evidence = {
        "schema": SCHEMA,
        "run_id": "testrun-" + uuid.uuid4().hex[:16],
        "work_session_id": args.work_session_id or None,
        "task_id": args.task_id.upper() or None,
        "claim_id": args.claim_id or None,
        "agent_id": args.agent_id or None,
        "branch": args.branch or None,
        "head_sha": args.head_sha or None,
        "commands": commands,
        "command_results": command_results,
        "cwd": cwd,
        "runner": args.runner,
        "executed": True,
        "exit_code": exit_code,
        "status": "success" if exit_code == 0 else "failed",
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_s": round(completed_at - started_at, 3),
        "output_hash": output_hash,
        "log_path": log_path or None,
    }
    print(json.dumps(evidence, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
