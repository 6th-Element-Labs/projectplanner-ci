#!/usr/bin/env python3
"""Self-contained tests for the HARDEN-4 unattended proof helpers."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("switchboard_core_test", ROOT / "adapters" / "switchboard_core.py")
proof_work = _load("proof_work_test", ROOT / "adapters" / "proof_work.py")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


calls = []
sb.handshake = lambda *a, **k: calls.append(("handshake", a, k)) or {"ok": True}
sb.inbox = lambda *a, **k: calls.append(("inbox", a, k)) or [{"id": 7, "message": "hello"}]
sb.heartbeat = lambda *a, **k: calls.append(("heartbeat", a, k)) or None
sb.claim_next = lambda *a, **k: calls.append(("claim_next", a, k)) or {
    "claimed": False,
    "reason": "no_unblocked_work",
}

res = sb.run_session("switchboard", "codex/proof", "codex", lambda task: {},
                     lanes="PROOF", max_tasks=1)
ok([c[0] for c in calls[:4]] == ["handshake", "inbox", "heartbeat", "claim_next"],
   "run_session reads inbox before claim_next")
ok(res["startup_inbox"][0]["id"] == 7,
   "run_session returns startup inbox evidence")


with tempfile.TemporaryDirectory() as tmp:
    old_env = dict(os.environ)
    os.environ.update({
        "PM_PROOF_WORK_ROOT": tmp,
        "PM_PROOF_REPO_URL": "https://example.invalid/projectplanner.git",
        "PM_PROOF_NOW": "2026-06-29T04-00-00Z",
        "PM_AGENT_ID": "codex/PROOF-1-live",
        "PM_RUNNER_SESSION_ID": "run_test",
    })
    cmd_log = []

    def fake_run(args, cwd=None, check=True):
        cmd_log.append((list(args), str(cwd) if cwd else ""))
        if args[0:2] == ["git", "clone"]:
            Path(args[3]).mkdir(parents=True, exist_ok=True)
            return ""
        if args[0:2] == ["git", "rev-parse"]:
            return "abc123"
        if args[0:3] == ["gh", "pr", "create"]:
            return "https://github.com/6th-Element-Labs/projectplanner/pull/99"
        return ""

    proof_work._run = fake_run
    evidence = proof_work.run_task({
        "task_id": "PROOF-1",
        "task": {"task_id": "PROOF-1", "title": "Proof payload"},
    })
    proof_path = Path(tmp) / "proof-1" / evidence["proof_file"]
    ok(evidence["branch"] == "codex/PROOF-1-unattended-proof",
       "proof worker uses task-scoped branch")
    ok(evidence["head_sha"] == "abc123" and evidence["pr_number"] == 99,
       "proof worker returns PR evidence")
    ok(proof_path.exists() and "run_test" in proof_path.read_text(encoding="utf-8"),
       "proof worker writes runner evidence into docs artifact")
    ok(any(cmd[0][:3] == ["git", "push", "-u"] for cmd in cmd_log),
       "proof worker pushes the branch")
    os.environ.clear()
    os.environ.update(old_env)


print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
