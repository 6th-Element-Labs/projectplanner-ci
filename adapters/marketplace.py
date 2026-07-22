#!/usr/bin/env python3
"""Install and verify the public Switchboard runtime adapter bundles."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "adapters"

PROFILES = {
    "claude-code": {"tier": "T2", "control": "hook_deny", "caveat": "hooks must be enabled by Claude Code", "files": ["switchboard_core.py", "claude-code"]},
    "codex": {"tier": "T1/T2", "control": "advisory_poll unless launcher honors deny", "caveat": "native pre-tool denial is launcher-dependent", "files": ["switchboard_core.py", "codex"]},
    "cursor": {"tier": "T1", "control": "advisory_poll", "caveat": "the project rule is advisory unless a managed runner adds a guard", "files": []},
    "openai-loop": {"tier": "T1", "control": "integrator_enforced", "caveat": "the integrator must call guard_tool before every effect", "files": ["switchboard_core.py", "openai-loop"]},
    "langgraph": {"tier": "T2", "control": "hook_deny when every boundary is wrapped", "caveat": "unwrapped graph boundaries reduce fidelity to T1", "files": ["switchboard_core.py", "langgraph"]},
    "agent-host": {"tier": "T3", "control": "managed runner + runner kill", "caveat": "production enrollment requires a server-issued signed host bundle", "files": []},
}


def _merge(left: dict, right: dict) -> dict:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def install(runtime: str, target: Path, base_url: str, project: str) -> dict:
    profile = PROFILES[runtime]
    bundle_root = target / ".switchboard" / "adapters"
    bundle_root.mkdir(parents=True, exist_ok=True)
    installed = []
    for relative in profile["files"]:
        source = ADAPTERS / relative
        destination = bundle_root / relative
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        installed.append(str(destination.relative_to(target)))

    if runtime == "claude-code":
        source_settings = json.loads((ADAPTERS / "claude-code/settings.json").read_text())
        encoded = json.dumps(source_settings).replace("/adapters/", "/.switchboard/adapters/")
        source_settings = json.loads(encoded)
        settings_path = target / ".claude" / "settings.json"
        current = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        _write_json(settings_path, _merge(current, source_settings))
        installed.append(".claude/settings.json")
    elif runtime == "cursor":
        for relative in ("mcp.json", "rules/switchboard.mdc"):
            source = ROOT / ".cursor" / relative
            destination = target / ".cursor" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == "mcp.json" and destination.exists():
                _write_json(destination, _merge(json.loads(destination.read_text()), json.loads(source.read_text())))
            else:
                shutil.copy2(source, destination)
            installed.append(str(destination.relative_to(target)))

    config = target / ".switchboard" / "adapter.env.example"
    config.write_text(
        f"PM_BASE={base_url}\nPM_PROJECT={project}\nPM_MCP_TOKEN=replace-with-scoped-token\n"
        f"PM_AGENT_ID={runtime}/replace-with-scope\n",
        encoding="utf-8",
    )
    installed.append(str(config.relative_to(target)))
    manifest = {"schema": "switchboard.adapter_install.v1", "runtime": runtime,
                "profile": profile, "installed": installed}
    _write_json(target / ".switchboard" / f"{runtime}.json", manifest)
    return manifest


def smoke(runtime: str, project: str) -> int:
    profile = PROFILES[runtime]
    command = [sys.executable, str(ADAPTERS / "conformance.py"), "--adapter", runtime,
               "--runtime", runtime, "--project", project,
               "--control-mode", "hook_deny" if runtime in {"claude-code", "langgraph"} else "advisory_poll"]
    print("Control fidelity: " + json.dumps(profile, sort_keys=True))
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install public Switchboard adapter bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    add = sub.add_parser("install")
    add.add_argument("runtime", choices=sorted(PROFILES))
    add.add_argument("--target", type=Path, default=Path.cwd())
    add.add_argument("--base-url", default="https://plan.taikunai.com")
    add.add_argument("--project", default="switchboard")
    check = sub.add_parser("smoke")
    check.add_argument("runtime", choices=sorted(PROFILES))
    check.add_argument("--project", default="switchboard")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps(PROFILES, indent=2, sort_keys=True))
        return 0
    if args.command == "install":
        print(json.dumps(install(args.runtime, args.target.resolve(), args.base_url, args.project), indent=2))
        return 0
    return smoke(args.runtime, args.project)


if __name__ == "__main__":
    raise SystemExit(main())
