#!/usr/bin/env python3
"""QA-29: v4 must see authenticated Human receipts without treating Blocked as authority."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401

_TMP = Path(tempfile.mkdtemp(prefix="qa29-v4-human-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(_TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(_TMP / "registry.db")
(_TMP / "projects").mkdir()
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import human_blocker as human_blocker_cmd  # noqa: E402
from switchboard.domain.mission_bot_v4 import decide_mission_transition  # noqa: E402


PROJECT = "switchboard"
AGENT = "agent/codex/qa-29-fixture"


def _create_bound_task() -> tuple[str, str]:
    task = store.create_task(
        {
            "workstream_id": "QA",
            "title": "QA-29 server-stamped Human receipt",
            "description": "session_profile:code_strict",
            "status": "Not Started",
            "ui_impact": "no",
        },
        actor="test",
        project=PROJECT,
    )
    task_id = task["task_id"]
    with _conn(PROJECT) as connection:
        connection.execute(
            "UPDATE tasks SET status='In Progress', assignee=? WHERE task_id=?",
            (AGENT, task_id),
        )
    session_result = store.create_work_session(
        {
            "agent_id": AGENT,
            "task_id": task_id,
            "repo_role": "canonical",
            "storage_mode": "worktree",
            "worktree_path": str(_TMP / f"wt-{task_id}"),
            "branch": f"codex/{task_id}-qa29",
            "upstream": f"origin/codex/{task_id}-qa29",
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "status": "active",
            "dirty_status": "clean",
            "policy_profile": "code_strict",
            "env": {
                "execution_id": f"exec-{task_id}",
                "execution_generation": 1,
                "execution_role": "implementation",
            },
        },
        actor=AGENT,
        project=PROJECT,
    )
    work_session_id = session_result["work_session"]["work_session_id"]
    now = time.time()
    with _conn(PROJECT) as connection:
        connection.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, status, claimed_at, "
            "expires_at, principal_id) VALUES (?,?,?,?,?,?,?)",
            (
                f"taskclaim-{task_id.lower()}",
                task_id,
                AGENT,
                "active",
                now,
                now + 1800,
                "test",
            ),
        )
    return task_id, work_session_id


class MissionBotV4HumanReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store.init_db(PROJECT)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_real_server_command_receipt_yields_human_never_start(self) -> None:
        task_id, work_session_id = _create_bound_task()
        original_fence = human_blocker_cmd._fence_session_runner
        human_blocker_cmd._fence_session_runner = lambda *args, **kwargs: {
            "fenced": True,
            "execution_id": f"exec-{task_id}",
            "generation": 1,
        }
        try:
            result = human_blocker_cmd.execute_mapping(
                {
                    "task_id": task_id,
                    "work_session_id": work_session_id,
                    "reason": "operator_answer_required",
                    "binding": "direct_session",
                    "agent_id": AGENT,
                    "source_tool": "agent_requires_human",
                },
                actor=AGENT,
                project=PROJECT,
            )
        finally:
            human_blocker_cmd._fence_session_runner = original_fence

        self.assertTrue(result["recorded"])
        session = store.get_work_session(work_session_id, project=PROJECT)
        receipt = session["hygiene"]["blocker"]
        decision = decide_mission_transition(
            {
                "scope_active": True,
                "terminal_provenance": False,
                "dependencies_satisfied": True,
                "human_request": receipt,
                "runner_live": False,
                "handled_through": 2,
                "latest_sequence": 3,
                "requested_role": "remediation",
            }
        )
        self.assertEqual("HUMAN", decision["state"])
        self.assertEqual("wait", decision["action"])
        self.assertNotEqual("start_task", decision["action"])

    def test_board_blocked_alone_allows_replacement_start(self) -> None:
        decision = decide_mission_transition(
            {
                "scope_active": True,
                "terminal_provenance": False,
                "dependencies_satisfied": True,
                "board_status": "Blocked",
                "human_request": None,
                "runner_live": False,
                "handled_through": 7,
                "latest_sequence": 8,
                "requested_role": "remediation",
            }
        )
        self.assertEqual(
            {
                "state": "ACTIVE",
                "action": "start_task",
                "requested_role": "remediation",
                "event_pointer": 8,
            },
            decision,
        )

    def test_coord_98_three_receipts_replay_to_human(self) -> None:
        corpus_path = (
            ROOT / "tests/fixtures/mission_bot_v4/coord_98_human_receipts.json"
        )
        corpus = json.loads(corpus_path.read_text())
        self.assertEqual("COORD-98", corpus["task_id"])
        self.assertEqual(3, len(corpus["receipts"]))
        for incident in corpus["receipts"]:
            decision = decide_mission_transition(
                {
                    "scope_active": True,
                    "terminal_provenance": False,
                    "dependencies_satisfied": True,
                    "human_request": incident["blocker"],
                    "runner_live": False,
                    "handled_through": 0,
                    "latest_sequence": 1,
                    "requested_role": "remediation",
                }
            )
            self.assertEqual("HUMAN", decision["state"], incident["work_session_id"])
            self.assertEqual("wait", decision["action"])


if __name__ == "__main__":
    unittest.main()
