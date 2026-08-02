"""UI-85: Scope is a normal read-only project chat on GPT-5.6 Sol/high."""
import unittest
from unittest import mock

from path_setup import ROOT

import agent  # noqa: E402
import background_jobs  # noqa: E402


class ScopeSolChatTests(unittest.TestCase):
    def test_chat_sends_reasoning_effort_to_gateway(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        with mock.patch.object(agent.httpx, "post", return_value=response) as post:
            agent._chat(
                [{"role": "user", "content": "hello"}],
                tools=[{"type": "function", "function": {
                    "name": "lookup", "parameters": {"type": "object", "properties": {}}
                }}],
                model="taikun-scope",
                reasoning_effort="high",
            )

        body = post.call_args.kwargs["json"]
        self.assertEqual("taikun-scope", body["model"])
        self.assertEqual("high", body["reasoning_effort"])

    def test_scope_chat_uses_sol_high_and_only_read_tools(self):
        calls = []

        def fake_chat(messages, **kwargs):
            calls.append((messages, kwargs))
            return {"role": "assistant", "content": "A direct project answer."}

        with (mock.patch.object(agent, "_chat", side_effect=fake_chat),
              mock.patch.object(agent.store, "projects", return_value=[
                  {"id": "switchboard", "label": "Switchboard",
                   "purpose": "coordinate project work"}
              ]),
              mock.patch.object(agent.store, "list_tasks_for_board", return_value=[])):
            result = agent.run_project_chat("What should we decide?", project="switchboard")

        self.assertEqual("A direct project answer.", result["answer"])
        messages, kwargs = calls[0]
        self.assertEqual("taikun-scope", kwargs["model"])
        self.assertEqual("high", kwargs["reasoning_effort"])
        self.assertIn("Answer naturally and directly", messages[0]["content"])
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        self.assertEqual(
            {"get_project_contract", "doc_search", "search_tasks", "get_task", "plan_signals"},
            names,
        )

    def test_scope_session_uses_normal_project_chat(self):
        seen = {}

        def fake_project_chat(question, history=None, project="maxwell"):
            seen.update(question=question, history=history, project=project)
            return {"answer": "hello", "sources": []}

        with mock.patch.object(agent, "run_project_chat", side_effect=fake_project_chat):
            result = background_jobs._step_plan_agent("switchboard", {
                "question": "Hello project",
                "history": [{"role": "user", "content": "Earlier"}],
                "session": "scope",
                "record_chat": False,
            })

        self.assertEqual("hello", result["answer"])
        self.assertEqual({
            "question": "Hello project",
            "history": [{"role": "user", "content": "Earlier"}],
            "project": "switchboard",
        }, seen)

    def test_gateway_has_scope_sol_alias(self):
        text = (ROOT / "deploy/gateway/config.yaml").read_text(encoding="utf-8")
        self.assertIn("model_name: taikun-scope", text)
        self.assertIn("model: openai/gpt-5.6-sol", text)


if __name__ == "__main__":
    unittest.main()
