"""SQLite store for the taikun-pm satellite — tasks + activity, seeded from a
bundled plan snapshot. One file, zero ops (see ADR 0007). No shared DB touched."""
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("PM_DB_PATH", os.path.join(os.path.dirname(__file__), "taikun_pm.db"))
SEED_PATH = os.environ.get("PM_SEED_PATH", os.path.join(os.path.dirname(__file__), "seed_plan.json"))

# Fields a PATCH may change (everything an editor touches in an Asana-style board).
EDITABLE = ["title", "description", "owner_org", "owner_person_or_role", "assignee",
            "phase", "status", "effort_days", "duration_days", "start_date",
            "finish_date", "risk_level", "is_blocking", "sort_order",
            "entry_criteria", "exit_criteria", "deliverable", "depends_on"]

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).
META_SECTIONS = ["project", "generated", "schedule_start", "schedule_note", "owner_orgs",
                 "rollups", "executive_summary", "timeline_note", "critical_path",
                 "milestones", "consolidated_risks", "consolidated_decisions", "people"]

# A sensible default people list for the assignee picker (the real names in the plan).
DEFAULT_PEOPLE = ["Steve Ridder", "Taikun eng", "Darko", "Sahir", "Sebastian", "Mike",
                  "Michelle", "Sierra", "Clovis", "Devin", "Brent", "IFS owner", "Nubo"]


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                workstream_id TEXT, workstream_name TEXT,
                title TEXT, description TEXT,
                owner_org TEXT, owner_person_or_role TEXT, assignee TEXT,
                phase TEXT, status TEXT DEFAULT 'Not Started',
                effort_days REAL, duration_days INTEGER,
                start_date TEXT, finish_date TEXT, start_day INTEGER,
                depends_on TEXT, entry_criteria TEXT, exit_criteria TEXT, deliverable TEXT,
                risk_level TEXT, is_blocking INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                created_at REAL, updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS ix_tasks_ws ON tasks(workstream_id);
            CREATE INDEX IF NOT EXISTS ix_activity_task ON activity(task_id);
            """
        )


def seed_if_empty():
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if n:
            return n
        if not os.path.exists(SEED_PATH):
            return 0
        plan = json.load(open(SEED_PATH))
        now = time.time()
        order = 0
        for w in plan.get("workstreams", []):
            for t in w.get("tasks", []):
                order += 1
                c.execute(
                    """INSERT OR REPLACE INTO tasks
                    (task_id, workstream_id, workstream_name, title, description,
                     owner_org, owner_person_or_role, assignee, phase, status,
                     effort_days, duration_days, start_date, finish_date, start_day,
                     depends_on, entry_criteria, exit_criteria, deliverable,
                     risk_level, is_blocking, sort_order, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t["task_id"], w["workstream_id"], w["name"], t.get("title"),
                     t.get("description"), t.get("owner_org"), t.get("owner_person_or_role"),
                     t.get("assignee"), t.get("phase"), t.get("status", "Not Started"),
                     t.get("effort_days"), t.get("duration_days"), t.get("start_date"),
                     t.get("finish_date"), t.get("start_day"),
                     json.dumps(t.get("depends_on", [])), t.get("entry_criteria"),
                     t.get("exit_criteria"), t.get("deliverable"), t.get("risk_level"),
                     1 if t.get("is_blocking") else 0, order, now, now),
                )
        for k in META_SECTIONS:
            if k in plan:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                          (k, json.dumps(plan[k])))
        if "people" not in plan:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                      ("people", json.dumps(DEFAULT_PEOPLE)))
        return order


def _task_row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["depends_on"] = json.loads(d.get("depends_on") or "[]")
    d["is_blocking"] = bool(d.get("is_blocking"))
    d["_wsId"] = d.pop("workstream_id")
    d["_wsName"] = d.pop("workstream_name")
    return d


def list_tasks(workstream: Optional[str] = None, status: Optional[str] = None,
               assignee: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM tasks WHERE 1=1"
    p: List[Any] = []
    if workstream:
        q += " AND workstream_id=?"; p.append(workstream)
    if status:
        q += " AND status=?"; p.append(status)
    if assignee:
        q += " AND assignee=?"; p.append(assignee)
    q += " ORDER BY sort_order"
    with _conn() as c:
        return [_task_row(r) for r in c.execute(q, p).fetchall()]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            return None
        t = _task_row(r)
        t["activity"] = [dict(a) | {"payload": json.loads(a["payload"] or "{}")}
                         for a in c.execute(
                             "SELECT * FROM activity WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
        return t


def update_task(task_id: str, fields: Dict[str, Any], actor: str = "user") -> Optional[Dict[str, Any]]:
    sets, vals, changed = [], [], {}
    for k, v in fields.items():
        if k not in EDITABLE:
            continue
        if k == "is_blocking":
            v = 1 if v else 0
        if k == "depends_on" and isinstance(v, list):
            v = json.dumps(v)
        sets.append(f"{k}=?"); vals.append(v); changed[k] = v
    if not sets:
        return get_task(task_id)
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(task_id)
    with _conn() as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        if cur.rowcount == 0:
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "edit", json.dumps(changed), time.time()))
    return get_task(task_id)


def add_comment(task_id: str, actor: str, text: str, kind: str = "comment") -> Optional[Dict[str, Any]]:
    with _conn() as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, kind, json.dumps({"text": text}), time.time()))
    return get_task(task_id)


def create_task(data: Dict[str, Any], actor: str = "user") -> Optional[Dict[str, Any]]:
    ws = (data.get("workstream_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not ws or not title:
        return None
    with _conn() as c:
        wsname = data.get("workstream_name")
        if not wsname:
            r = c.execute("SELECT workstream_name FROM tasks WHERE workstream_id=? LIMIT 1", (ws,)).fetchone()
            wsname = r[0] if r else ws
        ids = [row[0] for row in c.execute("SELECT task_id FROM tasks WHERE workstream_id=?", (ws,)).fetchall()]
        mx = 0
        for t in ids:
            tail = t.rsplit("-", 1)[-1]
            if tail.isdigit():
                mx = max(mx, int(tail))
        tid = f"{ws}-{mx + 1}"
        while c.execute("SELECT 1 FROM tasks WHERE task_id=?", (tid,)).fetchone():
            mx += 1
            tid = f"{ws}-{mx + 1}"
        order = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks").fetchone()[0]
        now = time.time()
        c.execute(
            """INSERT INTO tasks (task_id, workstream_id, workstream_name, title, description,
                 owner_org, owner_person_or_role, assignee, phase, status, effort_days, duration_days,
                 start_date, finish_date, start_day, depends_on, entry_criteria, exit_criteria,
                 deliverable, risk_level, is_blocking, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, ws, wsname, title, data.get("description"), data.get("owner_org"),
             data.get("owner_person_or_role"), data.get("assignee"), data.get("phase", "Build"),
             data.get("status", "Not Started"), data.get("effort_days"), data.get("duration_days"),
             data.get("start_date"), data.get("finish_date"), 0,
             json.dumps(data.get("depends_on", [])), data.get("entry_criteria"), data.get("exit_criteria"),
             data.get("deliverable"), data.get("risk_level", "Medium"),
             1 if data.get("is_blocking") else 0, order, now, now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (tid, actor, "create", json.dumps({"title": title}), now))
    return get_task(tid)


def delete_task(task_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM activity WHERE task_id=?", (task_id,))
        return cur.rowcount > 0


def get_meta(key: str, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def board_payload() -> Dict[str, Any]:
    tasks = list_tasks()
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload: Dict[str, Any] = {k: get_meta(k) for k in META_SECTIONS}
    payload["workstreams"] = list(by_ws.values())
    return payload
