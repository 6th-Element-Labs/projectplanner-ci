#!/usr/bin/env python3
"""WATCH-11: the Switchboard relay is the only browser PTY transport."""
from pathlib import Path
import sys

from path_setup import ROOT

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


host = (ROOT / "adapters" / "agent_host.py").read_text(encoding="utf-8")
supervisor = (ROOT / "adapters" / "codex" / "supervisor.py").read_text(encoding="utf-8")
stream = (ROOT / "adapters" / "codex" / "pty_stream.py").read_text(encoding="utf-8")
coord = (ROOT / "src" / "switchboard" / "storage" / "repositories" /
         "coordination.py").read_text(encoding="utf-8")
runner = (ROOT / "src" / "switchboard" / "storage" / "repositories" /
          "runner.py").read_text(encoding="utf-8")

open_path = host[host.index('elif action == "open":'):host.index('elif action == "inject":')]
ok("http_chunked" not in open_path and "build_stream_url" not in open_path,
   "runner_open contains no legacy browser transport or URL builder")
ok("local_stream_url" not in open_path and "legacy_streamer" not in open_path,
   "runner_open contains no compatibility fallback branch")
ok('"stream_bind": rec.get' not in host and '"stream_port": rec.get' not in host,
   "runner registration does not advertise host-local stream coordinates")
ok('"stream_bind": stream_bind' not in supervisor and
   '"stream_port": stream_port' not in supervisor,
   "supervisor receipts do not publish stream coordinates")
ok("build_stream_url" not in stream,
   "the retired local stream URL builder is deleted")
ok("stream_bind" not in coord and "stream_port" not in coord and
   "stream_bind" not in runner and "stream_port" not in runner,
   "watchability and binding predicates do not require stream coordinates")
ok(not (ROOT / "adapters" / "codex" / "pty_relay_bridge.py").exists(),
   "the obsolete localhost relay bridge is deleted")

print(f"\nWATCH-11 single browser transport: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
