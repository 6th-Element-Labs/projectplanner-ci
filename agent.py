"""Slim per-task ReAct agent (ADR 0007) — runs inside the satellite, calls the
shared LLM gateway. Tools: doc_search (RAG over plan docs) + propose_task_update
(propose-then-confirm; never applies a change directly). Synchronous; the app
calls it via asyncio.to_thread so it doesn't block the event loop.
"""
import json
import os
import time

import httpx

import rag
import store

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
CHAT_MODEL = os.environ.get("PM_LLM_CHAT_MODEL", "taikun-chat")
MAX_ITERS = 6

TOOLS = [
    {"type": "function", "function": {
        "name": "doc_search",
        "description": "Search the TEEP Barnett plan docs (PRD, architecture, system-integrations, "
                       "security, asset-binding, the full project plan) for grounding. Use this before "
                       "asserting any fact about the project, dependencies, owners, or approach.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_tasks",
        "description": "Filter the LIVE plan's tasks. Returns id/title/status/owner/workstream/dates for "
                       "matches. Use to find tasks by workstream, status, owner person, blocking flag, or text.",
        "parameters": {"type": "object", "properties": {
            "workstream": {"type": "string", "description": "workstream id, e.g. SSO, SEN, BEDROCK, GW"},
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "Blocked", "Done"]},
            "owner_person": {"type": "string", "description": "substring match on owner_person_or_role"},
            "blocking": {"type": "boolean"},
            "query": {"type": "string", "description": "free-text match on title/description/owner"}}}}},
    {"type": "function", "function": {
        "name": "get_task",
        "description": "Get the FULL detail of one task by id: description, all fields, and recent activity.",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "propose_task_update",
        "description": "Propose a change to a task for the user to confirm. Does NOT apply it — the user must "
                       "click Confirm. Include task_id (REQUIRED in plan-wide chat; optional when scoped to one "
                       "task). Only include fields you actually want to change.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "which task to change (required in plan-wide chat)"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "Blocked", "Done"]},
            "assignee": {"type": "string"},
            "owner_org": {"type": "string", "enum": ["Taikun", "TEEP", "Sensirion/Nubo", "IFS Merrick", "Joint"]},
            "owner_person_or_role": {"type": "string"},
            "phase": {"type": "string", "enum": ["Kickoff", "Bootstrap", "Build", "Cutover", "Operate"]},
            "effort_days": {"type": "number"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "finish_date": {"type": "string", "description": "YYYY-MM-DD"},
            "risk_level": {"type": "string", "enum": ["Low", "Medium", "High"]},
            "is_blocking": {"type": "boolean"},
            "entry_criteria": {"type": "string"},
            "exit_criteria": {"type": "string"},
            "deliverable": {"type": "string"},
            "rationale": {"type": "string", "description": "one short line on why"}},
            "required": ["rationale"]}}},
]

# Editable fields the agent may propose (mirrors store.EDITABLE minus internal ones).
_PROPOSABLE = ["title", "description", "status", "assignee", "owner_org", "owner_person_or_role",
               "phase", "effort_days", "start_date", "finish_date", "risk_level", "is_blocking",
               "entry_criteria", "exit_criteria", "deliverable"]


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


# ---- plan-wide (global) context ------------------------------------------
def board_summary_text():
    """One compact line per task — the whole plan at a glance for the system prompt."""
    lines = []
    for t in store.list_tasks():
        deps = ",".join(t.get("depends_on") or [])
        flags = ("; BLOCKING" if t.get("is_blocking") else "") + ("; deps " + deps if deps else "")
        lines.append(
            f"{t['task_id']} [{t.get('_wsId')}] {t.get('status')} :: {t.get('title')} "
            f"(owner {t.get('owner_org')}/{t.get('owner_person_or_role')}; "
            f"{t.get('start_date')}..{t.get('finish_date')}{flags})")
    return "\n".join(lines)


def _system_global():
    today = time.strftime("%Y-%m-%d")
    proj = store.get_meta("project") or "Project Maxwell"
    return (
        f"You are Maxwell, the assistant for {proj} (TEEP Barnett), with visibility into the ENTIRE plan. "
        f"Today is {today}.\n\n"
        "CURRENT BOARD — one line per task: ID [workstream] status :: title (owner; start..finish; flags):\n"
        f"{board_summary_text()}\n\n"
        "Answer questions about the whole plan (blockers, owners, risks, what's due/overdue, what changed). "
        "ALWAYS ground project claims in the plan docs via doc_search before asserting them. Use get_task for a "
        "task's full description + activity, and search_tasks to filter. To change a task, call "
        "propose_task_update WITH its task_id — the user must Confirm; NEVER say a change was applied. Be concise "
        "and operator-friendly; cite the doc when relevant. Overdue = finish_date before today and status not Done."
    )


def _task_brief(t, full=False):
    if not t:
        return None
    b = {"task_id": t["task_id"], "workstream": t.get("_wsId"), "title": t.get("title"),
         "status": t.get("status"), "owner_org": t.get("owner_org"),
         "owner_person_or_role": t.get("owner_person_or_role"), "assignee": t.get("assignee"),
         "phase": t.get("phase"), "start_date": t.get("start_date"), "finish_date": t.get("finish_date"),
         "is_blocking": t.get("is_blocking"), "depends_on": t.get("depends_on"), "risk_level": t.get("risk_level")}
    if full:
        b["description"] = t.get("description")
        b["entry_criteria"] = t.get("entry_criteria")
        b["exit_criteria"] = t.get("exit_criteria")
        b["deliverable"] = t.get("deliverable")
        b["recent_activity"] = [{"actor": a.get("actor"), "kind": a.get("kind"),
                                 "text": (a.get("payload") or {}).get("text") or (a.get("payload") or {})}
                                for a in (t.get("activity") or [])[-6:]]
    return b


def _search_tasks(args):
    owner = (args.get("owner_person") or "").lower()
    blocking = args.get("blocking")
    q = (args.get("query") or "").lower()
    out = []
    for t in store.list_tasks(workstream=args.get("workstream") or None, status=args.get("status") or None):
        if owner and owner not in (t.get("owner_person_or_role") or "").lower():
            continue
        if blocking and not t.get("is_blocking"):
            continue
        if q:
            hay = f"{t.get('task_id')} {t.get('title')} {t.get('description')} {t.get('owner_person_or_role')}".lower()
            if q not in hay:
                continue
        out.append(_task_brief(t))
    return out[:60]


def run(task, message, history=None):
    """task=None runs the PLAN-WIDE agent; a task dict runs the per-task agent."""
    system = _system(task) if task else _system_global()
    msgs = [{"role": "system", "content": system}]
    for h in (history or []):
        if h.get("content"):
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
            elif name == "search_tasks":
                content = json.dumps(_search_tasks(args))
            elif name == "get_task":
                t = store.get_task(args.get("task_id", ""))
                content = json.dumps(_task_brief(t, full=True)) if t else "no such task"
            elif name == "propose_task_update":
                tid = args.get("task_id") or (task and task.get("task_id"))
                if not tid:
                    content = "Specify task_id to propose a change."
                elif not store.get_task(tid):
                    content = f"No task {tid}."
                else:
                    proposal = {k: v for k, v in args.items()
                                if k in (_PROPOSABLE + ["rationale"]) and v not in (None, "")}
                    proposal["task_id"] = tid
                    content = f"Proposal for {tid} recorded; tell the user what you propose and that they must Confirm."
            else:
                content = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
    return {"answer": (last or {}).get("content") or "(reached step limit)", "proposal": proposal,
            "sources": list(dict.fromkeys(sources))}
