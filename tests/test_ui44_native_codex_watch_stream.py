#!/usr/bin/env python3
"""UI-44: native Codex output reaches Watch before the child exits."""
from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import threading
import time

from path_setup import ROOT  # noqa: F401

from adapters import codex_local_worker  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


class ObservedBytes(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.first_chunk = threading.Event()
        self.lock = threading.Lock()

    def write(self, value):
        with self.lock:
            written = super().write(value)
        if b"native-codex-started" in value:
            self.first_chunk.set()
        return written

    def snapshot(self):
        with self.lock:
            return self.getvalue()


stream = ObservedBytes()
result = {}
error = []
script = (
    "import time; "
    "print('native-codex-started', flush=True); "
    "time.sleep(1.0); "
    "print('native-codex-finished', flush=True)"
)


def run_child():
    try:
        result["completed"] = codex_local_worker._run_streaming_command(
            [sys.executable, "-c", script],
            cwd=tempfile.gettempdir(),
            env={},
            timeout=10,
            stream=stream,
        )
    except Exception as exc:  # pragma: no cover - reported by assertion below
        error.append(exc)


thread = threading.Thread(target=run_child)
thread.start()
saw_while_running = stream.first_chunk.wait(3) and thread.is_alive()
ok(saw_while_running, "Watch receives native Codex output while the child is still running")
thread.join(5)
ok(not thread.is_alive() and not error, "streaming child exits cleanly without a stuck pump")

expected = b"native-codex-started\nnative-codex-finished\n"
completed = result.get("completed")
captured = (completed.stdout or "").encode() if completed else b""
ok(stream.snapshot() == expected and captured == expected,
   "Watch bytes and retained completion-evidence bytes are identical")
ok(hashlib.sha256(captured).hexdigest() == hashlib.sha256(expected).hexdigest(),
   "native output remains exact for output_sha256 evidence")

started = time.monotonic()
try:
    codex_local_worker._run_streaming_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tempfile.gettempdir(), env={}, timeout=0.2,
        stream=io.BytesIO(),
    )
    timed_out = False
except Exception as exc:
    timed_out = exc.__class__.__name__ == "TimeoutExpired"
ok(timed_out and time.monotonic() - started < 5,
   "timed-out native Codex children are killed promptly")

print(f"\nUI-44 native Watch stream: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
