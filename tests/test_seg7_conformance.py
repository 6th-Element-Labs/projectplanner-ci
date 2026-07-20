"""SEG-7 conformance harness contract and thin-adapter ratchet."""
from __future__ import annotations

import ast
from pathlib import Path

from path_setup import ROOT
from scripts.seg7_conformance import SURFACES, run


def test_two_project_conformance_report():
    report = run()
    assert report["schema"] == "switchboard.segmentation_conformance.v1"
    assert report["task_id"] == "SEG-7"
    assert len(report["tested_sha"]) == 40
    assert report["scenario"]["directions"] == 2
    assert set(report["scenario"]["surfaces"]) == set(SURFACES)
    assert report["llm_calls"] == 0
    assert report["embedding_gateway_calls"] == 0
    assert report["local_embedding_calls"] > 0
    assert report["cardinality"]["projects"] == 64
    assert report["cardinality"]["cache_entries"] <= 8
    assert report["ok"], report["failures"]


def test_tested_sha_override_fails_closed(monkeypatch):
    monkeypatch.setenv("SEG7_TESTED_SHA", "0" * 40)
    try:
        run()
    except ValueError as exc:
        assert "does not match git HEAD" in str(exc)
    else:
        raise AssertionError("spoofed tested SHA was accepted")


def test_customer_adapters_remain_thin():
    ceilings = {"app.py": 25, "mcp_server.py": 25, "store.py": 900}
    for name, ceiling in ceilings.items():
        path = ROOT / name
        assert len(path.read_text(encoding="utf-8").splitlines()) <= ceiling, name
        ast.parse(path.read_text(encoding="utf-8"), filename=name)
