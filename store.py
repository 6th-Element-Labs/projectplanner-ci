"""SQLite store for the taikun-pm satellite — tasks + activity, seeded from a
bundled plan snapshot. One file, zero ops (see ADR 0007). No shared DB touched."""
import json
import hashlib
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("PM_DB_PATH", os.path.join(os.path.dirname(__file__), "taikun_pm.db"))
SEED_PATH = os.environ.get("PM_SEED_PATH", os.path.join(os.path.dirname(__file__), "seed_plan.json"))
HELM_DB_PATH = os.environ.get("PM_HELM_DB_PATH", os.path.join(os.path.dirname(__file__), "helm.db"))
HELM_SEED_PATH = os.environ.get("PM_HELM_SEED_PATH",
                                os.path.join(os.path.dirname(__file__), "seeds", "helm_seed_plan.json"))
SWITCHBOARD_DB_PATH = os.environ.get("PM_SWITCHBOARD_DB_PATH",
                                     os.path.join(os.path.dirname(__file__), "switchboard.db"))
SWITCHBOARD_SEED_PATH = os.environ.get("PM_SWITCHBOARD_SEED_PATH",
                                       os.path.join(os.path.dirname(__file__), "seeds",
                                                    "switchboard_seed_plan.json"))

# Multi-project registry. Each project is its OWN sqlite file — physical isolation, so a Helm
# request can never read or write Maxwell's rows (no shared table, no project_id column). The
# default is always 'maxwell', so every existing caller behaves exactly as before.
PROJECTS = {
    "maxwell": {"db": DB_PATH, "seed": SEED_PATH,
                "label": "Project Maxwell", "pretitle": "TEEP Barnett · TotalEnergies E&P"},
    "helm": {"db": HELM_DB_PATH, "seed": HELM_SEED_PATH,
             "label": "Helm — Marine Nav Companion", "pretitle": "6th Element Labs · web-first chartplotter"},
    "switchboard": {"db": SWITCHBOARD_DB_PATH, "seed": SWITCHBOARD_SEED_PATH,
                    "label": "Switchboard — Agent Coordination Layer",
                    "pretitle": "6th Element Labs · live dogfood control plane"},
}
DEFAULT_PROJECT = "maxwell"


def hash_token(token: str) -> str:
    """Stable one-way token hash for principal lookup."""
    return hashlib.sha256(("switchboard:" + (token or "")).encode("utf-8")).hexdigest()


def projects() -> List[Dict[str, Any]]:
    """The switcher's source of truth — [{id, label, pretitle}]."""
    return [{"id": k, "label": v["label"], "pretitle": v.get("pretitle", "")} for k, v in PROJECTS.items()]


def _resolve(project: Optional[str]) -> Dict[str, str]:
    """Map a project id -> its config. Fail CLOSED on an unknown id — never silently fall back
    to Maxwell (which could leak a write across projects)."""
    p = PROJECTS.get(project or DEFAULT_PROJECT)
    if not p:
        raise ValueError(f"unknown project: {project!r}")
    return p

# Fields a PATCH may change (everything an editor touches in an Asana-style board).
EDITABLE = ["title", "description", "owner_org", "owner_person_or_role", "assignee",
            "phase", "status", "effort_days", "duration_days", "start_date",
            "finish_date", "risk_level", "is_blocking", "sort_order",
            "entry_criteria", "exit_criteria", "deliverable", "depends_on"]

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).
META_SECTIONS = ["project", "generated", "schedule_start", "schedule_note", "owner_orgs",
                 "rollups", "executive_summary", "timeline_note", "critical_path",
                 "milestones", "consolidated_risks", "consolidated_decisions", "people",
                 "working_agreement"]

# A sensible default people list for the assignee picker (the real names in the plan).
DEFAULT_PEOPLE = ["Steve Ridder", "Taikun eng", "Darko", "Sahir", "Sebastian", "Mike",
                  "Michelle", "Sierra", "Clovis", "Devin", "Brent", "IFS owner", "Nubo"]


def _conn(project: str = DEFAULT_PROJECT):
    c = sqlite3.connect(_resolve(project)["db"])
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db(project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
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
            CREATE TABLE IF NOT EXISTS file_leases (
                id          TEXT PRIMARY KEY,
                agent_id    TEXT NOT NULL,
                task_id     TEXT,
                files       TEXT NOT NULL,
                claimed_at  REAL NOT NULL,
                ttl_minutes INTEGER NOT NULL DEFAULT 30,
                released_at REAL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT,
                author      TEXT NOT NULL,
                title       TEXT NOT NULL,
                context     TEXT NOT NULL,
                decision    TEXT NOT NULL,
                rationale   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'accepted',
                supersedes  INTEGER,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_decisions_task ON decisions(task_id);
            CREATE TABLE IF NOT EXISTS agent_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent    TEXT NOT NULL,
                to_agent      TEXT NOT NULL,
                task_id       TEXT,
                message       TEXT NOT NULL,
                requires_ack  INTEGER NOT NULL DEFAULT 0,
                ack_deadline  REAL,
                sent_at       REAL NOT NULL,
                acked_at      REAL,
                ack_response  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_messages_to ON agent_messages(to_agent, acked_at);
            CREATE TABLE IF NOT EXISTS principals (
                id            TEXT PRIMARY KEY,
                kind          TEXT NOT NULL,
                display_name  TEXT NOT NULL,
                project       TEXT NOT NULL,
                scopes        TEXT NOT NULL,
                token_hash    TEXT NOT NULL,
                created_at    REAL NOT NULL,
                revoked_at    REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_principals_token ON principals(token_hash);
            CREATE TABLE IF NOT EXISTS agent_presence (
                agent_id      TEXT PRIMARY KEY,
                runtime       TEXT NOT NULL,
                model         TEXT,
                lane          TEXT,
                task_id       TEXT,
                control       TEXT NOT NULL DEFAULT '{}',
                principal_id  TEXT,
                registered_at REAL NOT NULL,
                heartbeat_at  REAL NOT NULL,
                ttl_s         INTEGER NOT NULL DEFAULT 120
            );
            CREATE INDEX IF NOT EXISTS ix_presence_lane ON agent_presence(lane, heartbeat_at);
            CREATE TABLE IF NOT EXISTS resource_leases (
                id            TEXT PRIMARY KEY,
                agent_id      TEXT NOT NULL,
                principal_id  TEXT,
                task_id       TEXT,
                resource_type TEXT NOT NULL,
                names         TEXT NOT NULL,
                claimed_at    REAL NOT NULL,
                ttl_seconds   INTEGER NOT NULL DEFAULT 1800,
                released_at   REAL
            );
            CREATE INDEX IF NOT EXISTS ix_resource_leases_agent ON resource_leases(agent_id);
            CREATE INDEX IF NOT EXISTS ix_resource_leases_type ON resource_leases(resource_type, released_at);
            CREATE TABLE IF NOT EXISTS task_claims (
                id             TEXT PRIMARY KEY,
                task_id        TEXT NOT NULL,
                agent_id       TEXT NOT NULL,
                principal_id   TEXT,
                status         TEXT NOT NULL,
                claimed_at     REAL NOT NULL,
                expires_at     REAL NOT NULL,
                completed_at   REAL,
                abandon_reason TEXT,
                idem_key       TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_task_claims_active
                ON task_claims(task_id, status, expires_at);
            CREATE TABLE IF NOT EXISTS task_git_state (
                task_id            TEXT PRIMARY KEY,
                branch             TEXT,
                head_sha           TEXT,
                pushed_at          REAL,
                pr_number          INTEGER,
                pr_url             TEXT,
                merged_sha         TEXT,
                merged_at          REAL,
                in_main_content    INTEGER NOT NULL DEFAULT 0,
                published_ref      TEXT,
                last_reconciled_at REAL,
                evidence_json      TEXT NOT NULL DEFAULT '{}',
                updated_at         REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                idem_key      TEXT NOT NULL,
                operation     TEXT NOT NULL,
                actor         TEXT NOT NULL,
                request_hash  TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at    REAL NOT NULL,
                PRIMARY KEY (idem_key, operation)
            );
            CREATE TABLE IF NOT EXISTS llm_spend (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id        TEXT,
                source            TEXT NOT NULL,
                confidence        TEXT NOT NULL DEFAULT 'unknown',
                task_id           TEXT,
                claim_id          TEXT,
                outcome_id        TEXT,
                agent_id          TEXT,
                principal_id      TEXT,
                runtime           TEXT,
                call_site         TEXT,
                provider          TEXT,
                model             TEXT,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens      INTEGER NOT NULL DEFAULT 0,
                cost_usd          REAL NOT NULL DEFAULT 0.0,
                latency_ms        REAL,
                status            TEXT NOT NULL DEFAULT 'ok',
                metadata_json     TEXT NOT NULL DEFAULT '{}',
                created_at        REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_spend_task ON llm_spend(task_id);
            CREATE INDEX IF NOT EXISTS ix_spend_agent ON llm_spend(agent_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_spend_request
                ON llm_spend(request_id) WHERE request_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS task_summaries (
                task_id         TEXT PRIMARY KEY,
                rationale       TEXT NOT NULL,
                generated_at    REAL NOT NULL,
                activity_cursor INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_ws ON tasks(workstream_id);
            CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status);
            CREATE INDEX IF NOT EXISTS ix_activity_task ON activity(task_id);
            CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(created_at);
            CREATE INDEX IF NOT EXISTS ix_chat_session ON chat(session);
            CREATE INDEX IF NOT EXISTS ix_leases_agent ON file_leases(agent_id);
            """
        )
        # Additive column migrations — safe to run on every startup
        for col_sql in [
            "ALTER TABLE tasks ADD COLUMN agent_state TEXT",  # JSON blob per agent
            "ALTER TABLE agent_messages ADD COLUMN signal TEXT",
            "ALTER TABLE agent_messages ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE agent_messages ADD COLUMN idem_key TEXT",
            "ALTER TABLE agent_messages ADD COLUMN principal_id TEXT",
        ]:
            try:
                c.execute(col_sql)
            except Exception:
                pass  # column already exists
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idem "
                      "ON agent_messages(idem_key) WHERE idem_key IS NOT NULL")
        except Exception:
            pass


def seed_if_empty(project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if n:
            return n
        seed_path = _resolve(project)["seed"]
        if not os.path.exists(seed_path):
            return 0
        plan = json.load(open(seed_path))
        now = time.time()
        order = 0
        for w in plan.get("workstreams", []):
            for t in w.get("tasks", []):
                order += 1
                so = order if t.get("sort_order") is None else t.get("sort_order")
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
                     1 if t.get("is_blocking") else 0, so, now, now),
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
    raw_state = d.pop("agent_state", None)
    d["agent_state"] = json.loads(raw_state) if raw_state else {}
    return d


def _git_state_row(r: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if not r:
        return {"branch": None, "head_sha": None, "pushed_at": None, "pr_number": None,
                "pr_url": None, "merged_sha": None, "merged_at": None,
                "in_main_content": False, "published_ref": None,
                "last_reconciled_at": None, "evidence": {}}
    d = dict(r)
    d["in_main_content"] = bool(d.get("in_main_content"))
    d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    return d


def _load_git_state(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    return _git_state_row(c.execute("SELECT * FROM task_git_state WHERE task_id=?",
                                    (task_id,)).fetchone())


def _parse_evidence(evidence: Any) -> Dict[str, Any]:
    if isinstance(evidence, dict):
        return dict(evidence)
    if not evidence:
        return {}
    if isinstance(evidence, str):
        try:
            parsed = json.loads(evidence)
            return parsed if isinstance(parsed, dict) else {"note": evidence}
        except Exception:
            return {"note": evidence}
    return {"value": evidence}


def _upsert_git_state(c: sqlite3.Connection, task_id: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    current = _load_git_state(c, task_id)
    evidence = dict(current.get("evidence") or {})
    if "evidence" in updates and isinstance(updates["evidence"], dict):
        evidence.update(updates.pop("evidence"))
    clean_updates = {k: v for k, v in updates.items() if v is not None}
    merged = {**current, **clean_updates}
    branch = merged.get("branch")
    head_sha = merged.get("head_sha")
    pushed_at = merged.get("pushed_at")
    pr_number = merged.get("pr_number")
    pr_url = merged.get("pr_url")
    merged_sha = merged.get("merged_sha")
    merged_at = merged.get("merged_at")
    in_main = 1 if merged.get("in_main_content") else 0
    published_ref = merged.get("published_ref")
    last_reconciled_at = merged.get("last_reconciled_at")
    c.execute(
        "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, pr_number, pr_url, "
        "merged_sha, merged_at, in_main_content, published_ref, last_reconciled_at, "
        "evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET branch=excluded.branch, head_sha=excluded.head_sha, "
        "pushed_at=excluded.pushed_at, pr_number=excluded.pr_number, pr_url=excluded.pr_url, "
        "merged_sha=excluded.merged_sha, merged_at=excluded.merged_at, "
        "in_main_content=excluded.in_main_content, published_ref=excluded.published_ref, "
        "last_reconciled_at=excluded.last_reconciled_at, evidence_json=excluded.evidence_json, "
        "updated_at=excluded.updated_at",
        (task_id, branch, head_sha, pushed_at, pr_number, pr_url, merged_sha, merged_at,
         in_main, published_ref, last_reconciled_at, json.dumps(evidence, sort_keys=True), now),
    )
    return _load_git_state(c, task_id)


def list_tasks(workstream: Optional[str] = None, status: Optional[str] = None,
               assignee: Optional[str] = None, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM tasks WHERE 1=1"
    p: List[Any] = []
    if workstream:
        q += " AND workstream_id=?"; p.append(workstream)
    if status:
        q += " AND status=?"; p.append(status)
    if assignee:
        q += " AND assignee=?"; p.append(assignee)
    q += " ORDER BY sort_order"
    with _conn(project) as c:
        return [_task_row(r) for r in c.execute(q, p).fetchall()]


def get_task(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            return None
        t = _task_row(r)
        t["activity"] = [dict(a) | {"payload": json.loads(a["payload"] or "{}")}
                         for a in c.execute(
                             "SELECT * FROM activity WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
        t["git_state"] = _load_git_state(c, task_id)
        s = c.execute("SELECT rationale FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        if s:
            t["rationale"] = s["rationale"]
        return t


def update_task(task_id: str, fields: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
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
        return get_task(task_id, project)
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(task_id)
    with _conn(project) as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        if cur.rowcount == 0:
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "edit", json.dumps(changed), time.time()))
    return get_task(task_id, project)


def add_comment(task_id: str, actor: str, text: str, kind: str = "comment",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, kind, json.dumps({"text": text}), time.time()))
    return get_task(task_id, project)


def create_task(data: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    ws = (data.get("workstream_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not ws or not title:
        return None
    with _conn(project) as c:
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
    return get_task(tid, project)


def get_activity_delta(since_cursor: int = 0, lane: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Return activity newer than since_cursor (activity.id rowid — monotonic, clock-skew-safe).
    lane filters to one workstream (e.g. 'ENGINE'). Returns
    {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Use this for polling instead of list_tasks/board_summary — empty updates = zero tokens wasted."""
    lane_upper = lane.strip().upper() if lane else ""
    with _conn(project) as c:
        if lane_upper:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ? AND t.workstream_id = ?
                   ORDER BY a.id""",
                (since_cursor, lane_upper),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ?
                   ORDER BY a.id""",
                (since_cursor,),
            ).fetchall()
        git_states = {r["task_id"]: _load_git_state(c, r["task_id"]) for r in rows}
    if not rows:
        return {"cursor": since_cursor, "updates": []}
    new_cursor = rows[-1]["id"]
    by_task: Dict[str, Any] = {}
    for row in rows:
        tid = row["task_id"]
        if tid not in by_task:
            by_task[tid] = {"task_id": tid, "status": row["status"],
                            "title": row["title"], "workstream_id": row["workstream_id"],
                            "kinds": [], "git_state": git_states.get(tid, {})}
        by_task[tid]["status"] = row["status"]
        if row["kind"] not in by_task[tid]["kinds"]:
            by_task[tid]["kinds"].append(row["kind"])
    return {"cursor": new_cursor, "updates": list(by_task.values())}


def _request_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _idem_hit(c: sqlite3.Connection, operation: str, idem_key: str,
              actor: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not idem_key:
        return None
    row = c.execute("SELECT request_hash, response_json FROM idempotency_keys "
                    "WHERE idem_key=? AND operation=?", (idem_key, operation)).fetchone()
    if not row:
        return None
    if row["request_hash"] != _request_hash(payload):
        return {"error": "idempotency conflict", "idem_key": idem_key, "operation": operation}
    return json.loads(row["response_json"])


def _idem_store(c: sqlite3.Connection, operation: str, idem_key: str,
                actor: str, payload: Dict[str, Any], response: Dict[str, Any]) -> None:
    if not idem_key:
        return
    c.execute(
        "INSERT OR REPLACE INTO idempotency_keys"
        "(idem_key, operation, actor, request_hash, response_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (idem_key, operation, actor, _request_hash(payload), json.dumps(response, sort_keys=True), time.time()),
    )


def append_activity(kind: str, actor: str, payload: Optional[Dict[str, Any]] = None,
                    task_id: Optional[str] = None,
                    project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (task_id, actor, kind, json.dumps(payload or {}, sort_keys=True), time.time()))
        return cur.lastrowid


def create_principal(kind: str, display_name: str, token: str, scopes: List[str],
                     principal_id: Optional[str] = None,
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    principal_id = principal_id or f"{kind}-{uuid.uuid4().hex[:12]}"
    now = time.time()
    scopes_json = json.dumps(scopes, sort_keys=True)
    with _conn(project) as c:
        c.execute(
            "INSERT INTO principals(id, kind, display_name, project, scopes, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (principal_id, kind, display_name, project, scopes_json, hash_token(token), now),
        )
    return {"id": principal_id, "kind": kind, "display_name": display_name,
            "project": project, "scopes": scopes, "created_at": now}


def get_principal_by_token(project: str, token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM principals WHERE token_hash=?",
                        (hash_token(token),)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["scopes"] = json.loads(out.get("scopes") or "[]")
    return out


def revoke_principal(principal_id: str, project: str = DEFAULT_PROJECT) -> bool:
    with _conn(project) as c:
        cur = c.execute("UPDATE principals SET revoked_at=? WHERE id=?",
                        (time.time(), principal_id))
        return cur.rowcount > 0


def register_agent(agent_id: str, runtime: str, model: str = "", lane: str = "",
                   task_id: str = "", ttl_s: int = 120,
                   control: Optional[Dict[str, Any]] = None,
                   principal_id: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    ttl_s = max(10, int(ttl_s or 120))
    control_json = json.dumps(control or {}, sort_keys=True)
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO agent_presence"
            "(agent_id, runtime, model, lane, task_id, control, principal_id, "
            "registered_at, heartbeat_at, ttl_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent_id, runtime, model or None, lane or None, task_id or None, control_json,
             principal_id or None, now, now, ttl_s),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id or None, actor, "agent.registered",
                   json.dumps({"agent_id": agent_id, "runtime": runtime, "lane": lane,
                               "control": control or {}}, sort_keys=True), now))
    return {"agent_id": agent_id, "runtime": runtime, "model": model or None,
            "lane": lane or None, "task_id": task_id or None,
            "control": control or {}, "registered_at": now,
            "heartbeat_at": now, "expires_at": now + ttl_s, "ttl_s": ttl_s}


def heartbeat(agent_id: str, project: str = DEFAULT_PROJECT,
              actor: str = "system") -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        cur = c.execute("UPDATE agent_presence SET heartbeat_at=? WHERE agent_id=?",
                        (now, agent_id))
        row = c.execute("SELECT * FROM agent_presence WHERE agent_id=?", (agent_id,)).fetchone()
        if cur.rowcount:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"] if row else None, actor, "agent.heartbeat",
                       json.dumps({"agent_id": agent_id}, sort_keys=True), now))
    if not row:
        return {"error": "agent not registered", "agent_id": agent_id}
    return _presence_row(row, now=now)


def _presence_row(row: sqlite3.Row, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    ttl_s = row["ttl_s"]
    expires_at = row["heartbeat_at"] + ttl_s
    return {"agent_id": row["agent_id"], "runtime": row["runtime"], "model": row["model"],
            "lane": row["lane"], "task_id": row["task_id"],
            "control": json.loads(row["control"] or "{}"),
            "registered_at": row["registered_at"], "heartbeat_at": row["heartbeat_at"],
            "expires_at": expires_at, "ttl_s": ttl_s, "stale": now >= expires_at}


def list_active_agents(lane: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        if lane:
            rows = c.execute("SELECT * FROM agent_presence WHERE lane=? ORDER BY heartbeat_at DESC",
                             (lane,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM agent_presence ORDER BY heartbeat_at DESC").fetchall()
    return [p for p in (_presence_row(r, now=now) for r in rows) if not p["stale"]]


def _active_resource_leases_in(c: sqlite3.Connection, now: float,
                               resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
    if resource_type:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL "
                         "AND resource_type=?", (resource_type,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_seconds"]]


def claim_resources(agent_id: str, resource_type: str, names: List[str],
                    task_id: Optional[str] = None, ttl_seconds: int = 1800,
                    principal_id: str = "", actor: str = "system",
                    idem_key: str = "",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    clean_names = sorted({n.strip() for n in names if n and n.strip()})
    payload = {"agent_id": agent_id, "resource_type": resource_type, "names": clean_names,
               "task_id": task_id, "ttl_seconds": ttl_seconds}
    if not clean_names:
        return {"error": "no resource names given"}
    with _conn(project) as c:
        hit = _idem_hit(c, "claim", idem_key, actor, payload)
        if hit is not None:
            return hit
        wanted = set(clean_names)
        for lease in _active_resource_leases_in(c, now, resource_type):
            if lease["agent_id"] == agent_id:
                continue
            overlap = wanted & set(json.loads(lease["names"] or "[]"))
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_seconds"]
                response = {"conflict": lease["agent_id"], "resource_type": resource_type,
                            "names": sorted(overlap), "task_id": lease.get("task_id"),
                            "retry_after_seconds": max(5, int((expires_at - now) / 2))}
                _idem_store(c, "claim", idem_key, actor, payload, response)
                return response
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task_id, resource_type,
             json.dumps(clean_names), now, max(1, int(ttl_seconds or 1800))),
        )
        response = {"lease_id": lease_id, "agent_id": agent_id, "resource_type": resource_type,
                    "names": clean_names, "task_id": task_id, "claimed_at": now,
                    "expires_at": now + max(1, int(ttl_seconds or 1800))}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "lease.claimed", json.dumps(response, sort_keys=True), now))
        _idem_store(c, "claim", idem_key, actor, payload, response)
        return response


def check_resources(resource_type: str, names: List[str],
                    project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    wanted = {n.strip() for n in names if n and n.strip()}
    out: List[Dict[str, Any]] = []
    with _conn(project) as c:
        for lease in _active_resource_leases_in(c, now, resource_type):
            for name in wanted & set(json.loads(lease["names"] or "[]")):
                out.append({"resource_type": resource_type, "name": name,
                            "held_by": lease["agent_id"], "lease_id": lease["id"],
                            "task_id": lease.get("task_id"),
                            "expires_at": lease["claimed_at"] + lease["ttl_seconds"]})
    return sorted(out, key=lambda x: x["name"])


def release_resource_lease(lease_id: str, actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM resource_leases WHERE id=?", (lease_id,)).fetchone()
        if not row:
            return {"error": "lease not found", "lease_id": lease_id}
        if row["released_at"] is not None:
            return {"released": False, "lease_id": lease_id, "note": "already released"}
        c.execute("UPDATE resource_leases SET released_at=? WHERE id=?", (now, lease_id))
        payload = {"lease_id": lease_id, "agent_id": row["agent_id"],
                   "resource_type": row["resource_type"], "names": json.loads(row["names"] or "[]")}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "lease.released", json.dumps(payload, sort_keys=True), now))
    return {"released": True, "lease_id": lease_id}


def list_active_resource_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        leases = _active_resource_leases_in(c, now)
    return [{"lease_id": l["id"], "agent_id": l["agent_id"], "task_id": l.get("task_id"),
             "resource_type": l["resource_type"], "names": json.loads(l["names"] or "[]"),
             "expires_at": l["claimed_at"] + l["ttl_seconds"]} for l in leases]


def _deps_done(task: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> bool:
    for dep in task.get("depends_on") or []:
        if by_id.get(dep, {}).get("status") != "Done":
            return False
    return True


def claim_next(agent_id: str, lanes: Optional[List[str]] = None,
               capabilities: Optional[List[str]] = None,
               max_risk: str = "", max_budget_usd: Optional[float] = None,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically claim the highest-priority unblocked task for an agent.

    This is the first TXP slice: deterministic, dependency-aware, and intentionally
    conservative. More sophisticated cost/reliability scoring can layer onto the same
    task_claims/activity records.
    """
    now = time.time()
    lane_set = {x.strip().upper() for x in (lanes or []) if x and x.strip()}
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds}
    ready_statuses = {"Not Started", "Ready", "Todo", "Backlog"}
    with _conn(project) as c:
        hit = _idem_hit(c, "claim_next", idem_key, actor, payload)
        if hit is not None:
            return hit
        active_claims = {
            r["task_id"] for r in c.execute(
                "SELECT task_id FROM task_claims WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        tasks = [_task_row(r) for r in rows]
        by_id = {t["task_id"]: t for t in tasks}
        eligible = []
        for t in tasks:
            if t["task_id"] in active_claims:
                continue
            if t.get("status") not in ready_statuses:
                continue
            if lane_set and (t.get("_wsId") or "").upper() not in lane_set:
                continue
            if not _deps_done(t, by_id):
                continue
            priority = int(t.get("sort_order") or 0)
            if t.get("is_blocking"):
                priority -= 10000
            eligible.append((priority, t["task_id"], t))
        if not eligible:
            response = {"claimed": False, "reason": "no_unblocked_work",
                        "retry_after_seconds": 60,
                        "cursor": c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]}
            _idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        _, _, task = sorted(eligible, key=lambda x: (x[0], x[1]))[0]
        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        expires_at = now + max(60, int(ttl_seconds or 1800))
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
            "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, task["task_id"], agent_id, principal_id or None, "active",
             now, expires_at, idem_key or None),
        )
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task["task_id"], "task",
             json.dumps([task["task_id"]]), now, max(60, int(ttl_seconds or 1800))),
        )
        c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                  (agent_id, now, task["task_id"]))
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task["task_id"], "agent_id": agent_id}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task["task_id"], actor, "task.claimed",
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task["task_id"],)).fetchone())
        spend = task_tally(task["task_id"], project=project)
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task["task_id"]], "expires_at": expires_at},
            "budget": {"spent_usd": spend["spend"]["cost_usd"],
                       "remaining_usd": max_budget_usd - spend["spend"]["cost_usd"]
                       if max_budget_usd is not None else None},
            "recommendation": {"model_tier": "balanced",
                               "reason": "initial deterministic dispatcher"},
        }
        _idem_store(c, "claim_next", idem_key, actor, payload, response)
        return response


def complete_claim(claim_id: str, evidence: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    evidence_obj = _parse_evidence(evidence)
    pushed_at = evidence_obj.get("pushed_at")
    if pushed_at is None and evidence_obj.get("head_sha"):
        pushed_at = now
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
                  (now, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (now, row["task_id"]))
        git_state = _upsert_git_state(c, row["task_id"], {
            "branch": evidence_obj.get("branch"),
            "head_sha": evidence_obj.get("head_sha"),
            "pushed_at": pushed_at,
            "pr_number": evidence_obj.get("pr_number"),
            "pr_url": evidence_obj.get("pr_url"),
            "evidence": evidence_obj,
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.completed",
                   json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                               "next_status": "In Review"}, sort_keys=True), now))
    return {"completed": True, "claim_id": claim_id, "task_id": row["task_id"],
            "status": "In Review", "git_state": git_state}


def abandon_claim(claim_id: str, reason: str,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        c.execute("UPDATE task_claims SET status='abandoned', abandon_reason=? WHERE id=?",
                  (reason, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status='Not Started', updated_at=? WHERE task_id=? "
                  "AND status='In Progress'",
                  (now, row["task_id"]))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.abandoned",
                   json.dumps({"claim_id": claim_id, "reason": reason}, sort_keys=True), now))
    return {"abandoned": True, "claim_id": claim_id, "task_id": row["task_id"]}


def mark_task_pr_opened(task_id: str, pr_number: int, pr_url: str = "",
                        branch: str = "", head_sha: str = "",
                        actor: str = "github-webhook",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return {"error": "task not found", "task_id": task_id}
        c.execute("UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "evidence": {"pr_number": pr_number, "pr_url": pr_url,
                         "branch": branch, "head_sha": head_sha},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.pr_opened",
                   json.dumps({"pr_number": pr_number, "pr_url": pr_url,
                               "branch": branch, "head_sha": head_sha}, sort_keys=True), now))
    return {"task_id": task_id, "status": "In Review", "git_state": git_state}


def mark_task_merged(task_id: str, merged_sha: str, pr_number: Optional[int] = None,
                     pr_url: str = "", branch: str = "", head_sha: str = "",
                     actor: str = "github-webhook",
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return {"error": "task not found", "task_id": task_id}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "merged_sha": merged_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": {"merged_sha": merged_sha, "pr_number": pr_number,
                         "pr_url": pr_url, "branch": branch, "head_sha": head_sha},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.pr_merged",
                   json.dumps({"merged_sha": merged_sha, "pr_number": pr_number,
                               "pr_url": pr_url}, sort_keys=True), now))
    return {"task_id": task_id, "status": "Done", "git_state": git_state}


def report_usage(source: str, confidence: str, task_id: Optional[str] = None,
                 claim_id: Optional[str] = None, outcome_id: Optional[str] = None,
                 agent_id: Optional[str] = None, principal_id: str = "",
                 runtime: str = "", call_site: str = "", provider: str = "",
                 model: str = "", prompt_tokens: int = 0,
                 completion_tokens: int = 0, total_tokens: Optional[int] = None,
                 cost_usd: float = 0.0, latency_ms: Optional[float] = None,
                 status: str = "ok", metadata: Optional[Dict[str, Any]] = None,
                 request_id: Optional[str] = None,
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    total = int(total_tokens if total_tokens is not None else prompt_tokens + completion_tokens)
    now = time.time()
    with _conn(project) as c:
        if request_id:
            old = c.execute("SELECT * FROM llm_spend WHERE request_id=?", (request_id,)).fetchone()
            if old:
                return _spend_row(old)
        cur = c.execute(
            "INSERT INTO llm_spend(request_id, source, confidence, task_id, claim_id, outcome_id, "
            "agent_id, principal_id, runtime, call_site, provider, model, prompt_tokens, "
            "completion_tokens, total_tokens, cost_usd, latency_ms, status, metadata_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, source, confidence, task_id, claim_id, outcome_id, agent_id,
             principal_id or None, runtime or None, call_site or None, provider or None, model or None,
             int(prompt_tokens or 0), int(completion_tokens or 0), total, float(cost_usd or 0.0),
             latency_ms, status or "ok", json.dumps(metadata or {}, sort_keys=True), now),
        )
        row = c.execute("SELECT * FROM llm_spend WHERE id=?", (cur.lastrowid,)).fetchone()
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, agent_id or principal_id or "tally", "tally.usage_reported",
                   json.dumps({"spend_id": cur.lastrowid, "source": source,
                               "cost_usd": float(cost_usd or 0.0)}, sort_keys=True), now))
    return _spend_row(row)


def _spend_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    return out


def task_tally(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM llm_spend WHERE task_id=?", (task_id,)).fetchall()
    spend = {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}}
    for row in rows:
        source = row["source"]
        bucket = spend["by_source"].setdefault(source, {"cost_usd": 0.0, "total_tokens": 0,
                                                        "confidence": row["confidence"]})
        bucket["cost_usd"] += float(row["cost_usd"] or 0.0)
        bucket["total_tokens"] += int(row["total_tokens"] or 0)
        spend["cost_usd"] += float(row["cost_usd"] or 0.0)
        spend["total_tokens"] += int(row["total_tokens"] or 0)
    spend["cost_usd"] = round(spend["cost_usd"], 6)
    for bucket in spend["by_source"].values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return {"task_id": task_id, "spend": spend,
            "unit_cost": {"cost_per_verified_outcome": None},
            "outcomes": {"verified": 0, "proposed": 0, "rejected": 0}}


def _active_leases_in(c, now: float) -> List[Dict[str, Any]]:
    """Active leases using an existing connection — not released and not TTL-expired."""
    rows = c.execute("SELECT * FROM file_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_minutes"] * 60]


def claim_files(agent_id: str, files: List[str], task_id: Optional[str] = None,
                ttl_minutes: int = 30, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Claim a set of file paths for an agent. Returns {lease_id, files, expires_at} on
    success, or {conflict, task_id, files, retry_after_seconds} if any file is held by
    another active lease. Same agent claiming its own files is idempotent (no conflict)."""
    now = time.time()
    file_set = set(files)
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            if lease["agent_id"] == agent_id:
                continue
            held = set(json.loads(lease["files"] or "[]"))
            overlap = file_set & held
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_minutes"] * 60
                remaining = max(0.0, expires_at - now)
                return {"conflict": lease["agent_id"], "task_id": lease.get("task_id"),
                        "files": sorted(overlap),
                        "retry_after_seconds": max(30, int(remaining / 2))}
        lease_id = f"lease-{agent_id}-{int(now)}"
        c.execute(
            "INSERT OR REPLACE INTO file_leases(id, agent_id, task_id, files, claimed_at, ttl_minutes) "
            "VALUES (?,?,?,?,?,?)",
            (lease_id, agent_id, task_id, json.dumps(sorted(files)), now, ttl_minutes),
        )
    expires_at = now + ttl_minutes * 60
    return {"lease_id": lease_id, "agent_id": agent_id, "task_id": task_id,
            "files": sorted(files), "expires_at": expires_at, "ttl_minutes": ttl_minutes}


def release_files(lease_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Release a lease by id. Returns {released: true} or {error: ...}."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE file_leases SET released_at=? WHERE id=? AND released_at IS NULL",
            (now, lease_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT id FROM file_leases WHERE id=?", (lease_id,)).fetchone()
            if r:
                return {"error": "lease already released", "lease_id": lease_id}
            return {"error": "lease not found", "lease_id": lease_id}
    return {"released": True, "lease_id": lease_id}


def check_files(files: List[str], project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """For each file path, return its holder if held by an active lease. Files not held
    are omitted. [{file, held_by, task_id, expires_at}]."""
    now = time.time()
    file_set = set(files)
    results = []
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            held = set(json.loads(lease["files"] or "[]"))
            for f in file_set & held:
                results.append({"file": f, "held_by": lease["agent_id"],
                                 "task_id": lease.get("task_id"),
                                 "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(results, key=lambda x: x["file"])


def request_unblock(requesting_agent: str, blocking_task_id: str,
                    blocked_task_id: str, message: str,
                    owner_agent: str, ack_deadline_minutes: int = 60,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a blocking dep request: agent on blocked_task_id asks owner_agent (working
    on blocking_task_id) to unblock. Returns message record with id to poll via
    get_message_status. Records the request as a 'dep_request' activity on both tasks."""
    payload = (f"[DEP REQUEST] Agent {requesting_agent} is blocked on {blocking_task_id} "
               f"while working on {blocked_task_id}. {message}")
    msg = send_agent_message(requesting_agent, owner_agent, payload,
                             task_id=blocked_task_id,
                             requires_ack=True,
                             ack_deadline_minutes=ack_deadline_minutes,
                             project=project)
    # Activity trail on both tasks
    for tid in (blocked_task_id, blocking_task_id):
        add_comment(tid, requesting_agent,
                    f"Unblock request sent to {owner_agent} re {blocking_task_id}: {message[:120]}",
                    kind="dep_request", project=project)
    return {"request_id": msg["id"], "from": requesting_agent, "to": owner_agent,
            "blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id,
            "poll_with": "get_message_status"}


def list_unblock_requests(owner_agent: str,
                          project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Return unacked blocking dep requests directed to this agent."""
    msgs = list_unacked_messages(owner_agent, project=project)
    return [m for m in msgs if "[DEP REQUEST]" in (m.get("message") or "")]


def record_decision(task_id: Optional[str], author: str, title: str,
                    context: str, decision: str, rationale: str,
                    supersedes: Optional[int] = None,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Append an architectural decision record (ADR-lite) to the decisions log.
    Immutable once written — to reverse, record a new decision with status='superseded'
    and reference the old id in supersedes. Returns the full record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "INSERT INTO decisions(task_id, author, title, context, decision, rationale, "
            "supersedes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, author, title, context, decision, rationale, supersedes, now),
        )
        dec_id = cur.lastrowid
        if supersedes:
            c.execute("UPDATE decisions SET status='superseded' WHERE id=?", (supersedes,))
    return {"id": dec_id, "task_id": task_id, "author": author, "title": title,
            "context": context, "decision": decision, "rationale": rationale,
            "status": "accepted", "supersedes": supersedes, "created_at": now}


def list_decisions(task_id: Optional[str] = None, status: str = "",
                   project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """List decisions, optionally filtered by task_id and/or status ('accepted',
    'superseded', 'proposed'). Returns newest-first."""
    q = "SELECT * FROM decisions WHERE 1=1"
    p: List[Any] = []
    if task_id:
        q += " AND task_id=?"; p.append(task_id)
    if status:
        q += " AND status=?"; p.append(status)
    q += " ORDER BY id DESC"
    with _conn(project) as c:
        rows = c.execute(q, p).fetchall()
    return [dict(r) for r in rows]


def get_decision(decision_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return dict(r) if r else None


def set_agent_state(task_id: str, agent_id: str, state: Dict[str, Any],
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Upsert this agent's state blob inside the task's agent_state JSON map.
    Other agents' state keys are preserved. Returns the full merged agent_state."""
    with _conn(project) as c:
        row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = json.loads(row["agent_state"] or "{}") if row["agent_state"] else {}
        current[agent_id] = state
        c.execute("UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
                  (json.dumps(current, sort_keys=True), time.time(), task_id))
    return current


def get_agent_state(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Return the full agent_state map for a task (all agents' state blobs)."""
    with _conn(project) as c:
        row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return {"error": "task not found", "task_id": task_id}
    return json.loads(row["agent_state"] or "{}") if row["agent_state"] else {}


def send_agent_message(from_agent: str, to_agent: str, message: str,
                       task_id: Optional[str] = None, requires_ack: bool = False,
                       ack_deadline_minutes: Optional[int] = None,
                       signal: Optional[str] = None, priority: int = 0,
                       principal_id: str = "", idem_key: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a directed message from one agent to another. Returns the message record."""
    now = time.time()
    deadline = (now + ack_deadline_minutes * 60) if ack_deadline_minutes else None
    payload = {"from_agent": from_agent, "to_agent": to_agent, "message": message,
               "task_id": task_id, "requires_ack": requires_ack,
               "ack_deadline_minutes": ack_deadline_minutes,
               "signal": signal, "priority": priority}
    with _conn(project) as c:
        hit = _idem_hit(c, "send", idem_key, from_agent, payload)
        if hit is not None:
            return hit
        cur = c.execute(
            "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, requires_ack, "
            "ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (from_agent, to_agent, task_id, message, 1 if requires_ack else 0, deadline, now,
             signal or None, int(priority or 0), idem_key or None, principal_id or None),
        )
        msg_id = cur.lastrowid
        response = {"id": msg_id, "from_agent": from_agent, "to_agent": to_agent,
                    "task_id": task_id, "message": message, "requires_ack": requires_ack,
                    "ack_deadline": deadline, "sent_at": now, "acked_at": None,
                    "signal": signal, "priority": int(priority or 0)}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, from_agent, "message.sent", json.dumps(response, sort_keys=True), now))
        _idem_store(c, "send", idem_key, from_agent, payload, response)
        return response


def ack_message(message_id: int, response: str = "",
                actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Mark a message as acknowledged by the receiving agent. Returns updated record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE agent_messages SET acked_at=?, ack_response=? WHERE id=? AND acked_at IS NULL",
            (now, response or None, message_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
            if r:
                return dict(r) | {"note": "already acked"}
            return {"error": "message not found", "id": message_id}
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (r["task_id"], actor, "message.acked",
                   json.dumps({"message_id": message_id, "response": response}, sort_keys=True), now))
    return dict(r)


def list_unacked_messages(to_agent: str, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Messages directed to this agent that have not been acknowledged yet."""
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM agent_messages WHERE to_agent=? AND acked_at IS NULL "
            "ORDER BY priority DESC, id",
            (to_agent,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message_status(message_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    """Sender polls this to see whether a message has been acked."""
    with _conn(project) as c:
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
    return dict(r) if r else None


def set_task_summary(task_id: str, rationale: str, activity_cursor: int,
                     project: str = DEFAULT_PROJECT) -> None:
    """Upsert the Haiku-generated rationale for a task."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO task_summaries(task_id, rationale, generated_at, activity_cursor) "
            "VALUES (?,?,?,?)",
            (task_id, rationale, time.time(), activity_cursor),
        )


def get_task_summary(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def get_tasks_needing_summary(project: str = DEFAULT_PROJECT,
                              min_interval: int = 900) -> List[str]:
    """Task IDs that have activity AND either no summary yet or new activity since the last
    summary (and enough time has passed to re-run — min_interval seconds)."""
    now = time.time()
    cutoff = now - min_interval
    with _conn(project) as c:
        rows = c.execute(
            """SELECT t.task_id,
                      MAX(a.id) AS max_act,
                      s.activity_cursor,
                      s.generated_at
               FROM tasks t
               JOIN activity a ON a.task_id = t.task_id
               LEFT JOIN task_summaries s ON s.task_id = t.task_id
               GROUP BY t.task_id""",
        ).fetchall()
    result = []
    for row in rows:
        task_id, max_act, cursor, gen_at = row[0], row[1], row[2], row[3]
        no_summary = cursor is None
        new_activity = (not no_summary) and (max_act > cursor)
        interval_ok = gen_at is None or gen_at < cutoff
        if (no_summary or new_activity) and interval_ok:
            result.append(task_id)
    return result


def list_active_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """All active leases board-wide (not released, not TTL-expired)."""
    now = time.time()
    with _conn(project) as c:
        leases = _active_leases_in(c, now)
    out = []
    for lease in leases:
        out.append({"lease_id": lease["id"], "agent_id": lease["agent_id"],
                    "task_id": lease.get("task_id"),
                    "files": json.loads(lease["files"] or "[]"),
                    "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(out, key=lambda x: x["lease_id"])


def delete_task(task_id: str, project: str = DEFAULT_PROJECT) -> bool:
    with _conn(project) as c:
        cur = c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM activity WHERE task_id=?", (task_id,))
        return cur.rowcount > 0


def get_meta(key: str, default=None, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def set_meta(key: str, value, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


def get_working_agreement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Canonical connect-time rules for agents in this workspace."""
    override = get_meta("working_agreement", {}, project=project) or {}
    default = {
        "project": project,
        "canonical_main_sha": get_meta("canonical_main_sha", None, project=project),
        "branch_convention": "claude/<TASK-ID>-<slug>",
        "definition_of_done": "merged to main with a recorded merge SHA — agents never self-set Done",
        "push_before_claiming_progress": True,
        "merge_strategy": "squash",
        "main_writes": "PR only — never push main directly",
        "ports_doc": "docs/PORTS.md",
        "byo_data": True,
        "session_start_sequence": [
            "get_working_agreement(project)",
            "register_agent",
            "inbox(unacked)",
            "check+claim before first write",
        ],
        "agent_completion_rule": "complete_claim moves work to In Review; only merge webhook sets Done",
    }
    return {**default, **override, "project": project}


def update_canonical_main_sha(sha: str, actor: str = "github-webhook",
                              project: str = DEFAULT_PROJECT) -> None:
    if not sha:
        return
    set_meta("canonical_main_sha", sha, project=project)
    append_activity("git.main_advanced", actor, {"canonical_main_sha": sha},
                    task_id=None, project=project)


def reconcile(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Local drift report for board provenance.

    This first pass intentionally avoids external GitHub calls; it catches board-internal
    contradictions and leaves deeper reachability/content checks for the runner/API-backed
    phase.
    """
    now = time.time()
    findings: List[Dict[str, Any]] = []
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        for row in rows:
            task = _task_row(row)
            git_state = _load_git_state(c, task["task_id"])
            status = task.get("status")
            if status == "Done" and not git_state.get("merged_sha"):
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_without_merged_sha",
                                 "detail": "Task is Done but has no recorded merge SHA."})
            if status == "In Review" and not (git_state.get("branch") or git_state.get("pr_url")):
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "review_without_provenance",
                                 "detail": "Task is In Review but lacks branch/PR evidence."})
            if status == "In Progress" and not git_state.get("head_sha"):
                findings.append({"severity": "low", "task_id": task["task_id"],
                                 "code": "progress_without_pushed_head",
                                 "detail": "Task is In Progress with no reported pushed head SHA."})
            _upsert_git_state(c, task["task_id"], {"last_reconciled_at": now})
        cursor = c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    agreement = get_working_agreement(project)
    if not agreement.get("canonical_main_sha"):
        findings.append({"severity": "medium", "task_id": None,
                         "code": "missing_canonical_main_sha",
                         "detail": "No canonical main SHA recorded yet; wait for a default-branch push webhook or set meta."})
    append_activity("reconcile.completed", "reconcile",
                    {"findings": len(findings)}, task_id=None, project=project)
    return {"project": project, "ok": not findings, "findings": findings,
            "activity_cursor": cursor, "checked_at": now,
            "external_checks": "not_configured"}


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
def add_chat(session: str, role: str, content: str, payload: Optional[Dict[str, Any]] = None,
             project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT INTO chat(session, role, content, payload, created_at) VALUES (?,?,?,?,?)",
                  (session, role, content, json.dumps(payload or {}), time.time()))


def clear_chat(session: str, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("DELETE FROM chat WHERE session=?", (session,))


def recent_chat(session: str, limit: int = 20, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
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


def all_rag_rows() -> List[Dict[str, Any]]:
    """rag_docs rows WITH ids — for re-embedding in place (rag.reembed_dynamic)."""
    with _conn() as c:
        rows = c.execute("SELECT id, source_kind, label, text FROM rag_docs ORDER BY id").fetchall()
    return [{"id": r["id"], "source_kind": r["source_kind"], "label": r["label"], "text": r["text"]} for r in rows]


def update_rag_embedding(rag_id: int, embedding: List[float]):
    with _conn() as c:
        c.execute("UPDATE rag_docs SET embedding=? WHERE id=?", (json.dumps(embedding), rag_id))


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


def update_inbox_triage(item_id: int, triage: Dict[str, Any]):
    """Rewrite an item's stored triage JSON — used after a PARTIAL confirm so the proposals
    that were held back (e.g. status->Done awaiting evidence) stay in the queue."""
    with _conn() as c:
        c.execute("UPDATE inbox SET triage=? WHERE id=?", (json.dumps(triage or {}), item_id))


def inbox_pending_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0]


def board_payload(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    tasks = list_tasks(project=project)
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload: Dict[str, Any] = {k: get_meta(k, project=project) for k in META_SECTIONS}
    payload["workstreams"] = list(by_ws.values())
    return payload
