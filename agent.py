"""Slim per-task ReAct agent (ADR 0007) — runs inside the satellite, calls the
shared LLM gateway. Its tools are derived from the selected project's corpus,
contract, and live board. Web and MCP callers run it through the persisted
background-job adapter; proposals are never applied without confirmation.
"""
import copy
import datetime
import json
import os
import time

import httpx

import rag
import project_contract
import signals
import store

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
CHAT_MODEL = os.environ.get("PM_LLM_CHAT_MODEL", "taikun-chat")
# Tool-loop budgets. Interactive chat stays snappy at 6; inbound triage (a call transcript,
# forwarded thread, or document touching many tasks) needs more grounding+propose turns, so it
# gets a larger budget. Both are env-overridable for tuning without a redeploy.
MAX_ITERS = int(os.environ.get("PM_AGENT_ITERS", "6"))
TRIAGE_ITERS = int(os.environ.get("PM_TRIAGE_ITERS", "14"))

TOOLS = [
    {"type": "function", "function": {
        "name": "doc_search",
        "description": "Search the selected project's segmented corpus for grounding before asserting "
                       "facts about scope, dependencies, owners, decisions, or approach.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_tasks",
        "description": "Filter the LIVE plan's tasks. Returns id/title/status/owner/workstream/dates for "
                       "matches. Use to find tasks by workstream, status, owner person, blocking flag, or text.",
        "parameters": {"type": "object", "properties": {
            "workstream": {"type": "string", "description": "workstream id on the selected project"},
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "In Review", "Blocked", "Done"]},
            "owner_person": {"type": "string", "description": "substring match on owner_person_or_role"},
            "blocking": {"type": "boolean"},
            "query": {"type": "string", "description": "free-text match on title/description/owner"}}}}},
    {"type": "function", "function": {
        "name": "get_task",
        "description": "Get the FULL detail of one task by id: description, all fields, and recent activity.",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "plan_signals",
        "description": "Get derived plan health: counts + lists of overdue / due-soon / blocked / ready tasks, "
                       "critical-path slips, past-due decisions, and each owner's next-best 1-2 tasks. Use for "
                       "'what's slipping?', 'what should X do next?', risk summaries, or digests.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_task_update",
        "description": "Propose a change to a task for the user to confirm. Does NOT apply it — the user must "
                       "click Confirm. Include task_id (REQUIRED in plan-wide chat; optional when scoped to one "
                       "task). Only include fields you actually want to change.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "which task to change (required in plan-wide chat)"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "In Review", "Blocked", "Done"]},
            "assignee": {"type": "string"},
            "owner_org": {"type": "string"},
            "owner_person_or_role": {"type": "string"},
            "phase": {"type": "string"},
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
    {"type": "function", "function": {
        "name": "propose_bulk_update",
        "description": "Propose the SAME field change to MULTIPLE tasks at once (e.g. mark several Done). "
                       "Gather the exact task_ids first (from the board list or search_tasks). Each task "
                       "becomes a separate confirmable proposal. Does NOT apply — the user confirms.",
        "parameters": {"type": "object", "properties": {
            "task_ids": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": ["Not Started", "In Progress", "In Review", "Blocked", "Done"]},
            "owner_org": {"type": "string"},
            "owner_person_or_role": {"type": "string"},
            "assignee": {"type": "string"},
            "phase": {"type": "string"},
            "risk_level": {"type": "string", "enum": ["Low", "Medium", "High"]},
            "is_blocking": {"type": "boolean"},
            "rationale": {"type": "string", "description": "one short line on why"}},
            "required": ["task_ids", "rationale"]}}},
    {"type": "function", "function": {
        "name": "propose_date_shift",
        "description": "Propose shifting the start AND finish dates of MULTIPLE tasks by N days (e.g. 'push "
                       "every Bedrock task out a week' = days 7). The server computes each task's new dates. "
                       "Each becomes a confirmable proposal. Does NOT apply — the user confirms.",
        "parameters": {"type": "object", "properties": {
            "task_ids": {"type": "array", "items": {"type": "string"}},
            "days": {"type": "integer", "description": "+N moves later, -N earlier"},
            "rationale": {"type": "string", "description": "one short line on why"}},
            "required": ["task_ids", "days", "rationale"]}}},
    {"type": "function", "function": {
        "name": "propose_new_task",
        "description": "Propose creating a NEW task. Does NOT create it — the user confirms. "
                       "workstream_id must be an existing workstream on the selected project.",
        "parameters": {"type": "object", "properties": {
            "workstream_id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "owner_org": {"type": "string"},
            "owner_person_or_role": {"type": "string"},
            "phase": {"type": "string"},
            "risk_level": {"type": "string", "enum": ["Low", "Medium", "High"]},
            "rationale": {"type": "string", "description": "one short line on why"}},
            "required": ["workstream_id", "title", "rationale"]}}},
    {"type": "function", "function": {
        "name": "set_recipients",
        "description": "Set WHO your email reply goes to (inbound-message handling only). Call this when the "
                       "message tells you to send to / copy specific people (e.g. 'send this to Sahir, cc Darko "
                       "and me') or to reply to everyone. Provide EMAIL ADDRESSES — resolve names using KNOWN "
                       "CONTACTS and the thread's From/To/Cc shown in your prompt. If you do NOT call this, the "
                       "reply defaults to everyone already on the thread (reply-all). The sender is always kept "
                       "copied, so you don't need to add them just to 'copy me'.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "array", "items": {"type": "string"},
                   "description": "primary recipient email addresses"},
            "cc": {"type": "array", "items": {"type": "string"},
                   "description": "cc recipient email addresses"}},
            "required": ["to"]}}},
    {"type": "function", "function": {
        "name": "dispatch_to_dev",
        "description": "Hand a task to the Claude Code DEVELOPER agent to actually build or fix it. The dev "
                       "agent makes the code change on a branch and opens a PR — it never merges to main. "
                       "Call this ONLY when the message EXPLICITLY asks for a Claude Code / dev-agent dispatch "
                       "(e.g. 'have Claude Code build this', 'dispatch the dev agent to fix SEN-6', 'get the "
                       "developer on this'). Pass the existing task_id; OMIT task_id to dispatch the NEW task "
                       "you just proposed in this same reply. Do NOT call it for status / FYI / info emails.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "existing task id; omit/empty to dispatch the new task proposed here"}},
            "required": []}}},
]


def _tool(tools, name):
    return next(item for item in tools if item["function"]["name"] == name)


def tools_for_project(project="maxwell"):
    """Build the model tool catalog from the selected project's live board."""
    tools = copy.deepcopy(TOOLS)
    tasks = store.list_tasks_for_board(project=project)
    project_item = next((item for item in store.projects() if item["id"] == project), {})
    label = project_item.get("label") or project
    workstreams = sorted({item.get("_wsId") for item in tasks if item.get("_wsId")})
    owners = sorted({item.get("owner_org") for item in tasks if item.get("owner_org")})
    phases = sorted({item.get("phase") for item in tasks if item.get("phase")})

    _tool(tools, "doc_search")["function"]["description"] = (
        f"Search the segmented corpus for {label}. Use this before asserting project facts, "
        "decisions, dependencies, owners, scope, milestones, or approach."
    )
    search_properties = _tool(tools, "search_tasks")["function"]["parameters"]["properties"]
    search_properties["workstream"]["description"] = (
        "workstream id" + (f"; current values: {', '.join(workstreams)}" if workstreams else "")
    )
    if workstreams:
        search_properties["workstream"]["enum"] = workstreams

    for tool_name in ("propose_task_update", "propose_bulk_update", "propose_new_task"):
        properties = _tool(tools, tool_name)["function"]["parameters"]["properties"]
        if owners and "owner_org" in properties:
            properties["owner_org"]["enum"] = owners
        elif "owner_org" in properties:
            properties["owner_org"].pop("enum", None)
        if phases and "phase" in properties:
            properties["phase"]["enum"] = phases
        elif "phase" in properties:
            properties["phase"].pop("enum", None)
    create_tool = _tool(tools, "propose_new_task")["function"]
    create_tool["description"] = (
        "Propose creating a NEW task. It is not created until the user confirms. "
        "workstream_id must identify an existing workstream"
        + (f": {', '.join(workstreams)}." if workstreams else ".")
    )
    if workstreams:
        create_tool["parameters"]["properties"]["workstream_id"]["enum"] = workstreams

    tools.insert(0, {"type": "function", "function": {
        "name": "get_project_contract",
        "description": (
            f"Read the canonical Switchboard project boundary and operating contract for {label}: "
            "purpose, repository roles, policies, boards, and work-session rules."
        ),
        "parameters": {"type": "object", "properties": {}},
    }})
    return tools

# Editable fields the agent may propose (mirrors store.EDITABLE minus internal ones).
_PROPOSABLE = ["title", "description", "status", "assignee", "owner_org", "owner_person_or_role",
               "phase", "effort_days", "start_date", "finish_date", "risk_level", "is_blocking",
               "entry_criteria", "exit_criteria", "deliverable"]


def _project_voice(project: str):
    project_item = next((item for item in store.projects() if item["id"] == project), {})
    label = project_item.get("label") or project
    purpose = project_item.get("purpose") or store.project_access(project).get("purpose") or "its project outcomes"
    if project == "helm":
        return {
            "who": "the Helm assistant",
            "board": "the Helm marine-chartplotter board",
            "ground": ("Ground claims in this task's description + activity and the Helm corpus; "
                       "read file-level evidence via get_task and use doc_search for Helm artifacts."),
            "global_ground": ("Ground every answer in the BOARD above — it carries code-audit comments "
                              "with file-level evidence (read them via get_task) — and the Helm corpus. "
                              "Helm tasks have no dates, so ignore overdue/schedule questions."),
        }
    if project == "switchboard":
        return {
            "who": "Switchboard",
            "board": "the Switchboard dogfood board for the agent coordination layer",
            "ground": ("Ground claims in this task's description, activity, live board state, and the "
                       "Switchboard corpus."),
            "global_ground": ("Ground every answer in the BOARD above and task activity (read full tasks "
                              "via get_task), the project contract, and the Switchboard corpus."),
        }
    if project == "maxwell":
        return {
            "who": "Maxwell",
            "board": "the TEEP Barnett project board",
            "ground": "Ground project claims in the Maxwell corpus before stating them.",
            "global_ground": ("Ground project claims in the Maxwell corpus and project contract. "
                              "Overdue = finish_date before today and status not Done."),
        }
    return {
        "who": f"the {label} project agent",
        "board": f"the {label} project board",
        "ground": (f"Ground claims in this project's corpus, live task evidence, and project contract. "
                   f"The project purpose is: {purpose}."),
        "global_ground": ("Ground every answer in the selected project's corpus, live board/task activity, "
                          "and project contract. Never import assumptions from another project."),
    }


def _system(task, project="maxwell"):
    deps = ", ".join(task.get("depends_on") or []) or "none"
    voice = _project_voice(project)
    return (
        f"You are {voice['who']}, an assistant embedded in {voice['board']}, scoped to ONE task.\n"
        f"Task {task['task_id']}: {task.get('title')}\n"
        f"Workstream {task.get('_wsId')} ({task.get('_wsName')}) · phase {task.get('phase')} · "
        f"owner {task.get('owner_org')}/{task.get('owner_person_or_role')} · assignee {task.get('assignee') or 'unassigned'} · "
        f"status {task.get('status')} · {task.get('start_date')}..{task.get('finish_date')} · depends on: {deps}.\n\n"
        f"Help the operator move this task forward and answer questions about it. {voice['ground']} To change the "
        "task, call propose_task_update — the user must confirm; never say a change has been applied. Be "
        "concise and operator-friendly; cite the source you used when relevant."
    )


def _chat(messages, tool_choice="auto", meta=None, tools=None):
    # No temperature: gpt-5.x only supports the default (1). Add back only for models that allow it.
    # tool_choice="none" forces a tool-free turn (used to flush a final summary when out of steps).
    body = {"model": CHAT_MODEL, "messages": messages, "tools": tools or TOOLS,
            "tool_choice": tool_choice}
    if meta:
        # UI-12: attribute this gateway call's spend (source=agent, + task/project when scoped).
        body["metadata"] = meta
    r = httpx.post(f"{BASE}/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


# ---- plan-wide (global) context ------------------------------------------
def board_summary_text(project="maxwell"):
    """One compact line per task — the whole plan at a glance for the system prompt."""
    lines = []
    for t in store.list_tasks_for_board(project=project):
        deps = ",".join(t.get("depends_on") or [])
        flags = ("; BLOCKING" if t.get("is_blocking") else "") + ("; deps " + deps if deps else "")
        lines.append(
            f"{t['task_id']} [{t.get('_wsId')}] {t.get('status')} :: {t.get('title')} "
            f"(owner {t.get('owner_org')}/{t.get('owner_person_or_role')}; "
            f"{t.get('start_date')}..{t.get('finish_date')}{flags})")
    return "\n".join(lines)


def _system_global(project="maxwell"):
    today = time.strftime("%Y-%m-%d")
    proj = store.get_meta("project", project=project) or "the plan"
    voice = _project_voice(project)
    return (
        f"You are {voice['who']}, the assistant for {proj}, with visibility into the ENTIRE plan. "
        f"Today is {today}.\n\n"
        "CURRENT BOARD — one line per task: ID [workstream] status :: title (owner; start..finish; flags):\n"
        f"{board_summary_text(project=project)}\n\n"
        "Answer questions about the whole plan (blockers, owners, what's ready vs blocked, what changed). "
        f"{voice['global_ground']} Use get_project_contract for project boundaries and policies, get_task for a "
        "task's full description + activity, and search_tasks to filter. To "
        "change a task, call propose_task_update WITH its task_id — the user must Confirm; NEVER say a "
        "change was applied. Be concise and operator-friendly."
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
        b["rationale"] = t.get("rationale")  # Haiku-generated activity summary; None until first run
        if t.get("dependency_state") is not None:
            b["dependency_state"] = t.get("dependency_state")
        if t.get("rationale_state") is not None:
            b["rationale_state"] = t.get("rationale_state")
        if t.get("rationale_raw") is not None:
            b["rationale_raw"] = t.get("rationale_raw")
        for key in ("git_state", "provenance", "external_ci", "active_claims", "identity",
                    "human_gate", "session_health", "terminal_state", "project_context",
                    "publication"):
            if t.get(key) is not None:
                b[key] = t.get(key)
        recent = []
        for a in (t.get("activity") or [])[-6:]:
            payload = a.get("payload") or {}
            text = payload.get("text") if isinstance(payload, dict) else payload
            recent.append({"actor": a.get("actor"), "kind": a.get("kind"),
                           "text": text if text is not None else payload})
        b["recent_activity"] = recent
    return b


def _search_tasks(args, project="maxwell"):
    owner = (args.get("owner_person") or "").lower()
    blocking = args.get("blocking")
    q = (args.get("query") or "").lower()
    out = []
    for t in store.list_tasks_slim(
            workstream=args.get("workstream") or None,
            status=args.get("status") or None,
            project=project):
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


def run(task, message, history=None, system=None, max_iters=None, project="maxwell"):
    """task=None runs the PLAN-WIDE agent; a task dict runs the per-task agent; pass
    `system` to override the prompt (used by triage). `max_iters` overrides the tool-loop
    budget — triage passes a larger one since inbound calls/threads need more grounding turns.
    `project` selects the board the plan-wide agent reads/proposes against ('maxwell' default,
    'helm', or 'switchboard')."""
    if system is None:
        system = _system(task, project) if task else _system_global(project)
    project_tools = tools_for_project(project)
    iters = max_iters or MAX_ITERS
    msgs = [{"role": "system", "content": system}]
    for h in (history or []):
        if h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": message})

    chat_meta = {"source": "agent", "project": project}
    if task and task.get("task_id"):
        chat_meta["task_id"] = task["task_id"]

    sources, proposals, new_tasks, last = [], [], [], None
    recipients = None
    dispatch_targets = []
    for i in range(iters):
        # On the final budgeted turn, stop the model from spending it on yet another search:
        # tell it to commit any remaining proposals and wrap up. Without this, a long artifact
        # (e.g. a call transcript touching many tasks) burns the whole budget grounding and
        # returns "(reached step limit)" with zero proposals.
        if i == iters - 1:
            msgs.append({"role": "system", "content":
                         "You are on your LAST step. Do NOT call read-only tools (doc_search/"
                         "search_tasks/get_task/plan_signals) again. Make any remaining propose_* "
                         "(and set_recipients/dispatch_to_dev) calls the message clearly implies, "
                         "then write your final summary."})
        m = _chat(msgs, meta=chat_meta, tools=project_tools)
        last = m
        tcs = m.get("tool_calls")
        if not tcs:
            return _result(m.get("content") or "", proposals, new_tasks, sources, recipients, dispatch_targets)
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": tcs})
        for tc in tcs:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            if name == "get_project_contract":
                content = json.dumps(project_contract.build(project))
            elif name == "doc_search":
                hits = rag.search(args.get("query", ""), top_k=5, project=project)
                sources += [h["file"] for h in hits]
                content = "\n\n".join(f"[{h['file']}] {h['text']}" for h in hits) or "no matches"
            elif name == "search_tasks":
                content = json.dumps(_search_tasks(args, project))
            elif name == "get_task":
                t = store.get_task(args.get("task_id", ""), project=project)
                content = json.dumps(_task_brief(t, full=True)) if t else "no such task"
            elif name == "plan_signals":
                # compute_plan_signals is now cached (HARDEN-36) — copy before trimming
                # so this tool's 15-item cap can't truncate the shared cached lists.
                sig = dict(signals.compute_plan_signals(project=project))
                for k in ("overdue", "due_soon", "blocked", "ready", "critical_slip"):
                    sig[k] = sig[k][:15]
                content = json.dumps(sig)
            elif name == "propose_task_update":
                tid = args.get("task_id") or (task and task.get("task_id"))
                if not tid:
                    content = "Specify task_id to propose a change."
                elif not store.get_task(tid, project=project):
                    content = f"No task {tid}."
                else:
                    prop = {k: v for k, v in args.items()
                            if k in (_PROPOSABLE + ["rationale"]) and v not in (None, "")}
                    prop["task_id"] = tid
                    proposals.append(prop)
                    content = f"Proposal for {tid} recorded ({len(proposals)} pending); tell the user to Confirm."
            elif name == "propose_bulk_update":
                setf = {k: v for k, v in args.items() if k in _PROPOSABLE and v not in (None, "")}
                rat = args.get("rationale") or "bulk update"
                n = 0
                for tid in (args.get("task_ids") or []):
                    if not store.get_task(tid, project=project):
                        continue
                    prop = dict(setf)
                    prop["task_id"] = tid
                    prop["rationale"] = rat
                    proposals.append(prop)
                    n += 1
                content = f"Proposed {n} updates ({len(proposals)} pending); tell the user to Confirm all."
            elif name == "propose_date_shift":
                days = int(args.get("days") or 0)
                rat = args.get("rationale") or f"shift {days:+d}d"
                n = 0
                for tid in (args.get("task_ids") or []):
                    t = store.get_task(tid, project=project)
                    if not t:
                        continue
                    prop = {"task_id": tid, "rationale": rat}
                    for f in ("start_date", "finish_date"):
                        d = t.get(f)
                        if d:
                            try:
                                prop[f] = (datetime.date.fromisoformat(d)
                                           + datetime.timedelta(days=days)).isoformat()
                            except Exception:
                                pass
                    if len(prop) > 2:  # at least one date shifted
                        proposals.append(prop)
                        n += 1
                content = f"Proposed a {days:+d}d shift on {n} tasks ({len(proposals)} pending); tell the user to Confirm all."
            elif name == "propose_new_task":
                if not (args.get("workstream_id") and args.get("title")):
                    content = "workstream_id and title are required."
                else:
                    nt = {k: v for k, v in args.items()
                          if k in ("workstream_id", "title", "description", "owner_org",
                                   "owner_person_or_role", "phase", "risk_level", "rationale")
                          and v not in (None, "")}
                    new_tasks.append(nt)
                    content = f"Proposed new task in {nt['workstream_id']} ({len(new_tasks)} pending); tell the user to Confirm."
            elif name == "set_recipients":
                to = [a.strip() for a in (args.get("to") or []) if a and a.strip()]
                cc = [a.strip() for a in (args.get("cc") or []) if a and a.strip()]
                recipients = {"to": to, "cc": cc}
                content = f"Reply recipients set — to: {to or '(none)'}; cc: {cc or '(none)'}."
            elif name == "dispatch_to_dev":
                tid = (args.get("task_id") or "").strip()
                dispatch_targets.append(tid or "NEW")
                content = (f"Queued a Claude Code dev dispatch for {tid}." if tid
                           else "Queued a Claude Code dev dispatch for the new task proposed here.")
            else:
                content = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
    # Budget exhausted with tool calls still pending. Force ONE tool-free closing turn so the user
    # always gets a real summary (and we keep whatever proposals were already staged), instead of
    # the old "(reached step limit)" dead-end that surfaced as "No task changes detected".
    answer = ""
    try:
        answer = (_chat(msgs, tool_choice="none", meta=chat_meta,
                        tools=project_tools).get("content") or "").strip()
    except Exception:
        pass
    if not answer:
        answer = ((last or {}).get("content") or "").strip()
    if not answer:
        answer = ("I reviewed this but ran out of analysis steps before finishing. "
                  + (f"I staged {len(proposals)} proposed change(s) — review them below, then re-send "
                     "or ask a focused follow-up so I can continue."
                     if proposals else
                     "No changes were staged yet — re-send or ask a focused follow-up so I can finish."))
    return _result(answer, proposals, new_tasks, sources, recipients, dispatch_targets)


def _result(answer, proposals, new_tasks, sources, recipients=None, dispatch_targets=None):
    return {"answer": answer, "proposals": proposals, "new_tasks": new_tasks,
            "proposal": (proposals[-1] if proposals else None),
            "recipients": recipients, "dispatch_targets": dispatch_targets or [],
            "sources": list(dict.fromkeys(sources))}


def _system_triage(applied_mode=False, headers=None, project="maxwell"):
    today = time.strftime("%Y-%m-%d")
    proj = store.get_meta("project", project=project) or project
    voice = _project_voice(project)
    contacts = (store.get_contacts() if project == "maxwell" else
                (store.get_meta("contacts", {}, project=project) or {}))
    contacts_text = ", ".join(f"{n} <{e}>" for e, n in
                              sorted(contacts.items(), key=lambda kv: (kv[1] or kv[0]))) or "(none)"
    h = headers or {}
    thread_text = ("\nTHIS MESSAGE'S HEADERS — From: %s | To: %s | Cc: %s\n"
                   % (h.get("from") or "?", h.get("to") or "-", h.get("cc") or "-")) if headers else ""
    frame = ("In this mode your changes are APPLIED IMMEDIATELY — you act autonomously, so write your reply in "
             "the PAST tense ('I've moved SEN-2 to In Progress', 'I closed GW-3'), never as a proposal.\n"
             if applied_mode else
             "These are PROPOSALS the user confirms; do not say a change was applied.\n")
    return (
        f"You are {voice['who']}, the autonomous PM agent for {proj}, handling an INBOUND MESSAGE "
        f"(an email, forwarded thread, transcript, or document). Today is {today}.\n\n"
        "CURRENT BOARD — one line per task: ID [workstream] status :: title (owner; start..finish; flags):\n"
        f"{board_summary_text(project)}\n\n"
        "Do BOTH, as warranted:\n"
        "1) ANSWER any question or request for info the message contains — directly and specifically, grounded "
        "in the board + docs (doc_search; the message itself is already indexed). \n"
        "2) MAKE the plan changes the message clearly and directly implies, via the propose_* tools.\n\n"
        "Reason carefully and DO NOT keyword-match:\n"
        "- The CURRENT BOARD above already lists EVERY task — use it to resolve which task(s) the message "
        "refers to; do NOT spend steps re-searching for tasks you can already see. Call get_task only when "
        "you need a task's full description / exit-criteria / activity to judge it, and doc_search only for "
        "plan facts not on the board. Spend the budget DECIDING and PROPOSING, not browsing.\n"
        "- Before CLOSING a task (status Done), read its exit_criteria/deliverable and confirm the message "
        "ACTUALLY satisfies them — 'sounds done' is not 'done'. If unsure which task or whether it's truly "
        "done, do NOT change it; ask in your reply.\n"
        "- Status -> propose_task_update; several -> propose_bulk_update; an explicit slip -> propose_date_shift; "
        "genuinely new work -> propose_new_task. Honor explicit instructions in the message.\n"
        "- If the message EXPLICITLY asks for a Claude Code / dev-agent dispatch ('have Claude Code build/fix "
        "this', 'dispatch the dev agent', 'get the developer on X'), call dispatch_to_dev — with the existing "
        "task_id, or no task_id to dispatch the new task you just proposed. It builds the change + opens a PR "
        "(never main). Do NOT dispatch for status/FYI emails or when not clearly asked.\n"
        "- BE CONSERVATIVE: change ONLY what the message clearly implies. Do NOT speculatively reschedule "
        "downstream/dependent tasks unless the message explicitly says to — instead MENTION the likely knock-on "
        "in your reply so a human can decide.\n\n"
        + frame +
        f"KNOWN CONTACTS (name <email>): {contacts_text}\n"
        + thread_text +
        "\nEMAIL REPLY: your summary is sent as the email reply. By DEFAULT it goes to everyone already on the "
        "thread (reply-all: the sender plus this message's To/Cc), and the original sender is ALWAYS kept "
        "copied. If the message asks you to send to / copy specific people (e.g. 'send this to Sahir, cc Darko "
        "and me'), call set_recipients with their EMAIL ADDRESSES, resolved from KNOWN CONTACTS / the headers "
        "above. The full prior message is auto-quoted beneath your reply, so do not restate it. Write a clear, "
        "direct reply: answer the question and state plainly what you did (or that nothing changed and why). "
        "2-5 sentences."
    )


def triage(kind, title, text, applied_mode=False, headers=None, project="maxwell"):
    """Triage an inbound artifact against the plan. Returns {answer(summary), proposals, new_tasks,
    recipients, sources}. headers={from,to,cc,date} lets the agent reply-all / route to named people.
    project scopes the grounding tools (doc_search / search_tasks / get_task) to that board."""
    artifact = f"INBOUND {kind.upper()}" + (f" — {title}" if title else "") + ":\n\n" + (text or "")
    return run(None, artifact, system=_system_triage(applied_mode, headers, project), max_iters=TRIAGE_ITERS,
               project=project)
