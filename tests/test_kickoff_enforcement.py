"""Kickoff approvals remain advisory and cannot disable Autopilot."""
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

TMP = Path(tempfile.mkdtemp(prefix="kickoff-enforcement-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")

from path_setup import ROOT  # noqa: F401  (adds ROOT + src to sys.path for standalone CI)
import store
from switchboard.application.commands import claim_next as claim_next_cmd
from switchboard.application.contracts.claims import ClaimNextCommand


def _proj():
    pid = "kickoffenf" + uuid.uuid4().hex[:8]
    store.create_project(pid, project_id=pid)
    return pid


def _cmd(p):
    return ClaimNextCommand(agent_id="agent-x", project=p)


def test_retired_setting_cannot_block_claims_while_gates_open():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        calls = []
        out = claim_next_cmd.execute(
            _cmd(p), actor="t",
            claim=lambda **kw: calls.append(kw) or {"claimed": True})
        assert out == {"claimed": True}
        assert len(calls) == 1
        state = store.get_kickoff_state(project=p)
        assert state["build_authorized"] is False
        assert state["enforced"] is False
        assert store.kickoff_enforcement(project=p) == {
            "enforced": False,
            "authorized": True,
            "blocking_gate": "",
            "reason": "",
        }


def test_complete_kickoff_record_remains_visible_but_advisory():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        for g in ["vision", "prd", "arch", "rules", "scope"]:
            store.approve_kickoff_gate(g, actor="t", project=p)
        out = claim_next_cmd.execute(
            _cmd(p), actor="t", claim=lambda **kw: {"claimed": True})
        assert out == {"claimed": True}


def test_retired_setting_cannot_add_a_kickoff_merge_finding():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        out = store.merge_gate({"task_id": "X-1"}, actor="t", project=p)
        codes = {finding["code"] for finding in out["findings"]}
        assert "kickoff_blocked" not in codes


def test_revised_kickoff_record_does_not_block_claims():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        for g in ["vision", "prd", "arch", "rules", "scope"]:
            store.approve_kickoff_gate(g, actor="t", project=p)
        store.revise_kickoff_gate("arch", actor="t", project=p)
        out = claim_next_cmd.execute(
            _cmd(p), actor="t", claim=lambda **kw: {"claimed": True})
        assert out == {"claimed": True}
        assert store.get_kickoff_state(project=p)["build_authorized"] is False


if __name__ == "__main__":
    test_retired_setting_cannot_block_claims_while_gates_open()
    test_complete_kickoff_record_remains_visible_but_advisory()
    test_retired_setting_cannot_add_a_kickoff_merge_finding()
    test_revised_kickoff_record_does_not_block_claims()
