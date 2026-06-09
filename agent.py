"""Slim per-task ReAct agent (ADR 0007) — runs inside the satellite, calls the
shared LLM gateway. Tools: doc_search (RAG over plan docs) + propose_task_update
(propose-then-confirm; never applies a change directly). Synchronous; the app
calls it via asyncio.to_thread so it doesn't block the event loop.
"""
import json
import os

import httpx

import rag

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
CHAT_MODEL = os.environ.get("PM_LLM_CHAT_MODEL", "taikun-chat")
MAX_ITERS = 5

TOOLS = [
    {"type": "function", "function": {
        "name": "doc_search",
        "description": "Search the TEEP Barnett plan docs (PRD, architecture, system-integrations, "
                       "security, asset-binding, the full project plan) for grounding. Use this before "
                       "asserting any fact about the project, dependencies, owners, or approach.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "propose_task_update",
        "description": "Propose a change to THIS task for the user to confirm. Does NOT apply it — the "
                       "user must click Confirm. Only include fields you actually want to change.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "Blocked", "Done"]},
            "assignee": {"type": "string"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "finish_date": {"type": "string", "description": "YYYY-MM-DD"},
            "rationale": {"type": "string", "description": "one short line on why"}},
            "required": ["rationale"]}}},
]


def _system(task):
    deps = ", ".join(task.get("depends_on") or []) or "none"
    return (
        "You are Maxwell, an assistant embedded in the TEEP Barnett project board, scoped to ONE task.\n"
        f"Task {task['task_id']}: {task.get('title')}\n"
        f"Workstream {task.get('_wsId')} ({task.get('_wsName')}) · phase {task.get('phase')} · "
        f"owner {task.get('owner_org')}/{task.get('owner_person_or_role')} · assignee {task.get('assignee') or 'unassigned'} · "
        f"status {task.get('status')} · {task.get('start_date')}..{task.get('finish_date')} · depends on: {deps}.\n\n"
        "Help the operator move this task forward and answer questions about it. ALWAYS ground claims about "
        "the project in the plan via doc_search before stating them. To change the task, call "
        "propose_task_update — the user must confirm; never say a change has been applied. Be concise and "
        "operator-friendly; cite the doc you used when relevant."
    )


def _chat(messages):
    # No temperature: gpt-5.x only supports the default (1). Add back only for models that allow it.
    body = {"model": CHAT_MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto"}
    r = httpx.post(f"{BASE}/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def run(task, message, history=None):
    msgs = [{"role": "system", "content": _system(task)}]
    for h in (history or []):
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": message})

    sources, proposal, last = [], None, None
    for _ in range(MAX_ITERS):
        m = _chat(msgs)
        last = m
        tcs = m.get("tool_calls")
        if not tcs:
            return {"answer": m.get("content") or "", "proposal": proposal, "sources": list(dict.fromkeys(sources))}
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": tcs})
        for tc in tcs:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            if name == "doc_search":
                hits = rag.search(args.get("query", ""), top_k=5)
                sources += [h["file"] for h in hits]
                content = "\n\n".join(f"[{h['file']}] {h['text']}" for h in hits) or "no matches"
            elif name == "propose_task_update":
                proposal = {k: v for k, v in args.items()
                            if k in ("status", "assignee", "start_date", "finish_date", "rationale") and v not in (None, "")}
                content = "Proposal recorded; tell the user what you propose and that they must Confirm it."
            else:
                content = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
    return {"answer": (last or {}).get("content") or "(reached step limit)", "proposal": proposal,
            "sources": list(dict.fromkeys(sources))}
