"""Plan signals (Phase 3 — see docs/AGENT_ROADMAP.md).

Pure, derived health/triage over the LIVE board. No LLM. Feeds the By-person
"Next up", the agent's plan_signals tool, GET /api/signals, the MCP get_plan_signals
tool, and (later) the proactive digest. One source of truth for "what's slipping" and
"what should each person do next".
"""
import datetime

import store


def _date(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except Exception:
        return None


def _as_list(value):
    if isinstance(value, list):
        return value
    return []


def _task_deps(t):
    deps = t.get("depends_on") or []
    if isinstance(deps, list):
        return [d for d in deps if isinstance(d, str) and d]
    if isinstance(deps, str):
        return [d for d in deps.replace(",", " ").split() if d]
    return []


def _brief(t):
    return {"task_id": t["task_id"], "title": t.get("title"), "workstream": t.get("_wsId"),
            "status": t.get("status"), "owner_org": t.get("owner_org"),
            "owner_person_or_role": t.get("owner_person_or_role"),
            "finish_date": t.get("finish_date"), "is_blocking": t.get("is_blocking"),
            "depends_on": _task_deps(t)}


def _people_of(t, people):
    owner = (t.get("owner_person_or_role") or "").lower()
    if not owner:
        return ["Unassigned"]
    m = [p for p in people if p.lower() in owner]
    return m or ["Unassigned"]


def compute_plan_signals(due_soon_days: int = 7, project: str = "maxwell") -> dict:
    tasks = store.list_tasks(project=project)
    by_id = {t["task_id"]: t for t in tasks}
    today = datetime.date.today()

    def is_done(t):
        return t.get("status") == "Done"

    def deps_done(t):
        return all(by_id.get(d, {}).get("status") == "Done" for d in _task_deps(t))

    def is_actionable(t):
        # Something the owner can actually pick up now.
        if t.get("status") == "In Progress":
            return True
        return t.get("status") == "Not Started" and deps_done(t)

    overdue, due_soon, blocked, ready, waiting = [], [], [], [], []
    for t in tasks:
        if is_done(t):
            continue
        fd = _date(t.get("finish_date"))
        if t.get("status") == "Blocked":
            blocked.append(t)
        if fd and fd < today:
            overdue.append(t)
        elif fd and (fd - today).days <= due_soon_days:
            due_soon.append(t)
        if t.get("status") == "Not Started" and deps_done(t):
            ready.append(t)
        elif t.get("status") == "Not Started" and not deps_done(t):
            waiting.append(t)

    # Critical-path slip: critical-path tasks that are overdue or blocked.
    cp_ids = set()
    for c in _as_list(store.get_meta("critical_path", project=project)):
        if isinstance(c, dict):
            task_id = c.get("task_id")
        elif isinstance(c, str):
            task_id = c
        else:
            task_id = None
        if task_id:
            cp_ids.add(task_id)
    critical_slip = [t for t in tasks if t["task_id"] in cp_ids and not is_done(t)
                     and (t.get("status") == "Blocked"
                          or (_date(t.get("finish_date")) and _date(t.get("finish_date")) < today))]

    # Past-due decisions (only when needed_by is a real date).
    past_due_decisions = []
    for d in _as_list(store.get_meta("consolidated_decisions", project=project)):
        if not isinstance(d, dict):
            continue
        nb = _date(d.get("needed_by"))
        if nb and nb < today:
            past_due_decisions.append({"question": d.get("question"), "owner": d.get("owner"),
                                       "needed_by": d.get("needed_by"), "workstream": d.get("workstream")})

    # Next-best per owner: rank the ACTIONABLE tasks each person owns.
    people = [p for p in _as_list(store.get_meta("people", project=project)) if isinstance(p, str)]
    if not people:
        people = store.DEFAULT_PEOPLE

    def score(t):
        fd = _date(t.get("finish_date"))
        s = 0
        if fd and fd < today:
            s += 1000 + (today - fd).days
        if t.get("is_blocking"):
            s += 500
        if t.get("status") == "In Progress":
            s += 200
        if fd and 0 <= (fd - today).days <= due_soon_days:
            s += 100
        return s

    by_owner_next = {}
    for t in tasks:
        if is_done(t) or not is_actionable(t):
            continue
        for owner in _people_of(t, people):
            by_owner_next.setdefault(owner, []).append(t)
    for owner in list(by_owner_next):
        ranked = sorted(by_owner_next[owner], key=score, reverse=True)
        by_owner_next[owner] = [_brief(x) for x in ranked[:2]]

    def briefs(lst, key=lambda t: t.get("finish_date") or "9999"):
        return [_brief(t) for t in sorted(lst, key=key)]

    return {
        "as_of": today.isoformat(),
        "counts": {"overdue": len(overdue), "due_soon": len(due_soon), "blocked": len(blocked),
                   "ready": len(ready), "waiting_on_deps": len(waiting),
                   "critical_slip": len(critical_slip), "past_due_decisions": len(past_due_decisions)},
        "overdue": briefs(overdue),
        "due_soon": briefs(due_soon),
        "blocked": briefs(blocked),
        "ready": briefs(ready),
        "critical_slip": briefs(critical_slip),
        "past_due_decisions": past_due_decisions,
        "by_owner_next": by_owner_next,
    }
