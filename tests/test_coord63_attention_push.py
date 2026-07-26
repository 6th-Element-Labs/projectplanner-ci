"""COORD-63: external delivery remains an adapter over Needs-you authority."""
from __future__ import annotations

from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import attention_push


def test_delivery_deep_links_and_audits_success():
    item = {
        "attention_id": "runner-progress:run-1",
        "source": "runner",
        "task_id": "COORD-63",
        "runner_session_id": "run-1",
        "title": "runner stalled",
    }
    with patch.object(attention_push.notify, "send",
                      return_value=[{"channel": "email", "sent": True}]) as send, \
         patch.object(attention_push, "append_activity") as audit:
        receipt = attention_push.deliver(item, project="switchboard")
    assert receipt["delivered"] is True
    assert receipt["deep_link"].endswith(
        "/?project=switchboard&attention=runner-progress%3Arun-1#tab-needs")
    assert "run-1" in send.call_args.args[1]
    assert send.call_args.args[3:] == ("switchboard", "notify")
    assert audit.call_args.args[0] == "attention.push_delivered"


def test_unconfigured_delivery_is_auditable_miss():
    with patch.object(attention_push.notify, "send", return_value=[
        {"channel": "slack", "sent": False, "dry_run": True},
        {"channel": "email", "sent": False, "dry_run": True},
    ]), patch.object(attention_push, "append_activity") as audit:
        receipt = attention_push.deliver({
            "attention_id": "provider:req-1",
            "source": "provider",
            "title": "Choose",
        }, project="switchboard")
    assert receipt["delivered"] is False
    assert audit.call_args.args[0] == "attention.push_missed"


if __name__ == "__main__":
    test_delivery_deep_links_and_audits_success()
    test_unconfigured_delivery_is_auditable_miss()
    print("COORD-63 attention push: 2 passed")
