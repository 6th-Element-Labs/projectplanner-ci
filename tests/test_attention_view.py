"""UI-29: the /api/attention universal view — normalization + ranking."""
from path_setup import ROOT  # noqa: F401  (adds ROOT + src to sys.path for standalone CI)
from switchboard.api.routers.attention import (
    _agent_item, _inbox_item, _rank, _request_item,
)


def test_agent_item_carries_the_decide_contract():
    it = _agent_item({"id": 7, "message": "which loader?", "from_agent": "atlas",
                      "to_agent": "web", "task_id": "ENGINE-9", "requires_ack": 1,
                      "sent_at": 0, "monitor": {"status": "pending"}})
    assert it["attention_id"] == "msg:7"
    assert it["source"] == "agent"
    assert it["task_id"] == "ENGINE-9"
    assert it["decide"]["path"] == "/api/agent_messages/ack"
    assert it["decide"]["body"]["message_id"] == 7
    assert it["payload"]["monitor"] == "pending"


def test_inbox_item_summarizes_triage_and_routes_to_confirm():
    it = _inbox_item({"id": 3, "source": "email", "sender": "ops@x", "subject": "window moved",
                      "summary": "shift dates", "received_at": 0,
                      "triage": {"proposals": [{"task_id": "SHIP-1"}, {"task_id": "SHIP-2"}],
                                 "new_tasks": [{}]}})
    assert it["attention_id"] == "inbox:3"
    assert it["payload"]["proposals"] == 2
    assert it["payload"]["new_tasks"] == 1
    assert it["payload"]["touches"] == ["SHIP-1", "SHIP-2"]
    assert it["decide"]["path"] == "/api/inbox/3/confirm"
    assert it["decide"]["alt"] == "/api/inbox/3/dismiss"


def test_request_item_routes_to_proto8_decide():
    it = _request_item({
        "request_id": "attention-9",
        "task_id": "COORD-46",
        "provider": "switchboard.completion",
        "prompt": "Supply credential",
        "created_at": 0,
        "version": 2,
        "context": {"reason_code": "credentialed_live_proof_unavailable"},
        "choices": [{"id": "supply_credential"}],
        "recommended_default": {"id": "supply_credential"},
    })
    assert it["attention_id"] == "attention:attention-9"
    assert it["source"] == "attention"
    assert it["decide"]["path"] == "/api/attention/requests/attention-9/decide"
    assert it["decide"]["body"]["expected_version"] == 2


def test_rank_puts_parked_agents_before_inbound():
    agent = _agent_item({"id": 1, "message": "q", "sent_at": 0})
    inbox = _inbox_item({"id": 2, "subject": "s", "received_at": 0,
                         "triage": {"proposals": [{"task_id": "A"}] * 5}})
    ranked = sorted([inbox, agent], key=_rank)
    assert ranked[0]["source"] == "agent"


def test_rank_puts_attention_between_agents_and_inbox():
    agent = _agent_item({"id": 1, "message": "q", "sent_at": 0})
    attention = _request_item({
        "request_id": "a1", "prompt": "need credential", "created_at": 0,
        "version": 1, "context": {}, "choices": [{"id": "go"}],
    })
    inbox = _inbox_item({"id": 2, "subject": "s", "received_at": 0,
                         "triage": {"proposals": [{"task_id": "A"}] * 5}})
    ranked = sorted([inbox, attention, agent], key=_rank)
    assert [i["source"] for i in ranked] == ["agent", "attention", "inbox"]


def test_rank_prefers_deadlines_then_breadth_within_a_source():
    a = _inbox_item({"id": 1, "subject": "one", "received_at": 0,
                     "triage": {"proposals": [{"task_id": "X"}]}})
    b = _inbox_item({"id": 2, "subject": "five", "received_at": 0,
                     "triage": {"proposals": [{"task_id": str(i)} for i in range(5)]}})
    assert sorted([a, b], key=_rank)[0]["payload"]["proposals"] == 5


if __name__ == "__main__":
    test_agent_item_carries_the_decide_contract()
    test_inbox_item_summarizes_triage_and_routes_to_confirm()
    test_request_item_routes_to_proto8_decide()
    test_rank_puts_parked_agents_before_inbound()
    test_rank_puts_attention_between_agents_and_inbox()
    test_rank_prefers_deadlines_then_breadth_within_a_source()
