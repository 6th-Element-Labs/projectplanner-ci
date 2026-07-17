#!/usr/bin/env python3
"""CO-13: bound-session chat inject into live Codex PTY (wrong-session refused)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
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


tmp = Path(tempfile.mkdtemp(prefix="co13-inject-"))
os.environ["PM_RUNNER_DIR"] = str(tmp / "runner")
os.environ["PM_RUNNER_USE_PTY"] = "1"
os.environ["PM_RUNNER_STREAM_SECRET"] = "co13-test-stream-secret"
os.environ["PM_HOST_ID"] = "host/co13-dedicated-codex"
os.environ["PM_RUNNER_STREAM_BIND"] = "127.0.0.1"

# Ticket crypto for inject scope
ticket, expires = pty_stream.mint_inject_ticket(
    runner_session_id="run_test", task_id="CO-13",
    host_id="host/co13-dedicated-codex", ttl_seconds=60)
ok_ticket, reason = pty_stream.verify_inject_ticket(
    ticket, runner_session_id="run_test", task_id="CO-13",
    host_id="host/co13-dedicated-codex")
ok(ok_ticket and expires > time.time(), "signed inject ticket mints and verifies")
bad_sess, bad_sess_reason = pty_stream.verify_inject_ticket(
    ticket, runner_session_id="run_other", task_id="CO-13",
    host_id="host/co13-dedicated-codex")
ok(not bad_sess and bad_sess_reason == "session_mismatch",
   "inject ticket rejects wrong runner_session_id")
bad_task, bad_task_reason = pty_stream.verify_inject_ticket(
    ticket, runner_session_id="run_test", task_id="OTHER",
    host_id="host/co13-dedicated-codex")
ok(not bad_task and bad_task_reason == "task_mismatch",
   "inject ticket rejects wrong task_id")
stream_ok, stream_reason = pty_stream.verify_ticket(
    ticket, runner_session_id="run_test", host_id="host/co13-dedicated-codex")
ok(not stream_ok and stream_reason == "wrong_scope",
   "stream verify rejects inject-scoped ticket")

payload = pty_stream.format_inject_payload("continue", kind="approve")
ok(payload.startswith(b"[Switchboard Approve] continue") and payload.endswith(b"\n"),
   "approve shortcut formats a prefixed inject payload")

# Live PTY: child echoes stdin lines mid-flight
child = [
    sys.executable, "-c",
    "import sys,time\n"
    "sys.stdout.write('PTY-READY\\n')\n"
    "sys.stdout.flush()\n"
    "deadline=time.time()+20\n"
    "while time.time()<deadline:\n"
    "    line=sys.stdin.readline()\n"
    "    if not line:\n"
    "        time.sleep(0.05)\n"
    "        continue\n"
    "    sys.stdout.write('ECHO:' + line)\n"
    "    sys.stdout.flush()\n"
    "    if 'DONE' in line:\n"
    "        break\n",
]
meta = supervisor.start_session(
    child, agent_id="cursor/CO-13-test", task_id="CO-13", claim_id="taskclaim-co13",
    cwd=str(ROOT), runner_dir=os.environ["PM_RUNNER_DIR"],
)
ok(meta.get("pty") is True
   and (meta.get("control") or {}).get("runner_inject") is True
   and int(meta.get("stream_port") or 0) > 0
   and meta.get("alive") is True,
   "supervisor launches PTY child with runner_inject + stream_port")

deadline = time.time() + 5
log_text = ""
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "PTY-READY" in log_text:
        break
    time.sleep(0.05)
ok("PTY-READY" in log_text, "child boots and streamer dual-writes ready marker")

# Happy-path mid-flight inject
injected = agent_host.supervisor_action("inject", meta["runner_session_id"], {
    "task_id": "CO-13",
    "text": "hello-from-mission",
    "kind": "freeform",
})
ok(injected.get("injected") is True
   and injected.get("task_id") == "CO-13"
   and int(injected.get("bytes_written") or 0) > 0,
   "runner_inject writes into live PTY for matching task_id")

deadline = time.time() + 5
saw_echo = False
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "ECHO:hello-from-mission" in log_text:
        saw_echo = True
        break
    time.sleep(0.05)
ok(saw_echo, "chat from Mission panel appears mid-flight in live Codex session")

# Shortcut inject
injected_hold = agent_host.supervisor_action("inject", meta["runner_session_id"], {
    "task_id": "CO-13",
    "text": "pause here",
    "kind": "hold",
})
ok(injected_hold.get("injected") is True, "Hold shortcut injects into bound session")

deadline = time.time() + 5
saw_hold = False
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "ECHO:[Switchboard Hold] pause here" in log_text:
        saw_hold = True
        break
    time.sleep(0.05)
ok(saw_hold, "Hold shortcut text appears in the live session")

# Wrong-session inject refused at host
wrong = agent_host.supervisor_action("inject", meta["runner_session_id"], {
    "task_id": "CO-12",
    "text": "should-not-land",
    "kind": "freeform",
})
ok(wrong.get("error") == "wrong_session" and wrong.get("reason") == "task_mismatch",
   "wrong-session inject refused at host (task_id mismatch)")

# Control-plane authz
store.init_project_registry()
store.init_db("switchboard")
now = time.time()
task = store.create_task({"workstream_id": "CO", "title": "co13 inject"},
                         actor="test", project="switchboard")
# Bind registry session to the live local runner id but keep task_id as the
# store-created task id so inject options must match that bind.
session = store.upsert_runner_session({
    "runner_session_id": meta["runner_session_id"],
    "host_id": "host/co13-dedicated-codex",
    "agent_id": "cursor/CO-13-test",
    "runtime": "codex",
    "task_id": task["task_id"],
    "claim_id": "taskclaim-co13",
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
        "wake_id": "wake-co13",
        "work_session_id": "worksession-co13",
    },
}, actor="test", project="switchboard")
ok("inject" in set(session["available_actions"])
   and session["environment"]["capabilities"]["inject"] == "supported",
   "runner_session with runner_inject advertises inject capability")

refused = store.request_runner_control(
    meta["runner_session_id"], "inject",
    options={"task_id": "WRONG-TASK", "text": "nope"},
    actor="test", project="switchboard")
ok(refused.get("requested") is False
   and (
       refused.get("error") == "wrong_session"
       or refused.get("error_code") == "wrong_session"
       or refused.get("reason") == "task_mismatch"
   ),
   "control plane refuses inject when task_id does not match bound session")

# Direct companion HTTP path with wrong task still 401/403 even with ticket forged for other task
inj_url = pty_stream.build_inject_url(
    bind_host=meta["stream_bind"], port=int(meta["stream_port"]),
    runner_session_id=meta["runner_session_id"])
bad_ticket, _ = pty_stream.mint_inject_ticket(
    runner_session_id=meta["runner_session_id"], task_id="OTHER",
    host_id=meta.get("host_id") or "")
req = urllib.request.Request(
    inj_url,
    data=json.dumps({
        "ticket": bad_ticket,
        "task_id": "OTHER",
        "text": "evil",
        "kind": "freeform",
    }).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
http_code = None
try:
    urllib.request.urlopen(req, timeout=5)
except urllib.error.HTTPError as exc:
    http_code = exc.code
    body = json.loads(exc.read().decode() or "{}")
    ok(http_code in {401, 403}
       and body.get("reason") in {"task_mismatch", "session_mismatch"},
       "companion refuses wrong-task inject over HTTP")
except Exception as exc:  # noqa: BLE001
    ok(False, f"companion wrong-task inject unexpected error: {exc}")
else:
    ok(False, "companion should refuse wrong-task inject")

# Finish child cleanly
agent_host.supervisor_action("inject", meta["runner_session_id"], {
    "task_id": "CO-13",
    "text": "DONE",
    "kind": "freeform",
})
killed = supervisor.kill_session(meta["runner_session_id"], runner_dir=os.environ["PM_RUNNER_DIR"])
ok(killed.get("status") == "killed" and not killed.get("alive"),
   "kill terminates PTY child (and streamer) cleanly")

# UI needles (Mission panel session chat)
from scripts.frontend_test_source import read_frontend_source
app_js = read_frontend_source(str(ROOT))
# UI-24: the chat composer moved from a runnerControlHtml() JS template into
# static/index.html as a persistent global panel (one terminal, reparented
# between the sidecar and the Dev tab, instead of re-rendered per task) — so
# its label now lives in the HTML shell, not the composed JS.
index_html = (Path(ROOT) / "static" / "index.html").read_text(encoding="utf-8")
ok("request_runner_inject" in app_js
   and "_runnerPtySendChat" in app_js  # UI-24: renamed from sendRunnerSessionChat
   and "data-runner-chat-kind" in app_js
   and "Session chat (bound Codex PTY" in index_html
   and "SwitchboardRunnerSession" in app_js,
   "Mission panel exposes session chat inject (not inbox-as-chat)")

print(f"\nCO-13 PTY inject: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
