#!/usr/bin/env python3
"""CO-12: PTY launch + signed stream_url + runner_open for dedicated Codex host."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

from path_setup import ROOT

from adapters import agent_host
import store


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pty_stream = _load("codex_pty_stream", ROOT / "adapters" / "codex" / "pty_stream.py")
supervisor = _load("codex_supervisor", ROOT / "adapters" / "codex" / "supervisor.py")


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


tmp = Path(tempfile.mkdtemp(prefix="co12-pty-"))
os.environ["PM_RUNNER_DIR"] = str(tmp / "runner")
os.environ["PM_RUNNER_USE_PTY"] = "1"
os.environ["PM_RUNNER_STREAM_SECRET"] = "co12-test-stream-secret"
os.environ["PM_HOST_ID"] = "host/co12-dedicated-codex"
os.environ["PM_RUNNER_STREAM_BIND"] = "127.0.0.1"

# Ticket crypto
ticket, expires = pty_stream.mint_ticket(
    runner_session_id="run_test", host_id="host/co12-dedicated-codex", ttl_seconds=60)
ok_ticket, reason = pty_stream.verify_ticket(
    ticket, runner_session_id="run_test", host_id="host/co12-dedicated-codex")
ok(ok_ticket and expires > time.time(), "signed stream ticket mints and verifies")
bad, bad_reason = pty_stream.verify_ticket(
    ticket, runner_session_id="run_other", host_id="host/co12-dedicated-codex")
ok(not bad and bad_reason == "session_mismatch", "ticket rejects wrong runner_session_id")
expired, exp_reason = pty_stream.verify_ticket(
    ticket, runner_session_id="run_test", host_id="host/co12-dedicated-codex",
    now=expires + 5)
ok(not expired and exp_reason == "expired", "ticket rejects expired tokens")

# Live PTY session: keep writing so open+curl overlap with live bytes
child = [
    sys.executable, "-c",
    "import os,sys,time\n"
    "sys.stdout.write('PTY=' + str(os.isatty(1)) + '\\n')\n"
    "size = os.get_terminal_size(1)\n"
    "sys.stdout.write(f'SIZE={size.lines}x{size.columns}\\n')\n"
    "sys.stdout.flush()\n"
    "for i in range(40):\n"
    "    sys.stdout.write(f'beat-{i}\\n')\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.1)\n",
]
meta = supervisor.start_session(
    child, agent_id="cursor/CO-12-test", task_id="CO-12", claim_id="taskclaim-co12",
    cwd=str(ROOT), runner_dir=os.environ["PM_RUNNER_DIR"],
)
ok(meta.get("pty") is True
   and (meta.get("control") or {}).get("runner_open") is True
   and int(meta.get("stream_port") or 0) > 0
   and meta.get("alive") is True,
   "supervisor launches PTY child with runner_open + stream_port")

deadline = time.time() + 5
log_text = ""
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "PTY=True" in log_text:
        break
    time.sleep(0.05)
ok("PTY=True" in log_text, "child sees a TTY and streamer dual-writes stdout.log")
ok("SIZE=40x120" in log_text,
   "child receives a usable 40x120 PTY before any browser attaches")
ok(supervisor._initial_pty_size({
       "PM_RUNNER_PTY_ROWS": "bogus",
       "PM_RUNNER_PTY_COLS": "0",
   }) == (40, 120),
   "invalid configured PTY dimensions fail safely to defaults")
ok(supervisor._initial_pty_size({
       "PM_RUNNER_PTY_ROWS": "50",
       "PM_RUNNER_PTY_COLS": "160",
   }) == (50, 160),
   "operator-configured PTY dimensions are honored")

opened = agent_host.supervisor_action("open", meta["runner_session_id"])
ok(opened.get("opened") is True
   and opened.get("transport") == "http_chunked"
   and opened.get("stream_url")
   and "ticket=" in opened["stream_url"],
   "runner_open succeeds and returns signed stream_url")

streamed = b""
err = None
try:
    req = urllib.request.Request(opened["stream_url"], method="GET")
    with urllib.request.urlopen(req, timeout=8) as resp:
        end = time.time() + 6
        while time.time() < end and (b"beat-" not in streamed or b"PTY=True" not in streamed):
            chunk = resp.read(256)
            if chunk:
                streamed += chunk
            else:
                break
except Exception as exc:  # noqa: BLE001
    err = exc
ok(err is None and (b"beat-" in streamed or b"PTY=True" in streamed),
   "curl/urlopen can consume live PTY byte stream over HTTP chunked body")

# Capability advertisement on registry surface
store.init_project_registry()
store.init_db("switchboard")
now = time.time()
task = store.create_task({"workstream_id": "CO", "title": "co12 open"},
                         actor="test", project="switchboard")
session = store.upsert_runner_session({
    "runner_session_id": meta["runner_session_id"],
    "host_id": "host/co12-dedicated-codex",
    "agent_id": "cursor/CO-12-test",
    "runtime": "codex",
    "task_id": task["task_id"],
    "claim_id": "taskclaim-co12",
    "pid": meta["pid"],
    "status": "running",
    "started_at": now,
    "heartbeat_at": now,
    "heartbeat_ttl_s": 60,
    "control": meta["control"],
    "metadata": {
        "log_path": meta["log_path"],
        "pty": True,
        "stream_port": meta["stream_port"],
        "stream_url": opened.get("stream_url"),
        "transport": "http_chunked",
    },
}, actor="test", project="switchboard")
ok("open" in set(session["available_actions"])
   and session["environment"]["capabilities"]["open"] == "supported",
   "runner_session with runner_open advertises open capability")

# Non-PTY / dead session still not_supported
refused = agent_host.supervisor_action("open", "run_missing_session")
ok(refused.get("error") in {"supervisor_failed", "not_supported", "FileNotFoundError", "JSONDecodeError"}
   or "error" in refused,
   "open fails closed when session is missing")

killed = supervisor.kill_session(meta["runner_session_id"], runner_dir=os.environ["PM_RUNNER_DIR"])
ok(killed.get("status") == "killed" and not killed.get("alive"),
   "kill terminates PTY child (and streamer) cleanly")

print(f"\nCO-12 PTY stream: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
