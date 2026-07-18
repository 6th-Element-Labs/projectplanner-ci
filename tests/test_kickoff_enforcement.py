"""UI-31: PM_KICKOFF_ENFORCE gates claim_next and merge_gate (fail-open off)."""
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


def test_disarmed_is_a_no_op():
    with patch.dict(os.environ):
        os.environ.pop("PM_KICKOFF_ENFORCE", None)
        p = _proj()
        calls = []
        out = claim_next_cmd.execute(
            _cmd(p), actor="t",
            claim=lambda **kw: calls.append(kw) or {"claimed": True})
        assert out == {"claimed": True} and len(calls) == 1


def test_armed_blocks_claims_while_gates_open():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        calls = []
        out = claim_next_cmd.execute(
            _cmd(p), actor="t",
            claim=lambda **kw: calls.append(kw) or {"claimed": True})
        assert out["claimed"] is False
        assert out["reason"] == "kickoff_blocked"
        assert out["blocking_gate"] == "vision"
        assert calls == []          # the claimer is never consulted


def test_armed_passes_through_once_authorized():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        for g in ["vision", "prd", "arch", "rules", "scope"]:
            store.approve_kickoff_gate(g, actor="t", project=p)
        out = claim_next_cmd.execute(
            _cmd(p), actor="t", claim=lambda **kw: {"claimed": True})
        assert out == {"claimed": True}


def test_armed_blocks_merge_gate_with_a_named_gate():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        out = store.merge_gate({"task_id": "X-1"}, actor="t", project=p)
        assert out["ok"] is False and out["status"] == "blocked"
        f = out["findings"][0]
        assert f["code"] == "kickoff_blocked"
        assert f["blocking_gate"] == "vision"


def test_revise_deauthorizes_enforced_projects_too():
    with patch.dict(os.environ, {"PM_KICKOFF_ENFORCE": "1"}):
        p = _proj()
        for g in ["vision", "prd", "arch", "rules", "scope"]:
            store.approve_kickoff_gate(g, actor="t", project=p)
        store.revise_kickoff_gate("arch", actor="t", project=p)
        out = claim_next_cmd.execute(
            _cmd(p), actor="t", claim=lambda **kw: {"claimed": True})
        assert out["claimed"] is False and out["blocking_gate"] == "rules"


if __name__ == "__main__":
    test_disarmed_is_a_no_op()
    test_armed_blocks_claims_while_gates_open()
    test_armed_passes_through_once_authorized()
    test_armed_blocks_merge_gate_with_a_named_gate()
    test_revise_deauthorizes_enforced_projects_too()
