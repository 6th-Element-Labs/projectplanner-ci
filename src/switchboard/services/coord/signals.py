"""Pure plan-signal projection over an injected repository-shaped reader."""
from __future__ import annotations

import datetime
from typing import Any, Protocol


DEFAULT_PEOPLE = ["Steve Ridder", "Taikun eng", "Darko", "Sahir", "Sebastian", "Mike"]


class SignalDataPort(Protocol):
    def list_tasks(self, project: str) -> list[dict[str, Any]]: ...

    def get_meta(self, key: str, default: Any, project: str) -> Any: ...


def _date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value) if value else None
    except Exception:
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _task_deps(task: dict[str, Any]) -> list[str]:
    deps = task.get("depends_on") or []
    if isinstance(deps, list):
        return [item for item in deps if isinstance(item, str) and item]
    if isinstance(deps, str):
        return [item for item in deps.replace(",", " ").split() if item]
    return []


def _brief(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "title": task.get("title"),
        "workstream": task.get("_wsId"),
        "status": task.get("status"),
        "owner_org": task.get("owner_org"),
        "owner_person_or_role": task.get("owner_person_or_role"),
        "finish_date": task.get("finish_date"),
        "is_blocking": task.get("is_blocking"),
        "depends_on": _task_deps(task),
    }


def _people_of(task: dict[str, Any], people: list[str]) -> list[str]:
    owner = (task.get("owner_person_or_role") or "").lower()
    if not owner:
        return ["Unassigned"]
    matches = [person for person in people if person.lower() in owner]
    return matches or ["Unassigned"]


def compute_plan_signals(data: SignalDataPort, *, project: str,
                         due_soon_days: int = 7) -> dict[str, Any]:
    """Return the existing plan-signals contract without a monolith facade."""
    tasks = data.list_tasks(project)
    by_id = {task["task_id"]: task for task in tasks}
    today = datetime.date.today()

    def is_done(task: dict[str, Any]) -> bool:
        return task.get("status") == "Done"

    def deps_done(task: dict[str, Any]) -> bool:
        return all(by_id.get(dep, {}).get("status") == "Done" for dep in _task_deps(task))

    def actionable(task: dict[str, Any]) -> bool:
        return task.get("status") == "In Progress" or (
            task.get("status") == "Not Started" and deps_done(task)
        )

    overdue: list[dict[str, Any]] = []
    due_soon: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for task in tasks:
        if is_done(task):
            continue
        finish = _date(task.get("finish_date"))
        if task.get("status") == "Blocked":
            blocked.append(task)
        if finish and finish < today:
            overdue.append(task)
        elif finish and (finish - today).days <= due_soon_days:
            due_soon.append(task)
        if task.get("status") == "Not Started" and deps_done(task):
            ready.append(task)
        elif task.get("status") == "Not Started" and not deps_done(task):
            waiting.append(task)

    critical_ids: set[str] = set()
    for item in _as_list(data.get_meta("critical_path", [], project)):
        task_id = item.get("task_id") if isinstance(item, dict) else item if isinstance(item, str) else None
        if task_id:
            critical_ids.add(task_id)
    critical_slip = [
        task for task in tasks
        if task["task_id"] in critical_ids and not is_done(task)
        and (task.get("status") == "Blocked" or (
            _date(task.get("finish_date")) is not None
            and _date(task.get("finish_date")) < today
        ))
    ]

    past_due_decisions: list[dict[str, Any]] = []
    for decision in _as_list(data.get_meta("consolidated_decisions", [], project)):
        if not isinstance(decision, dict):
            continue
        needed_by = _date(decision.get("needed_by"))
        if needed_by and needed_by < today:
            past_due_decisions.append({
                "question": decision.get("question"),
                "owner": decision.get("owner"),
                "needed_by": decision.get("needed_by"),
                "workstream": decision.get("workstream"),
            })

    people = [
        person for person in _as_list(data.get_meta("people", [], project))
        if isinstance(person, str)
    ] or DEFAULT_PEOPLE

    def score(task: dict[str, Any]) -> int:
        finish = _date(task.get("finish_date"))
        value = 0
        if finish and finish < today:
            value += 1000 + (today - finish).days
        if task.get("is_blocking"):
            value += 500
        if task.get("status") == "In Progress":
            value += 200
        if finish and 0 <= (finish - today).days <= due_soon_days:
            value += 100
        return value

    by_owner_next: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        if is_done(task) or not actionable(task):
            continue
        for owner in _people_of(task, people):
            by_owner_next.setdefault(owner, []).append(task)
    for owner, owner_tasks in list(by_owner_next.items()):
        by_owner_next[owner] = [_brief(item) for item in sorted(owner_tasks, key=score, reverse=True)[:2]]

    def briefs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_brief(item) for item in sorted(items, key=lambda task: task.get("finish_date") or "9999")]

    return {
        "as_of": today.isoformat(),
        "counts": {
            "overdue": len(overdue),
            "due_soon": len(due_soon),
            "blocked": len(blocked),
            "ready": len(ready),
            "waiting_on_deps": len(waiting),
            "critical_slip": len(critical_slip),
            "past_due_decisions": len(past_due_decisions),
        },
        "overdue": briefs(overdue),
        "due_soon": briefs(due_soon),
        "blocked": briefs(blocked),
        "ready": briefs(ready),
        "critical_slip": briefs(critical_slip),
        "past_due_decisions": past_due_decisions,
        "by_owner_next": by_owner_next,
    }
