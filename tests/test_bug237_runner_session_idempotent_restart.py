"""BUG-237: a failed launch must never permanently poison its runner_session_id.

runner_session_id is derived deterministically from the wake, so every retry
of the same dispatch reuses the same id. The supervisor used to hard-raise
"runner session already exists" whenever the session directory existed — even
for dead debris a crashed launch deliberately preserves for forensics — so one
failed launch made the dispatch loop unable to converge (prod 2026-07-29:
run_5982f585caee8f33, 144 ledger echoes before the box wedge).

Contract under test (BUG-201 semantics):
- live session, same agent  -> idempotent success, no second child spawned
- live session, other agent -> still raises (identity fencing stays fail-closed)
- dead / meta-less debris   -> archived aside, fresh start succeeds
- archived debris never appears in list_sessions
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from adapters.codex import supervisor


_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_pid_gone(pid, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not supervisor._pid_running(pid):
            return True
        time.sleep(0.05)
    return False


class Bug237IdempotentRunnerSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bug237-runner-")
        self.runner_dir = str(Path(self._tmp.name) / "runner")
        self._live_sessions = []

    def tearDown(self):
        for session_id in self._live_sessions:
            try:
                supervisor.kill_session(session_id, self.runner_dir, grace_seconds=0.5)
            except Exception:
                pass
        self._tmp.cleanup()

    def _start(self, session_id, agent_id="agent/test", task_id="BUG-237"):
        result = supervisor.start_session(
            list(_SLEEPER), agent_id, task_id=task_id, cwd=self._tmp.name,
            runner_dir=self.runner_dir, runner_session_id=session_id,
            use_pty=False)
        self._live_sessions.append(session_id)
        return result

    def test_dead_leftover_is_archived_and_start_converges(self):
        first = self._start("run_bug237dead")
        supervisor.kill_session("run_bug237dead", self.runner_dir, grace_seconds=0.5)
        self.assertTrue(_wait_pid_gone(first["pid"]))

        retry = self._start("run_bug237dead")
        self.assertTrue(retry["alive"])
        self.assertNotEqual(first["pid"], retry["pid"])
        stale = [p.name for p in Path(self.runner_dir).iterdir()
                 if p.name != "run_bug237dead"]
        self.assertEqual(len(stale), 1, f"expected one archived dir, saw {stale}")
        self.assertFalse(stale[0].startswith("run_"),
                         f"archived dir {stale[0]} must not match the run_* live glob")

    def test_meta_less_debris_from_crashed_launch_converges(self):
        debris = Path(self.runner_dir) / "run_bug237crash"
        debris.mkdir(parents=True)
        (debris / "pty_stream.stderr.log").write_text("boom", encoding="utf-8")

        result = self._start("run_bug237crash")
        self.assertTrue(result["alive"])
        meta = supervisor._read_meta("run_bug237crash", self.runner_dir)
        self.assertEqual(meta["runner_session_id"], "run_bug237crash")

    def test_live_same_agent_is_idempotent_success(self):
        first = self._start("run_bug237live")
        replay = supervisor.start_session(
            list(_SLEEPER), "agent/test", task_id="BUG-237", cwd=self._tmp.name,
            runner_dir=self.runner_dir, runner_session_id="run_bug237live",
            use_pty=False)
        self.assertTrue(replay["alive"])
        self.assertEqual(first["pid"], replay["pid"], "must not spawn a second child")
        self.assertTrue(replay.get("idempotent_replay"))

    def test_live_other_agent_still_raises(self):
        self._start("run_bug237fence", agent_id="agent/owner")
        with self.assertRaises(ValueError):
            supervisor.start_session(
                list(_SLEEPER), "agent/intruder", cwd=self._tmp.name,
                runner_dir=self.runner_dir, runner_session_id="run_bug237fence",
                use_pty=False)

    def test_list_sessions_ignores_archived_debris(self):
        first = self._start("run_bug237list")
        supervisor.kill_session("run_bug237list", self.runner_dir, grace_seconds=0.5)
        self.assertTrue(_wait_pid_gone(first["pid"]))
        self._start("run_bug237list")

        listed = [s["runner_session_id"] for s in supervisor.list_sessions(self.runner_dir)]
        self.assertEqual(listed, ["run_bug237list"])


if __name__ == "__main__":
    unittest.main()
