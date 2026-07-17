#!/usr/bin/env python3
"""COORD-29: managed executed tests inherit the active Python environment."""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "switchboard_core_test_env", ROOT / "adapters" / "switchboard_core.py")
sb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sb
spec.loader.exec_module(sb)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


captured = {}
original_run = sb.subprocess.run
original_path = os.environ.get("PATH", "")


def fake_run(argv, **kwargs):
    captured["argv"] = argv
    captured["env"] = kwargs.get("env")
    evidence = {
        "schema": "switchboard.executed_test_run.v1",
        "status": "success",
        "exit_code": 0,
    }
    return type("Completed", (), {"stdout": json.dumps(evidence)})()


try:
    sb.subprocess.run = fake_run
    result = sb.run_executed_tests(
        "/worktree", "worksession-coord29", "COORD-29", "claim-coord29",
        "codex/COORD-29", commands=["scripts/switchboard_ci.sh"])
finally:
    sb.subprocess.run = original_run

runner_path = (captured.get("env") or {}).get("PATH", "")
interpreter_bin = os.path.dirname(os.path.abspath(sys.executable))
ok(runner_path.split(os.pathsep)[0] == interpreter_bin,
   "executed-test runner PATH starts with the active interpreter bin")
ok(runner_path.endswith(original_path),
   "existing PATH is preserved after the interpreter bin")
ok(captured.get("env") is not os.environ,
   "runner receives an isolated environment copy")
ok(captured.get("argv", [None])[0] == sys.executable,
   "executed-test helper still launches with the active interpreter")
ok(result.get("status") == "success",
   "runner evidence parsing is unchanged")

# Personal Agent Hosts require a real OS sandbox, not only credential removal from env.
with tempfile.TemporaryDirectory(prefix="switchboard-test-sandbox-") as root_value:
    root = Path(root_value)
    user_home = root / "user-home"
    workspace = user_home / "state" / "workspaces" / "TASK"
    config_root = user_home / "config"
    state_root = user_home / "state"
    runner_root = state_root / "runner"
    runtime_root = state_root / "provider-runtimes"
    codex_home = state_root / "codex-home"
    source_codex_home = user_home / ".codex"
    for directory in (
            workspace, config_root, runner_root, runtime_root, codex_home,
            source_codex_home):
        directory.mkdir(parents=True, exist_ok=True)
    identity_path = config_root / "identity.json"
    config_path = config_root / "config.json"
    state_path = state_root / "state.json"
    identity_path.write_text('{"host_token":"must-not-cross"}', encoding="utf-8")
    config_path.write_text('{"project":"switchboard"}', encoding="utf-8")
    state_path.write_text('{"status":"installed"}', encoding="utf-8")
    codex_auth_path = codex_home / "auth.json"
    codex_auth_path.write_text(
        '{"tokens":{"access_token":"codex-auth-must-not-cross"}}', encoding="utf-8")
    source_codex_auth_path = source_codex_home / "auth.json"
    source_codex_auth_path.write_text(
        '{"tokens":{"access_token":"source-codex-auth-must-not-cross"}}',
        encoding="utf-8")
    sandbox_env = {
        "PM_PERSONAL_AGENT_HOST_EXECUTION": "1",
        "PM_AGENT_HOST_PLATFORM": "darwin",
        "PM_AGENT_HOST_IDENTITY_PATH": str(identity_path),
        "PM_AGENT_HOST_CONFIG_PATH": str(config_path),
        "PM_AGENT_HOST_STATE_PATH": str(state_path),
        "PM_AGENT_HOST_RUNNER_DIR": str(runner_root),
        "PM_AGENT_HOST_RUNTIME_ROOT": str(runtime_root),
        "PM_AGENT_HOST_CODEX_HOME": str(codex_home),
        "PM_AGENT_HOST_SOURCE_CODEX_HOME": str(source_codex_home),
        "PM_AGENT_HOST_USER_HOME": str(user_home),
        "CODEX_HOME": str(codex_home),
        "PM_MCP_TOKEN": "stable-host-bearer",
        "SWITCHBOARD_TOKEN": "alternate-host-bearer",
    }
    original_env = {key: os.environ.get(key) for key in sandbox_env}
    original_which = sb.shutil.which
    original_run = sb.subprocess.run
    sandbox_capture = {}

    def capture_sandbox(command, **kwargs):
        sandbox_capture["argv"] = command
        sandbox_capture["env"] = kwargs.get("env") or {}
        evidence = {
            "schema": "switchboard.executed_test_run.v1",
            "status": "success",
            "executed": True,
            "exit_code": 0,
        }
        return type("Completed", (), {"stdout": json.dumps(evidence)})()

    try:
        os.environ.update(sandbox_env)
        sb.shutil.which = lambda name: f"/usr/bin/{name}"
        sb.subprocess.run = capture_sandbox
        sandboxed = sb.run_executed_tests(
            str(workspace), "worksession-secure", "TASK-SECURE", "claim-secure",
            "codex/TASK-SECURE", commands=["scripts/switchboard_ci.sh"])
        mac_argv = sandbox_capture.get("argv") or []
        mac_profile = mac_argv[2] if len(mac_argv) > 2 else ""
        child_env = sandbox_capture.get("env") or {}
        ok(sandboxed.get("status") == "success"
           and mac_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
           and "(deny default)" in mac_profile
           and "(allow default)" not in mac_profile
           and "(deny process-info*)" in mac_profile
           and str(config_root.resolve()) in mac_profile
           and str(state_path.resolve()) in mac_profile
           and str(codex_home.resolve()) in mac_profile
           and str(source_codex_home.resolve()) in mac_profile
           and str(user_home.resolve()) in mac_profile
           and str(workspace.resolve()) in mac_profile
           and all(key not in child_env for key in sb._PERSONAL_TEST_HOST_PATH_ENV)
           and "CODEX_HOME" not in child_env
           and "PYTHONPATH" not in child_env
           and child_env.get("PYTHONNOUSERSITE") == "1"
           and "PM_MCP_TOKEN" not in child_env
           and "SWITCHBOARD_TOKEN" not in child_env,
           "macOS personal test harness denies host files/process inspection and scrubs paths")
        seatbelt_proven = sys.platform != "darwin"
        if sys.platform == "darwin" and original_which("sandbox-exec"):
            denied_results = [original_run(
                sb._personal_test_sandbox_argv([
                    sys.executable, "-c",
                    f"from pathlib import Path; print(Path({str(path)!r}).read_text())",
                ], str(workspace)),
                capture_output=True, text=True, env=child_env,
            ) for path in (codex_auth_path, source_codex_auth_path)]
            allowed_path = workspace / "sandbox-write-proof.txt"
            allowed = original_run(
                sb._personal_test_sandbox_argv([
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(allowed_path)!r}).write_text('ok')",
                ], str(workspace)),
                capture_output=True, text=True, env=child_env,
            )
            persistent_write_denied = True
            shared_root = Path("/Users/Shared")
            shared_path = shared_root / f"switchboard-sandbox-{os.getpid()}.txt"
            if shared_root.is_dir() and os.access(shared_root, os.W_OK):
                shared_attempt = original_run(
                    sb._personal_test_sandbox_argv([
                        sys.executable, "-c",
                        f"from pathlib import Path; Path({str(shared_path)!r}).write_text('no')",
                    ], str(workspace)),
                    capture_output=True, text=True, env=child_env,
                )
                persistent_write_denied = (
                    shared_attempt.returncode != 0 and not shared_path.exists())
                shared_path.unlink(missing_ok=True)
            seatbelt_proven = (
                all(result.returncode != 0 for result in denied_results)
                and all("auth-must-not-cross" not in result.stdout
                        for result in denied_results)
                and allowed.returncode == 0
                and allowed_path.read_text(encoding="utf-8") == "ok"
                and persistent_write_denied
            )
        ok(seatbelt_proven,
           "available macOS sandbox executable denies Codex credential reads but permits workspace tests")

        runtime_overlap_denied = []
        original_prefix, original_base_prefix = sb.sys.prefix, sb.sys.base_prefix
        try:
            sb.sys.prefix = str(state_root)
            sb.sys.base_prefix = str(state_root)
            for platform_name in ("darwin", "linux"):
                os.environ["PM_AGENT_HOST_PLATFORM"] = platform_name
                try:
                    sb._personal_test_sandbox_argv(
                        [sys.executable, "runner.py"], str(workspace))
                    runtime_overlap_denied.append(False)
                except RuntimeError:
                    runtime_overlap_denied.append(True)
        finally:
            sb.sys.prefix = original_prefix
            sb.sys.base_prefix = original_base_prefix
        ok(all(runtime_overlap_denied),
           "personal test sandboxes reject runtime roots that re-expose protected state")

        os.environ["PM_AGENT_HOST_PLATFORM"] = "linux"
        linux_argv = sb._personal_test_sandbox_argv(
            [sys.executable, "runner.py"], str(workspace))
        runtime_root = user_home / "python-runtime"
        runtime_root.mkdir()
        original_prefix, original_base_prefix = sb.sys.prefix, sb.sys.base_prefix
        try:
            sb.sys.prefix = str(runtime_root)
            sb.sys.base_prefix = str(runtime_root)
            linux_home_runtime_argv = sb._personal_test_sandbox_argv(
                [sys.executable, "runner.py"], str(workspace))
        finally:
            sb.sys.prefix = original_prefix
            sb.sys.base_prefix = original_base_prefix
        linux_tmp_hides_home = (
            os.path.commonpath((os.path.realpath("/tmp"), str(user_home.resolve())))
            == os.path.realpath("/tmp"))
        linux_home_hidden = (
            ["--tmpfs", str(user_home.resolve())] in [
                linux_argv[index:index + 2]
                for index, value in enumerate(linux_argv) if value == "--tmpfs"]
            or (linux_tmp_hides_home
                and ["--tmpfs", "/tmp"] in [
                    linux_argv[index:index + 2]
                    for index, value in enumerate(linux_argv) if value == "--tmpfs"]
                and ["--dir", str(user_home.resolve())] in [
                    linux_argv[index:index + 2]
                    for index, value in enumerate(linux_argv) if value == "--dir"]))
        ok(linux_argv[0] == "/usr/bin/bwrap"
           and "--unshare-pid" in linux_argv
           and linux_home_hidden
           and ["--bind", str(workspace.resolve()), str(workspace.resolve())]
               == linux_argv[linux_argv.index("--bind"):
                             linux_argv.index("--bind") + 3]
           and ["--dir", str(workspace.parent.resolve())] in [
               linux_argv[index:index + 2]
               for index, value in enumerate(linux_argv) if value == "--dir"]
           and ["--ro-bind", str(runtime_root.resolve()),
                str(runtime_root.resolve())] in [
               linux_home_runtime_argv[index:index + 3]
               for index, value in enumerate(linux_home_runtime_argv)
               if value == "--ro-bind"],
           "Linux personal test harness hides the supervisor and mounts only workspace writable")

        bubblewrap_available = (
            sys.platform.startswith("linux") and bool(original_which("bwrap")))
        # Exercise the real sandbox when this runner provides it. Absence is
        # covered by the separate fail-closed assertion below.
        bubblewrap_proven = not bubblewrap_available
        bubblewrap_diagnostics = {}
        if bubblewrap_available:
            sb.shutil.which = original_which

            def diagnostic_text(value):
                return str(value or "").replace(
                    "codex-auth-must-not-cross", "<redacted>").replace(
                    "source-codex-auth-must-not-cross", "<redacted>")[-1000:]

            probe = original_run(
                [original_which("bwrap"), "--ro-bind", "/", "/", "--", "true"],
                capture_output=True, text=True, env=child_env)
            if probe.returncode == 0:
                denied_results = [original_run(
                    sb._personal_test_sandbox_argv([
                        sys.executable, "-c",
                        f"from pathlib import Path; print(Path({str(path)!r}).read_text())",
                    ], str(workspace)),
                    capture_output=True, text=True, env=child_env,
                ) for path in (codex_auth_path, source_codex_auth_path)]
                allowed_path = workspace / "bubblewrap-write-proof.txt"
                allowed = original_run(
                    sb._personal_test_sandbox_argv([
                        sys.executable, "-c",
                        f"from pathlib import Path; Path({str(allowed_path)!r}).write_text('ok')",
                    ], str(workspace)),
                    capture_output=True, text=True, env=child_env,
                )
                bubblewrap_proven = (
                    all(result.returncode != 0 for result in denied_results)
                    and all("auth-must-not-cross" not in result.stdout
                            for result in denied_results)
                    and allowed.returncode == 0
                    and allowed_path.read_text(encoding="utf-8") == "ok"
                )
                if not bubblewrap_proven:
                    bubblewrap_diagnostics = {
                        "denied": [{
                            "returncode": result.returncode,
                            "secret_in_stdout": "auth-must-not-cross" in result.stdout,
                            "stdout": diagnostic_text(result.stdout),
                            "stderr": diagnostic_text(result.stderr),
                        } for result in denied_results],
                        "allowed": {
                            "returncode": allowed.returncode,
                            "path_exists": allowed_path.exists(),
                            "stdout": diagnostic_text(allowed.stdout),
                            "stderr": diagnostic_text(allowed.stderr),
                        },
                        "argv": sb._personal_test_sandbox_argv(
                            [sys.executable, "-c", "print('probe')"], str(workspace)),
                    }
            else:
                # Some hosted runners install bwrap while disabling unprivileged
                # namespaces. That is an unavailable sandbox, not an executable one;
                # prove the production path fails closed instead of skipping tests.
                sb.subprocess.run = original_run
                unavailable_kernel = sb.run_executed_tests(
                    str(workspace), "worksession-kernel-denied", "TASK-KERNEL-DENIED",
                    "claim-kernel-denied", "codex/TASK-KERNEL-DENIED",
                    commands=["true"], timeout_s=10)
                bubblewrap_proven = (
                    unavailable_kernel.get("status") == "error"
                    and unavailable_kernel.get("executed") is False)
                if not bubblewrap_proven:
                    bubblewrap_diagnostics = {
                        "probe": {
                            "returncode": probe.returncode,
                            "stdout": diagnostic_text(probe.stdout),
                            "stderr": diagnostic_text(probe.stderr),
                        },
                        "fail_closed_result": {
                            key: diagnostic_text(value)
                            for key, value in unavailable_kernel.items()
                            if key not in {"commands"}
                        },
                    }
                sb.subprocess.run = capture_sandbox
            sb.shutil.which = lambda name: f"/usr/bin/{name}"
        if not bubblewrap_proven and bubblewrap_diagnostics:
            print("  INFO  Linux bubblewrap diagnostics: "
                  + json.dumps(bubblewrap_diagnostics, sort_keys=True))
        ok(bubblewrap_proven,
           "available Linux sandbox executable denies Codex credential reads but permits workspace tests")

        sb.shutil.which = lambda _name: None
        unavailable = sb.run_executed_tests(
            str(workspace), "worksession-no-sandbox", "TASK-NO-SANDBOX",
            "claim-no-sandbox", "codex/TASK-NO-SANDBOX", commands=["true"])
        ok(unavailable.get("status") == "error"
           and unavailable.get("executed") is False
           and "bubblewrap" in unavailable.get("error", ""),
           "personal executed tests fail closed when the OS sandbox is unavailable")
    finally:
        sb.shutil.which = original_which
        sb.subprocess.run = original_run
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
