#!/usr/bin/env python3
"""COORD-27: managed worker pushes use ephemeral, secret-free GitHub CLI auth."""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "switchboard_core_coord27_test", ROOT / "adapters" / "switchboard_core.py")
sb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sb
spec.loader.exec_module(sb)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


TOKEN = "coord27-secret-sentinel"
HEAD = "a" * 40
calls = []


def fake_run(argv, **kwargs):
    calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
    if argv[-3:] == ["remote", "get-url", "origin"]:
        return SimpleNamespace(returncode=0, stdout=(
            "https://github.com/6th-Element-Labs/projectplanner.git\n"), stderr="")
    if argv[:3] == ["gh", "auth", "setup-git"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if "push" in argv:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if "ls-remote" in argv:
        return SimpleNamespace(returncode=0, stdout=f"{HEAD}\trefs/heads/codex/COORD-27\n",
                               stderr="")
    raise AssertionError(f"unexpected subprocess argv: {argv}")


saved_run = sb.subprocess.run
saved_gh = os.environ.get("GH_TOKEN")
saved_github = os.environ.get("GITHUB_TOKEN")
try:
    sb.subprocess.run = fake_run
    os.environ.pop("GH_TOKEN", None)
    os.environ["GITHUB_TOKEN"] = TOKEN
    result = sb._push_and_verify("/worker", "codex/COORD-27", HEAD)

    argvs = [call["argv"] for call in calls]
    setup_index = next(i for i, argv in enumerate(argvs) if argv[:3] == ["gh", "auth", "setup-git"])
    push_index = next(i for i, argv in enumerate(argvs) if "push" in argv)
    setup_call = calls[setup_index]
    push_call = calls[push_index]
    ok(result.get("ok") is True and result.get("remote_sha") == HEAD,
       "GitHub HTTPS branch is pushed and exact remote head is verified")
    ok(setup_index < push_index, "GitHub credential helper is configured before push")
    ok(setup_call["env"].get("GH_TOKEN") == TOKEN,
       "GITHUB_TOKEN is normalized to GH_TOKEN only in the child environment")
    ok(push_call["env"].get("GIT_TERMINAL_PROMPT") == "0",
       "push is noninteractive")
    ok(bool(push_call["env"].get("GIT_CONFIG_GLOBAL")) and
       "switchboard-git-auth-" in push_call["env"]["GIT_CONFIG_GLOBAL"],
       "push uses an isolated temporary global Git config")
    ok(not Path(push_call["env"]["GIT_CONFIG_GLOBAL"]).parent.exists(),
       "temporary Git credential-helper config is removed after verification")
    ok(all(TOKEN not in " ".join(call["argv"]) for call in calls),
       "runtime token never appears in command arguments")
    ok(TOKEN not in str(result), "runtime token never appears in returned evidence")

    calls.clear()
    os.environ.pop("GH_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)
    missing = sb._push_and_verify("/worker", "codex/COORD-27", HEAD)
    ok(missing.get("ok") is False and "missing GitHub runtime token" in missing.get("detail", ""),
       "GitHub HTTPS push fails closed when no runtime token is available")
    ok(not any("push" in call["argv"] for call in calls),
       "missing-token failure occurs before push")
finally:
    sb.subprocess.run = saved_run
    if saved_gh is None:
        os.environ.pop("GH_TOKEN", None)
    else:
        os.environ["GH_TOKEN"] = saved_gh
    if saved_github is None:
        os.environ.pop("GITHUB_TOKEN", None)
    else:
        os.environ["GITHUB_TOKEN"] = saved_github

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
