"""SEG-1: inbound prompts, contacts, branding, and calls stay project-scoped."""
from unittest.mock import Mock

import agent
import inbox
from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.repositories import activity


def _ctx(project: str, label: str) -> ProjectContext:
    return ProjectContext(project_id=project, source="test", label=label)


def test_two_project_prompt_snapshots_have_no_foreign_state(monkeypatch):
    prompts = []
    chat = Mock(side_effect=lambda messages, **_kwargs: (
        prompts.append(messages) or {"content": "handled", "tool_calls": []}
    ))
    metadata = {
        "alpha": {"project": "Alpha Board", "project_purpose": "Alpha rockets"},
        "beta": {"project": "Beta Board", "project_purpose": "Beta boats"},
    }
    contacts = {
        "alpha": {"alice@alpha.invalid": "Alice Alpha"},
        "beta": {"bob@beta.invalid": "Bob Beta"},
    }
    boards = {"alpha": "ALPHA-1 :: Alpha-only task", "beta": "BETA-1 :: Beta-only task"}

    monkeypatch.setattr(agent, "_chat", chat)
    monkeypatch.setattr(agent, "tools_for_project", lambda _project: [])
    monkeypatch.setattr(agent, "board_summary_text", lambda project: boards[project])
    monkeypatch.setattr(
        agent.store, "get_meta",
        lambda key, default=None, project="maxwell": metadata[project].get(key, default),
    )
    monkeypatch.setattr(agent.store, "get_contacts", lambda project="maxwell": contacts[project])

    for project, label in (("alpha", "Alpha Board"), ("beta", "Beta Board")):
        result = agent.triage(
            "email", "Status", f"message for {label}", headers={"from": "sender@example.test"},
            project_context=_ctx(project, label),
        )
        assert result["answer"] == "handled"

    assert chat.call_count == 2  # exactly one outbound LLM request per synthetic project
    alpha_request, beta_request = prompts
    alpha_text = "\n".join(str(message.get("content") or "") for message in alpha_request)
    beta_text = "\n".join(str(message.get("content") or "") for message in beta_request)
    assert "Alpha Board" in alpha_text and "ALPHA-1" in alpha_text and "Alice Alpha" in alpha_text
    assert "Beta Board" not in alpha_text and "BETA-1" not in alpha_text and "Bob Beta" not in alpha_text
    assert "Beta Board" in beta_text and "BETA-1" in beta_text and "Bob Beta" in beta_text
    assert "Alpha Board" not in beta_text and "ALPHA-1" not in beta_text and "Alice Alpha" not in beta_text


def test_contacts_and_reply_branding_use_the_same_context(monkeypatch):
    writes = []
    monkeypatch.setattr(
        inbox.store, "upsert_contact",
        lambda email, name, project="maxwell": writes.append((project, email, name)),
    )
    monkeypatch.setattr(
        inbox.store, "get_meta",
        lambda key, default=None, project="maxwell": {"alpha": "Alpha Board", "beta": "Beta Board"}.get(project, default),
    )

    alpha = _ctx("alpha", "Alpha Board")
    beta = _ctx("beta", "Beta Board")
    inbox._learn_contacts(alpha, "Alice <alice@alpha.invalid>")
    inbox._learn_contacts(beta, "Bob <bob@beta.invalid>")

    assert writes == [
        ("alpha", "alice@alpha.invalid", "Alice"),
        ("beta", "bob@beta.invalid", "Bob"),
    ]
    alpha_reply = inbox._compose_reply("Done", {}, alpha)
    beta_reply = inbox._compose_reply("Done", {}, beta)
    assert "Alpha Board" in alpha_reply and "Beta Board" not in alpha_reply and "Maxwell" not in alpha_reply
    assert "Beta Board" in beta_reply and "Alpha Board" not in beta_reply and "Maxwell" not in beta_reply


def test_empty_project_contacts_are_a_dict_and_render_as_none(monkeypatch):
    monkeypatch.setattr(activity, "get_meta", lambda *_args, **_kwargs: None)
    assert activity.get_contacts(project="empty-project") == {}

    monkeypatch.setattr(agent.store, "get_contacts", lambda project="maxwell": None)
    monkeypatch.setattr(agent.store, "get_meta", lambda key, default=None, project="maxwell": project)
    monkeypatch.setattr(agent, "board_summary_text", lambda _project: "")
    prompt = agent._system_triage(project="empty-project")
    assert "KNOWN CONTACTS (name <email>): (none)" in prompt
