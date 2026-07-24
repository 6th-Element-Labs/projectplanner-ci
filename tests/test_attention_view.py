"""UI-29: the /api/attention universal view — normalization + ranking."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401  (adds ROOT + src to sys.path for standalone CI)
from switchboard.api.routers.attention import (
    _agent_item, _decision_item, _dedupe, _inbox_item, _mission_item,
    _provider_item, _rank, create_router,
)


def test_agent_item_carries_the_decide_contract():
    it = _agent_item({"id": 7, "message": "which loader?", "from_agent": "atlas",
                      "to_agent": "web", "task_id": "ENGINE-9", "requires_ack": 1,
                      "sent_at": 0, "monitor": {"status": "pending"}})
    assert it["attention_id"] == "message:7"
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


def test_rank_is_impact_then_downstream_then_deadline_then_age():
    blocking = _agent_item({"id": 1, "message": "q", "sent_at": 20})
    risky = _inbox_item({"id": 2, "subject": "s", "received_at": 10,
                         "triage": {"proposals": [{"task_id": "A"}] * 9}})
    assert sorted([risky, blocking], key=_rank)[0]["source"] == "agent"
    first = dict(risky, source_id="a", unfinished_downstream=2, deadline=200, age_s=10)
    second = dict(risky, source_id="b", unfinished_downstream=2, deadline=100, age_s=20)
    assert sorted([first, second], key=_rank)[0]["source_id"] == "b"


def test_rank_prefers_deadlines_then_breadth_within_a_source():
    a = _inbox_item({"id": 1, "subject": "one", "received_at": 0,
                     "triage": {"proposals": [{"task_id": "X"}]}})
    b = _inbox_item({"id": 2, "subject": "five", "received_at": 0,
                     "triage": {"proposals": [{"task_id": str(i)} for i in range(5)]}})
    assert sorted([a, b], key=_rank)[0]["payload"]["proposals"] == 5


def test_projection_sources_have_stable_ids_links_blast_radius_and_evidence():
    provider = _provider_item({
        "request_id": "req-1", "prompt": "Choose", "provider": "openai",
        "task_id": "COORD-41", "host_id": "host-1", "runner_session_id": "run-1",
        "status": "pending", "schema_version": "provider.question.v1",
        "context": {"mission_id": "operator-ui", "deliverable_id": "alerts",
                    "unfinished_downstream": 4,
                    "completed_work_summary": "Shipped PR #812",
                    "why_automation_stopped": "Credential required",
                    "resume_condition": "Credential supplied",
                    "next_automatic_action": "Re-run live proof",
                    "blast_radius": {"tasks": 4}, "evidence": {"steps": []}},
    })
    mission = _mission_item(
        {"deliverable_id": "alerts", "mission_id": "operator-ui"},
        {"action": "approve", "attention": True, "delivery_impact": "blocking",
         "task_id": "COORD-41", "host_id": "host-1", "work_session_id": "ws-1",
         "blast_radius": {"tasks": 3}, "evidence": {"proof": "fixture"}}, 0)
    decision = _decision_item({
        "id": 12, "status": "proposed", "task_id": "COORD-41",
        "deliverable_id": "alerts", "title": "Ship?", "blast_radius": {"tasks": 2},
        "evidence": {"source": "wireframe"},
    })
    assert provider["source_id"] == "provider:req-1"
    assert provider["links"]["host"] == "host-1"
    assert provider["links"]["session"] == "run-1"
    assert provider["payload"]["blast_radius"]["tasks"] == 4
    assert provider["payload"]["status"] == "pending"
    assert provider["payload"]["completed_work_summary"] == "Shipped PR #812"
    assert provider["payload"]["frozen_payload"]["deliverable_id"] == "alerts"
    assert provider["links"]["deliverable"] == "alerts"
    assert mission["links"]["mission"] == "operator-ui"
    assert mission["payload"]["evidence"]["proof"] == "fixture"
    assert decision["source_id"] == "decision:12"
    assert _dedupe([provider, dict(provider)]) == [provider]


def test_projection_is_project_scoped_counts_sources_and_tracks_source_transition():
    class Service:
        items = [{
            "request_id": "req-1", "prompt": "Choose", "provider": "openai",
            "status": "pending", "created_at": 10, "context": {},
        }]

        def list_operator_queue(self, ctx, *, limit=100, offset=0):
            assert ctx.project_id == "switchboard"
            return {"project": ctx.project_id, "count": len(self.items),
                    "items": list(self.items)}

    service = Service()
    app = FastAPI()
    app.include_router(create_router(
        resolve_project=lambda project: project,
        resolve_principal=lambda *_args, **_kwargs: {
            "id": "operator", "kind": "user", "project": "switchboard",
            "effective_scopes": ["read"],
        },
        resolve_body_project=lambda body: body["project"],
        list_pending_acks=lambda **kwargs: [{
            "id": 9, "message": "Ack", "requires_ack": True, "sent_at": 20,
        }] if kwargs["project"] == "switchboard" else [],
        list_inbox=lambda status, project: [{
            "id": 4, "subject": "Review", "received_at": 30, "triage": {},
        }] if status == "pending" and project == "switchboard" else [],
        list_deliverables=lambda **kwargs: [{"id": "alerts"}],
        get_mission_status=lambda **kwargs: {
            "mission_id": "operator-ui", "deliverable_id": kwargs["deliverable_id"],
            "next_actions": [{"action": "approve", "attention": True,
                              "delivery_impact": "blocking"}],
        },
        list_decisions=lambda **kwargs: [{
            "id": 2, "title": "Open", "status": "proposed",
        }] if kwargs["project"] == "switchboard" and kwargs["status"] == "proposed" else [],
        service=service,
    ))
    client = TestClient(app)
    first = client.get("/api/attention?project=switchboard").json()
    assert first["schema"] == "switchboard.attention_projection.v1"
    assert first["count"] == 5
    assert first["sources"] == {
        "provider": 1, "agent": 1, "inbox": 1, "mission": 1, "decision": 1,
    }
    assert len({item["source_id"] for item in first["items"]}) == first["count"]

    service.items = []  # authoritative provider transition: no shadow queue record remains
    second = client.get("/api/attention?project=switchboard").json()
    assert second["count"] == 4
    assert second["sources"]["provider"] == 0


if __name__ == "__main__":
    test_agent_item_carries_the_decide_contract()
    test_inbox_item_summarizes_triage_and_routes_to_confirm()
    test_rank_is_impact_then_downstream_then_deadline_then_age()
    test_rank_prefers_deadlines_then_breadth_within_a_source()
    test_projection_sources_have_stable_ids_links_blast_radius_and_evidence()
    test_projection_is_project_scoped_counts_sources_and_tracks_source_transition()
