#!/usr/bin/env python3
"""ADAPTER-11 public install bundle regression tests."""

import json
import tempfile
from pathlib import Path

from adapters.marketplace import PROFILES, install


with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    for runtime in PROFILES:
        target = root / runtime
        target.mkdir()
        result = install(runtime, target, "https://switchboard.example", "demo")
        assert result["runtime"] == runtime
        assert result["profile"]["tier"].startswith("T")
        assert (target / ".switchboard" / f"{runtime}.json").is_file()
        config = (target / ".switchboard" / "adapter.env.example").read_text()
        assert "PM_BASE=https://switchboard.example" in config
        assert "PM_PROJECT=demo" in config

    claude_settings = json.loads((root / "claude-code/.claude/settings.json").read_text())
    commands = json.dumps(claude_settings["hooks"])
    assert "/.switchboard/adapters/claude-code/" in commands
    assert (root / "claude-code/.switchboard/adapters/switchboard_core.py").is_file()
    assert (root / "cursor/.cursor/mcp.json").is_file()
    assert (root / "cursor/.cursor/rules/switchboard.mdc").is_file()
    assert (root / "codex/.switchboard/adapters/codex/codex_adapter.py").is_file()

print("PASS ADAPTER-11 marketplace installs all runtime profiles")
