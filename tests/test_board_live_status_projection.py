#!/usr/bin/env python3
"""Live Task Execution paints blue without mutating workflow status."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT
from switchboard.storage.repositories import tasks as tasks_repo


BOARD = ROOT / "static" / "js" / "board.js"
INDEX = ROOT / "static" / "index.html"


def main() -> int:
    with patch(
        "switchboard.storage.repositories.runner.list_runner_sessions",
        return_value=[
            {"task_id": "ADAPTER-39", "status": "running", "heartbeat_at": 100, "heartbeat_ttl_s": 180},
            {"task_id": "STALE-1", "status": "running", "heartbeat_at": 1, "heartbeat_ttl_s": 10},
            {"task_id": "DONE-1", "status": "stopped", "heartbeat_at": 100, "heartbeat_ttl_s": 180},
        ],
    ), patch("switchboard.storage.repositories.tasks.time.time", return_value=110):
        assert tasks_repo._board_live_task_ids("switchboard") == {"ADAPTER-39"}

    node = shutil.which("node")
    if not node:
        print("SKIP board projection proof requires node")
        return 0
    script = Path(tempfile.mkdtemp(prefix="board-live-status-")) / "proof.js"
    try:
        script.write_text(
            "const window = {};\n"
            + BOARD.read_text(encoding="utf-8")
            + "\nconst methods = window.SwitchboardBoard.methods;\n"
            + "const app = Object.assign({\n"
            + "  tasks: [], STATUS_COLOR: {'Not Started':'secondary','In Progress':'blue','In Review':'yellow','Blocked':'red','Done':'green'},\n"
            + "  esc: (x) => String(x == null ? '' : x), taskTally: () => null, tallyMini: () => '',\n"
            + "  provenanceBadge: () => '', completionProjectionHtml: () => ''\n"
            + "}, methods);\n"
            + "const live = {task_id:'ADAPTER-39', title:'live', status:'Not Started', honest_display:{lifecycle_phase:'running', label:'In Progress'}};\n"
            + "const idle = {task_id:'IDLE-1', title:'idle', status:'Not Started', honest_display:{lifecycle_phase:'not_started', label:'Not Started'}};\n"
            + "const repair = {task_id:'BUG-1', title:'repair', status:'Blocked', honest_display:{lifecycle_phase:'running', label:'In Progress'}};\n"
            + "const done = {task_id:'DONE-1', title:'done', status:'Done', honest_display:{lifecycle_phase:'running', label:'In Progress'}};\n"
            + "app.tasks = [live, idle, repair, done];\n"
            + "console.log(JSON.stringify({live:app._boardTaskStatus(live), idle:app._boardTaskStatus(idle), repair:app._boardTaskStatus(repair), done:app._boardTaskStatus(done), columns:app._boardColumns('status'), card:app.taskCard(live)}));\n",
            encoding="utf-8",
        )
        run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        result = json.loads(run.stdout)
        assert result["live"] == "In Progress"
        assert result["idle"] == "Not Started"
        assert result["repair"] == "In Progress"
        assert result["done"] == "Done"
        assert result["columns"] == ["Not Started", "In Progress", "Done"]
        assert "bg-blue" in result["card"]
        assert live_status_is_projection_only()
        assert 'src="js/board.js?v=4"' in INDEX.read_text(encoding="utf-8")
        print("PASS live runner projects In Progress without workflow mutation")
        return 0
    finally:
        shutil.rmtree(script.parent, ignore_errors=True)


def live_status_is_projection_only() -> bool:
    source = BOARD.read_text(encoding="utf-8")
    body = source[source.index("_boardTaskStatus(task)"):source.index("_boardColumns(mode)")]
    return "task.status =" not in body and "updateTask" not in body


if __name__ == "__main__":
    raise SystemExit(main())
