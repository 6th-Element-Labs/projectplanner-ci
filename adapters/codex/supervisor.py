#!/usr/bin/env python3
"""Small managed-process supervisor for Codex Switchboard sessions.

This is the concrete T3 runner-kill half: Switchboard can only promise hard-stop control for
processes it launched or that registered a stable runner_session_id. The supervisor persists a
session record, injects PM_RUNNER_SESSION_ID/PM_AGENT_ID into the child environment, and can
terminate the child process group with a pre-kill snapshot.

CO-12/CO-13: local sessions launch under a real PTY by default. A companion pty_stream process
holds the master fd, dual-writes stdout.log, serves authenticated HTTP chunked streams, and
accepts bound-session POST injects for Mission panel chat.
"""
import argparse
import json
import os
import pty
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_RUNNER_DIR = Path(os.environ.get("PM_RUNNER_DIR", ".switchboard/runner")).resolve()
PTY_STREAM = Path(__file__).resolve().with_name("pty_stream.py")


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _now():
    return time.time()


def _runner_dir(runner_dir=None):
    root = Path(runner_dir or DEFAULT_RUNNER_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_dir(runner_session_id, runner_dir=None):
    return _runner_dir(runner_dir) / runner_session_id


def _meta_path(runner_session_id, runner_dir=None):
    return _session_dir(runner_session_id, runner_dir) / "session.json"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_meta(runner_session_id, runner_dir=None):
    return json.loads(_meta_path(runner_session_id, runner_dir).read_text(encoding="utf-8"))


def _pid_running(pid):
    if not pid:
        return False
    try:
        finished, _status = os.waitpid(int(pid), os.WNOHANG)
        if finished == int(pid):
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _git(args, cwd):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _tail(path, limit=4000):
    try:
        data = Path(path).read_bytes()
        return data[-limit:].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _snapshot(meta):
    cwd = meta.get("cwd") or os.getcwd()
    return {
        "captured_at": _now(),
        "runner_session_id": meta.get("runner_session_id"),
        "agent_id": meta.get("agent_id"),
        "task_id": meta.get("task_id"),
        "claim_id": meta.get("claim_id"),
        "pid": meta.get("pid"),
        "cwd": cwd,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
        "head_sha": _git(["rev-parse", "HEAD"], cwd),
        "log_tail": _tail(meta.get("log_path", "")),
    }


def _await_stream_ready(ready_path: Path, timeout_s: float | None = None) -> dict:
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get(
                "PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS", "15") or 15)
        except (TypeError, ValueError):
            timeout_s = 15.0
    deadline = time.time() + max(0.5, float(timeout_s))
    while time.time() < deadline:
        if ready_path.exists():
            try:
                return json.loads(ready_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.05)
    return {}


def start_session(command, agent_id, task_id="", claim_id="", cwd=None, runner_dir=None,
                  runner_session_id="", extra_env=None, use_pty=None):
    if not command:
        raise ValueError("command required")
    runner_session_id = runner_session_id or "run_" + uuid.uuid4().hex[:16]
    root = _session_dir(runner_session_id, runner_dir)
    if root.exists():
        raise ValueError(f"runner session already exists: {runner_session_id}")
    root.mkdir(parents=True)
    log_path = root / "stdout.log"
    env = os.environ.copy()
    env.update(extra_env or {})
    env.update({
        "PM_RUNNER_SESSION_ID": runner_session_id,
        "PM_AGENT_ID": agent_id,
    })
    if task_id:
        env["PM_TASK_ID"] = task_id
    if claim_id:
        env["PM_CLAIM_ID"] = claim_id
    if use_pty is None:
        use_pty = _truthy(os.environ.get("PM_RUNNER_USE_PTY", "1"))
    streamer_pid = None
    stream_bind = None
    stream_port = None
    ready_path = root / "stream_ready.json"
    host_id = str(env.get("PM_HOST_ID") or env.get("PM_CO_HOST_ID") or "")
    if use_pty:
        master_fd, slave_fd = pty.openpty()
        env.setdefault("TERM", os.environ.get("TERM") or "xterm-256color")
        proc = None
        streamer = None
        stream_log = None
        stream_error_path = root / "pty_stream.stderr.log"
        try:
            # Make Watch/Chat ready before the worker can emit output or exit. Starting
            # the child first created a real race: a fast authorization failure could
            # close the slave PTY while the companion was still importing, so the
            # supervisor deleted the only log and mislabeled the launch as no-PTY.
            stream_log = stream_error_path.open("ab")
            streamer = subprocess.Popen(
                [
                    sys.executable,
                    str(PTY_STREAM),
                    "--runner-session-id", runner_session_id,
                    "--log-path", str(log_path),
                    "--master-fd", str(master_fd),
                    "--host-id", host_id,
                    "--task-id", str(task_id or ""),
                    "--bind-host", os.environ.get("PM_RUNNER_STREAM_BIND", "127.0.0.1"),
                    "--port", str(int(os.environ.get("PM_RUNNER_STREAM_PORT", "0") or 0)),
                    "--ready-path", str(ready_path),
                ],
                pass_fds=(master_fd,),
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stream_log,
            )
            stream_log.close()
            stream_log = None
            streamer_pid = streamer.pid
            ready = _await_stream_ready(ready_path)
            stream_bind = ready.get("bind_host") or os.environ.get("PM_RUNNER_STREAM_BIND", "127.0.0.1")
            stream_port = ready.get("port")
            if not stream_port:
                companion_error = _tail(stream_error_path).strip()
                companion_status = streamer.poll()
                detail = companion_error or (
                    f"companion exit={companion_status}" if companion_status is not None
                    else "companion produced no ready receipt"
                )
                raise RuntimeError(
                    f"pty_stream companion failed to become ready: {detail}")

            proc = subprocess.Popen(
                command,
                cwd=cwd or os.getcwd(),
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            os.close(master_fd)
            master_fd = -1
        except Exception:
            if stream_log is not None:
                stream_log.close()
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if master_fd >= 0:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if streamer is not None and streamer.poll() is None:
                try:
                    os.killpg(streamer.pid, signal.SIGKILL)
                except Exception:
                    try:
                        streamer.kill()
                    except Exception:
                        pass
            # Preserve the bounded failure directory and companion stderr for the
            # operator. It has no session.json and therefore is never counted as live.
            raise
        control = {
            "tier": "T3",
            "runner_kill": True,
            "managed_process": True,
            "runner_open": True,
            "runner_inject": True,
            "runner_logs": True,
        }
    else:
        log = log_path.open("ab")
        proc = subprocess.Popen(
            command,
            cwd=cwd or os.getcwd(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        control = {"tier": "T3", "runner_kill": True, "managed_process": True}
    child_alive = proc.poll() is None
    meta = {
        "runner_session_id": runner_session_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "claim_id": claim_id,
        "pid": proc.pid,
        "pgid": proc.pid,
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "command": command,
        "log_path": str(log_path),
        "status": "running" if child_alive else "exited",
        "started_at": _now(),
        "control": control,
        "pty": bool(use_pty),
        "streamer_pid": streamer_pid,
        "stream_bind": stream_bind,
        "stream_port": stream_port,
        "host_id": host_id,
    }
    _write_json(_meta_path(runner_session_id, runner_dir), meta)
    if not child_alive:
        meta["exited_at"] = _now()
        _write_json(_meta_path(runner_session_id, runner_dir), meta)
    return {**meta, "alive": child_alive}


def status_session(runner_session_id, runner_dir=None):
    meta = _read_meta(runner_session_id, runner_dir)
    alive = _pid_running(meta.get("pid")) if meta.get("status") == "running" else False
    if meta.get("status") == "running" and not alive:
        meta["status"] = "exited"
        meta["exited_at"] = _now()
        _write_json(_meta_path(runner_session_id, runner_dir), meta)
    return {**meta, "alive": alive}


def snapshot_session(runner_session_id, runner_dir=None):
    meta = _read_meta(runner_session_id, runner_dir)
    snap = _snapshot(meta)
    meta["last_snapshot"] = snap
    meta["snapshot_at"] = snap["captured_at"]
    _write_json(_meta_path(runner_session_id, runner_dir), meta)
    return {**status_session(runner_session_id, runner_dir), "last_snapshot": snap}


def _stop_pid(pid, grace_seconds=5.0, signal_name="TERM"):
    sent = None
    if not _pid_running(pid):
        return sent
    sig = signal.SIGTERM if signal_name.upper() in ("TERM", "SIGTERM") else signal.SIGINT
    try:
        os.killpg(int(pid), sig)
        sent = sig.name
    except ProcessLookupError:
        return sent
    except PermissionError:
        try:
            os.kill(int(pid), sig)
            sent = sig.name
        except ProcessLookupError:
            return sent
    deadline = time.time() + max(0.0, float(grace_seconds))
    while time.time() < deadline and _pid_running(pid):
        time.sleep(0.05)
    if _pid_running(pid):
        try:
            os.killpg(int(pid), signal.SIGKILL)
            sent = "SIGKILL"
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(int(pid), signal.SIGKILL)
                sent = "SIGKILL"
            except ProcessLookupError:
                pass
    return sent


def kill_session(runner_session_id, runner_dir=None, grace_seconds=5.0, signal_name="TERM"):
    meta = _read_meta(runner_session_id, runner_dir)
    snap = _snapshot(meta)
    meta["last_snapshot"] = snap
    meta["stop_requested_at"] = _now()
    pid = int(meta.get("pid") or 0)
    streamer_pid = int(meta.get("streamer_pid") or 0)
    sent = _stop_pid(pid, grace_seconds=grace_seconds, signal_name=signal_name)
    if streamer_pid and streamer_pid != pid:
        _stop_pid(streamer_pid, grace_seconds=min(2.0, float(grace_seconds)), signal_name="TERM")
    meta["status"] = "killed"
    meta["killed_at"] = _now()
    meta["last_signal"] = sent
    _write_json(_meta_path(runner_session_id, runner_dir), meta)
    return {**meta, "alive": _pid_running(pid)}


def list_sessions(runner_dir=None):
    out = []
    for path in sorted(_runner_dir(runner_dir).glob("run_*/session.json")):
        try:
            out.append(status_session(path.parent.name, runner_dir))
        except Exception:
            pass
    return out


def _emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex Switchboard managed process supervisor")
    parser.add_argument("--runner-dir", default=str(DEFAULT_RUNNER_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start a managed child process")
    start.add_argument("--agent-id", required=True)
    start.add_argument("--task-id", default="")
    start.add_argument("--claim-id", default="")
    start.add_argument("--cwd", default=os.getcwd())
    start.add_argument("--runner-session-id", default="")
    start.add_argument("child", nargs=argparse.REMAINDER,
                       help="child command after --, e.g. -- python3 worker.py")

    status = sub.add_parser("status", help="inspect a managed session")
    status.add_argument("runner_session_id")

    snapshot = sub.add_parser("snapshot", help="capture a managed session snapshot")
    snapshot.add_argument("runner_session_id")

    kill = sub.add_parser("kill", help="terminate a managed session")
    kill.add_argument("runner_session_id")
    kill.add_argument("--grace-seconds", type=float, default=5.0)
    kill.add_argument("--signal", default="TERM")

    sub.add_parser("list", help="list known sessions")
    args = parser.parse_args(argv)

    if args.command == "start":
        child = args.child[1:] if args.child[:1] == ["--"] else args.child
        _emit(start_session(child, args.agent_id, args.task_id, args.claim_id, args.cwd,
                            args.runner_dir, args.runner_session_id))
    elif args.command == "status":
        _emit(status_session(args.runner_session_id, args.runner_dir))
    elif args.command == "snapshot":
        _emit(snapshot_session(args.runner_session_id, args.runner_dir))
    elif args.command == "kill":
        _emit(kill_session(args.runner_session_id, args.runner_dir, args.grace_seconds,
                           args.signal))
    elif args.command == "list":
        _emit({"sessions": list_sessions(args.runner_dir)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
