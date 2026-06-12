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
            CREATE TABLE IF NOT EXISTS chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT, role TEXT, content TEXT, payload TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL, since_ts REAL, content TEXT, meta TEXT
            );
            CREATE TABLE IF NOT EXISTS rag_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT, label TEXT, text TEXT, embedding TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, external_id TEXT, sender TEXT, subject TEXT,
                summary TEXT, triage TEXT, status TEXT DEFAULT 'pending',
                received_at REAL, created_at REAL
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_ws ON tasks(workstream_id);
            CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status);
            CREATE INDEX IF NOT EXISTS ix_activity_task ON activity(task_id);
            CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(created_at);
            CREATE INDEX IF NOT EXISTS ix_chat_session ON chat(session);
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
             data.get("owner_person_or_role"), data.get("assignee"), (data.get("phase") or "Build"),
             (data.get("status") or "Not Started"), data.get("effort_days"), data.get("duration_days"),
             data.get("start_date"), data.get("finish_date"), 0,
             json.dumps(data.get("depends_on", [])), data.get("entry_criteria"), data.get("exit_criteria"),
             data.get("deliverable"), (data.get("risk_level") or "Medium"),
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


def set_meta(key: str, value):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


# ---- dev dispatches (Claude Code runner) — so the UI can show the latest run per task ----
def add_dispatch(task_id: str, job_id: str):
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "task_id TEXT, job_id TEXT, created_at REAL)")
        c.execute("INSERT INTO dispatches(task_id, job_id, created_at) VALUES (?,?,?)",
                  (task_id, job_id, time.time()))


def latest_dispatch(task_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        try:
            r = c.execute("SELECT job_id, created_at FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
                          (task_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return {"job_id": r["job_id"], "created_at": r["created_at"]} if r else None


# ---- contacts (email -> display name) for inbound-reply routing ----------
# Seeded with the known TEEP participants so the email agent can resolve "Sahir",
# "Darko", "Steve" -> the right address; auto-learned from every inbound From/To/Cc.
_SEED_CONTACTS = {
    "steve@taikunai.com": "Steve Ridder",
    "sahir.shah@totalenergies.com": "Sahir Shah",
    "darko.jankovic@totalenergies.com": "Darko Jankovic",
}


def get_contacts() -> Dict[str, str]:
    c = get_meta("contacts")
    if not c:
        c = dict(_SEED_CONTACTS)
        set_meta("contacts", c)
    return c


def upsert_contact(email: str, name: Optional[str] = None):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return
    c = get_contacts()
    name = (name or "").strip()
    if email not in c or (name and not c.get(email)):
        c[email] = name or c.get(email) or email
        set_meta("contacts", c)


# ---- plan-wide chat (the global "Ask Taikun" session) --------------------
def add_chat(session: str, role: str, content: str, payload: Optional[Dict[str, Any]] = None):
    with _conn() as c:
        c.execute("INSERT INTO chat(session, role, content, payload, created_at) VALUES (?,?,?,?,?)",
                  (session, role, content, json.dumps(payload or {}), time.time()))


def clear_chat(session: str):
    with _conn() as c:
        c.execute("DELETE FROM chat WHERE session=?", (session,))


def recent_chat(session: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, payload, created_at FROM chat WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit)).fetchall()
    out = [{"role": r["role"], "content": r["content"],
            "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]
    out.reverse()
    return out


# ---- activity deltas + digests (Phase 3.5) -------------------------------
def activity_since(ts: float) -> List[Dict[str, Any]]:
    """Every activity event across all tasks since `ts` — the delta substrate."""
    with _conn() as c:
        rows = c.execute(
            "SELECT task_id, actor, kind, payload, created_at FROM activity WHERE created_at > ? ORDER BY created_at",
            (ts,)).fetchall()
    return [{"task_id": r["task_id"], "actor": r["actor"], "kind": r["kind"],
             "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]


def _digest_row(r):
    return {"id": r["id"], "created_at": r["created_at"], "since_ts": r["since_ts"],
            "content": r["content"], "meta": json.loads(r["meta"] or "{}")}


def add_digest(since_ts: float, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO digests(created_at, since_ts, content, meta) VALUES (?,?,?,?)",
                        (time.time(), since_ts, content, json.dumps(meta or {})))
        return cur.lastrowid


def last_digest() -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        return _digest_row(r) if r else None


def list_digests(limit: int = 20) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_digest_row(r) for r in rows]


# ---- incremental RAG corpus (Phase 5) — ingested artifacts, persisted + shared --------
def add_rag_chunk(source_kind: str, label: str, text: str, embedding: List[float]):
    with _conn() as c:
        c.execute("INSERT INTO rag_docs(source_kind, label, text, embedding, created_at) VALUES (?,?,?,?,?)",
                  (source_kind, label, text, json.dumps(embedding), time.time()))


def all_rag_chunks() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT label, text, embedding FROM rag_docs ORDER BY id").fetchall()
    return [{"label": r["label"], "text": r["text"], "embedding": json.loads(r["embedding"])} for r in rows]


def rag_docs_max_id() -> int:
    with _conn() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM rag_docs").fetchone()[0]


# ---- Live Inbox queue (Phase 5.5) — triaged inbound artifacts awaiting review ----------
def _inbox_row(r):
    return {"id": r["id"], "source": r["source"], "external_id": r["external_id"],
            "sender": r["sender"], "subject": r["subject"], "summary": r["summary"],
            "triage": json.loads(r["triage"] or "{}"), "status": r["status"],
            "received_at": r["received_at"], "created_at": r["created_at"]}


def inbox_exists(source: str, external_id: str) -> bool:
    if not external_id:
        return False
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM inbox WHERE source=? AND external_id=?",
                              (source, external_id)).fetchone())


def add_inbox_item(source, external_id, sender, subject, summary, triage, received_at=None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO inbox(source,external_id,sender,subject,summary,triage,status,received_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source, external_id, sender, subject, summary, json.dumps(triage or {}), "pending",
             received_at or time.time(), time.time()))
        return cur.lastrowid


def list_inbox(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    with _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM inbox WHERE status=? ORDER BY id DESC LIMIT ?",
                             (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_inbox_row(r) for r in rows]


def get_inbox_item(item_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
        return _inbox_row(r) if r else None


def set_inbox_status(item_id: int, status: str):
    with _conn() as c:
        c.execute("UPDATE inbox SET status=? WHERE id=?", (status, item_id))


def inbox_pending_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0]


def board_payload() -> Dict[str, Any]:
    tasks = list_tasks()
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload: Dict[str, Any] = {k: get_meta(k) for k in META_SECTIONS}
    payload["workstreams"] = list(by_ws.values())
    return payload
