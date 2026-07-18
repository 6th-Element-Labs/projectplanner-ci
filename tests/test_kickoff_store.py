"""UI-30: kickoff record store — ladder invariants."""
import os
import tempfile
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kickoff-store-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")

from path_setup import ROOT  # noqa: F401  (adds ROOT + src to sys.path for standalone CI)
import store
from switchboard.storage.repositories.kickoff import KickoffGateError


def _proj():
    pid = "kickofftest" + uuid.uuid4().hex[:8]
    store.create_project(pid, project_id=pid)
    return pid


def _assert_raises_kickoff_gate_error(action):
    try:
        action()
    except KickoffGateError:
        return
    raise AssertionError("expected KickoffGateError")


def test_fresh_project_frontier_is_vision_and_blocked():
    p = _proj()
    st = store.get_kickoff_state(project=p)
    assert st["frontier"] == "vision"
    assert st["build_authorized"] is False
    assert [g["s"] for g in st["gates"]] == ["now", "wait", "wait", "wait", "wait"]


def test_approve_walks_the_ladder_and_grants_authorization():
    p = _proj()
    for g in ["vision", "prd", "arch", "rules", "scope"]:
        st = store.approve_kickoff_gate(g, actor="tester", project=p)
    assert st["build_authorized"] is True
    assert st["frontier"] == ""
    assert all(x["s"] == "ok" for x in st["gates"])
    assert all(x["approved_by"] == "tester" for x in st["gates"])


def test_cannot_skip_ahead():
    p = _proj()
    _assert_raises_kickoff_gate_error(
        lambda: store.approve_kickoff_gate("arch", actor="tester", project=p))


def test_revise_marks_downstream_stale_and_deauthorizes():
    p = _proj()
    for g in ["vision", "prd", "arch", "rules", "scope"]:
        store.approve_kickoff_gate(g, actor="tester", project=p)
    st = store.revise_kickoff_gate("prd", actor="tester", project=p)
    assert st["build_authorized"] is False
    by = {g["gate"]: g for g in st["gates"]}
    assert by["prd"]["s"] == "ok" and by["prd"]["version"] == 2      # stays approved, bumped
    assert by["vision"]["s"] == "ok"                                  # upstream untouched
    assert by["arch"]["s"] == "stale"
    assert by["rules"]["s"] == "stale"
    assert by["scope"]["s"] == "stale"
    # re-approving the stale chain restores authorization
    for g in ["arch", "rules", "scope"]:
        st = store.approve_kickoff_gate(g, actor="tester", project=p)
    assert st["build_authorized"] is True


def test_revise_requires_an_approved_gate():
    p = _proj()
    _assert_raises_kickoff_gate_error(
        lambda: store.revise_kickoff_gate("vision", actor="tester", project=p))


if __name__ == "__main__":
    test_fresh_project_frontier_is_vision_and_blocked()
    test_approve_walks_the_ladder_and_grants_authorization()
    test_cannot_skip_ahead()
    test_revise_marks_downstream_stale_and_deauthorizes()
    test_revise_requires_an_approved_gate()
