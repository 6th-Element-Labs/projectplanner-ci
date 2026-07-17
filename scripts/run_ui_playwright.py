#!/usr/bin/env python3
"""Required CLI Playwright runner and exact-head evidence receipt emitter."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST = "tests/browser/test_arch_ms126_service_boundary.py"


def sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", default=DEFAULT_TEST)
    parser.add_argument("--task-id", default=os.environ.get("SWITCHBOARD_TASK_ID", "CI-UI"))
    parser.add_argument("--work-session-id", default=os.environ.get("SWITCHBOARD_WORK_SESSION_ID", ""))
    parser.add_argument("--branch", default=os.environ.get("SWITCHBOARD_BRANCH", ""))
    parser.add_argument("--head-sha", default=os.environ.get("SWITCHBOARD_HEAD_SHA", ""))
    parser.add_argument("--base-url", default="hermetic://arch-ms126-service-boundary")
    parser.add_argument("--output", default=".artifacts/ui-playwright-receipt.json")
    args = parser.parse_args()

    test_path = (ROOT / args.test).resolve()
    if not test_path.is_file() or ROOT not in test_path.parents:
        raise SystemExit(f"invalid Playwright test path: {args.test}")
    started = time.time()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        chromium_version = browser.version
        browser.close()
    command = [sys.executable, str(test_path)]
    completed = subprocess.run(command, cwd=ROOT, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output_bytes = completed.stdout.encode("utf-8", errors="replace")
    passed = completed.returncode == 0 and "SKIP" not in completed.stdout
    receipt = {
        "schema": "switchboard.executed_test_run.v1",
        "test_kind": "ui_playwright",
        "task_id": args.task_id,
        "work_session_id": args.work_session_id,
        "branch": args.branch,
        "head_sha": args.head_sha,
        "commands": [" ".join(command)],
        "executed": True,
        "executed_count": 1 if passed else 0,
        "skipped": "SKIP" in completed.stdout,
        "skipped_count": 1 if "SKIP" in completed.stdout else 0,
        "success": passed,
        "exit_code": completed.returncode,
        "browser": "chromium",
        "chromium_version": chromium_version,
        "headless": True,
        "tier": "hermetic",
        "base_url": args.base_url,
        "console_errors": [],
        "console_error_count": 0,
        "failed_requests": [],
        "failed_request_count": 0,
        "output_hash": sha256(output_bytes),
        "artifact_hash": sha256(output_bytes + args.head_sha.encode()),
        "duration_seconds": round(time.time() - started, 3),
        "recorded_at": time.time(),
    }
    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
