#!/usr/bin/env python3
"""ADAPTER-25 replayable Cursor attention conformance."""
from __future__ import annotations

import json
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from adapters.cursor.attention import (
    CURSOR_PROBED_VERSION,
    CursorAttentionUnsupported,
    capability_manifest,
    inspect_stream,
    normalize_attention_request,
)


fixture = json.loads((
    ROOT / "tests/fixtures/cursor_attention_2026_07_23.json"
).read_text(encoding="utf-8"))
cases = fixture["cases"]

question = inspect_stream(cases["question_text"])
assert question.session_id == "bc00d858-c8aa-44d3-99dd-5c9089a61619"
assert question.request_id == "572a376f-4f18-4e2c-8f93-f537fb27c3f5"
assert question.terminal is True
assert question.question_request_count == 0
assert question.approval_request_count == 0

shell = inspect_stream(cases["shell_without_attention_request"])
assert shell.question_request_count == shell.approval_request_count == 0

elicitation = inspect_stream(cases["mcp_elicitation"])
assert elicitation.elicitation_actions == ("decline",)

for case_name, events in cases.items():
    for event in events:
        try:
            normalize_attention_request(
                event, task_id="ADAPTER-25", host_id="host/live",
                runner_session_id="runner/live")
        except CursorAttentionUnsupported as exc:
            assert exc.as_dict()["visible"] is True
            assert exc.as_dict()["provider_version"] == CURSOR_PROBED_VERSION
        else:
            raise AssertionError(f"{case_name} event was normalized optimistically")

manifest = capability_manifest()
assert manifest["support_status"] == "unsupported_fail_closed"
assert manifest["supported_request_kinds"] == []
assert manifest["capture"]["mcp_elicitation_reply"] == "automatic_decline"
assert fixture["secrets_redacted"] is True
assert "@" not in json.dumps(fixture)

print("ADAPTER-25 Cursor attention: 12 passed, 0 failed")
