"""SQLite store for the taikun-pm satellite — tasks + activity, seeded from a
bundled plan snapshot. One file, zero ops (see ADR 0007). No shared DB touched."""
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

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
PROJECT_REGISTRY_DB_PATH = os.environ.get(
    "PM_PROJECT_REGISTRY_DB_PATH",
    os.path.join(os.path.dirname(DB_PATH), "project_registry.db"),
)
PROJECT_ID_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
PROJECT_ID_VALID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")

# Multi-project registry. Each project is its OWN sqlite file — physical isolation, so a Helm
# request can never read or write Maxwell's rows (no shared table, no project_id column). The
# default is always 'maxwell', so every existing caller behaves exactly as before.
BUILTIN_PROJECTS = {
    "maxwell": {"db": DB_PATH, "seed": SEED_PATH,
                "label": "Project Maxwell", "pretitle": "TEEP Barnett · TotalEnergies E&P"},
    "helm": {"db": HELM_DB_PATH, "seed": HELM_SEED_PATH,
             "label": "Helm — Marine Nav Companion", "pretitle": "6th Element Labs · web-first chartplotter"},
    "switchboard": {"db": SWITCHBOARD_DB_PATH, "seed": SWITCHBOARD_SEED_PATH,
                    "label": "Switchboard — Agent Coordination Layer",
                    "pretitle": "6th Element Labs · live dogfood control plane"},
}
# Back-compat for older call sites that only need the built-in project set. New code should call
# project_ids(), has_project(), projects(), or _resolve() so dynamic projects are included.
PROJECTS = BUILTIN_PROJECTS
DEFAULT_PROJECT = "maxwell"
TASK_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")


def _registry_conn():
    os.makedirs(os.path.dirname(PROJECT_REGISTRY_DB_PATH), exist_ok=True)
    c = sqlite3.connect(PROJECT_REGISTRY_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_project_registry() -> None:
    with _registry_conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                pretitle   TEXT,
                db_path    TEXT NOT NULL,
                seed_path  TEXT,
                created_at REAL NOT NULL,
                created_by TEXT
            )
            """
        )


def normalize_project_id(value: str) -> str:
    """Turn a human project name like 'Vulkan Renderer' into a stable project id."""
    slug = PROJECT_ID_SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    return slug


def _dynamic_projects() -> Dict[str, Dict[str, str]]:
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return {
        r["id"]: {
            "db": r["db_path"],
            "seed": r["seed_path"],
            "label": r["label"],
            "pretitle": r["pretitle"] or "",
        }
        for r in rows
    }


def _project_map() -> Dict[str, Dict[str, str]]:
    return {**_dynamic_projects(), **BUILTIN_PROJECTS}


def project_ids() -> List[str]:
    return list(_project_map())


def has_project(project: Optional[str]) -> bool:
    return (project or DEFAULT_PROJECT) in _project_map()


def hash_token(token: str) -> str:
    """Stable one-way token hash for principal lookup."""
    return hashlib.sha256(("switchboard:" + (token or "")).encode("utf-8")).hexdigest()


def projects() -> List[Dict[str, Any]]:
    """The switcher's source of truth — [{id, label, pretitle}]."""
    return [{"id": k, "label": v["label"], "pretitle": v.get("pretitle", "")}
            for k, v in _project_map().items()]


def coerce_csv_list(value: Any) -> List[str]:
    """Normalize REST/CLI list fields that may arrive as a list or comma/newline string."""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in raw:
        for part in str(item).replace("\n", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _resolve(project: Optional[str]) -> Dict[str, str]:
    """Map a project id -> its config. Fail CLOSED on an unknown id — never silently fall back
    to Maxwell (which could leak a write across projects)."""
    p = _project_map().get(project or DEFAULT_PROJECT)
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

PROTOCOL_ENVELOPE = {
    "name": "switchboard",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profile_version": "2026-06-28",
    "profiles": {
        "ixp_core": "1.0",
        "txp_dispatch": "0.1",
        "oxp_tally": "0.1",
        "reconcile": "0.1",
    },
    "compatible_versions": ["ixp.v1"],
    "field_aliases": {
        "send_agent_message.ack_timeout_seconds": "ack_deadline_minutes",
        "send_agent_message.ack_timeout_s": "ack_deadline_minutes",
    },
}

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
            CREATE TABLE IF NOT EXISTS coordination_monitors (
                id              TEXT PRIMARY KEY,
                kind            TEXT NOT NULL,
                target_type     TEXT NOT NULL,
                target_id       TEXT NOT NULL,
                task_id         TEXT,
                owner_agent     TEXT,
                subject_agent   TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                deadline        REAL,
                condition_json  TEXT NOT NULL DEFAULT '{}',
                on_timeout_json TEXT NOT NULL DEFAULT '{}',
                result_json     TEXT NOT NULL DEFAULT '{}',
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                last_checked_at REAL,
                fired_at        REAL,
                resolved_at     REAL
            );
            CREATE INDEX IF NOT EXISTS ix_monitors_status
                ON coordination_monitors(status, deadline);
            CREATE INDEX IF NOT EXISTS ix_monitors_target
                ON coordination_monitors(target_type, target_id);
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
            CREATE TABLE IF NOT EXISTS outcomes (
                id             TEXT PRIMARY KEY,
                project        TEXT NOT NULL,
                task_id        TEXT,
                epic_id        TEXT,
                claim_id       TEXT,
                type           TEXT NOT NULL,
                title          TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'proposed',
                verifier       TEXT,
                verification   TEXT,
                evidence_json  TEXT NOT NULL DEFAULT '{}',
                value_json     TEXT NOT NULL DEFAULT '{}',
                created_at     REAL NOT NULL,
                verified_at    REAL
            );
            CREATE INDEX IF NOT EXISTS ix_outcomes_task ON outcomes(task_id, status);
            CREATE INDEX IF NOT EXISTS ix_outcomes_claim ON outcomes(claim_id);
            CREATE TABLE IF NOT EXISTS kpis (
                id             TEXT PRIMARY KEY,
                project        TEXT NOT NULL,
                name           TEXT NOT NULL,
                unit           TEXT NOT NULL,
                direction      TEXT NOT NULL,
                owner          TEXT,
                baseline_value REAL,
                current_value  REAL,
                target_value   REAL,
                period         TEXT,
                created_at     REAL NOT NULL,
                updated_at     REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_kpis_project ON kpis(project);
            CREATE TABLE IF NOT EXISTS outcome_kpi_links (
                id                TEXT PRIMARY KEY,
                project           TEXT NOT NULL,
                outcome_id        TEXT NOT NULL,
                kpi_id            TEXT NOT NULL,
                contribution      REAL,
                contribution_unit TEXT,
                confidence        TEXT NOT NULL,
                rationale         TEXT,
                created_at        REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_outcome_kpi_outcome ON outcome_kpi_links(outcome_id);
            CREATE INDEX IF NOT EXISTS ix_outcome_kpi_kpi ON outcome_kpi_links(kpi_id);
            CREATE TABLE IF NOT EXISTS task_summaries (
                task_id         TEXT PRIMARY KEY,
                rationale       TEXT NOT NULL,
                generated_at    REAL NOT NULL,
                activity_cursor INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS agent_hosts (
                host_id            TEXT PRIMARY KEY,
                hostname           TEXT,
                agent_host_version TEXT,
                repo_root          TEXT,
                runtimes_json      TEXT NOT NULL DEFAULT '[]',
                limits_json        TEXT NOT NULL DEFAULT '{}',
                capacity_json      TEXT NOT NULL DEFAULT '{}',
                principal_id       TEXT,
                registered_at      REAL NOT NULL,
                heartbeat_at       REAL NOT NULL,
                heartbeat_ttl_s    INTEGER NOT NULL DEFAULT 60,
                status             TEXT NOT NULL DEFAULT 'online',
                last_error         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_agent_hosts_heartbeat
                ON agent_hosts(status, heartbeat_at);
            CREATE TABLE IF NOT EXISTS wake_intents (
                wake_id           TEXT PRIMARY KEY,
                source            TEXT NOT NULL,
                reason            TEXT NOT NULL,
                selector_json     TEXT NOT NULL DEFAULT '{}',
                policy_json       TEXT NOT NULL DEFAULT '{}',
                status            TEXT NOT NULL DEFAULT 'pending',
                requested_at      REAL NOT NULL,
                deadline          REAL,
                claimed_at        REAL,
                claimed_by_host   TEXT,
                completed_at      REAL,
                runner_session_id TEXT,
                agent_id          TEXT,
                result_json       TEXT NOT NULL DEFAULT '{}',
                task_id           TEXT,
                principal_id      TEXT,
                idem_key          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_wake_intents_status
                ON wake_intents(status, deadline, requested_at);
            CREATE INDEX IF NOT EXISTS ix_wake_intents_host
                ON wake_intents(claimed_by_host, status);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_wake_intents_idem
                ON wake_intents(idem_key) WHERE idem_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS archived_tasks (
                archive_id          TEXT PRIMARY KEY,
                task_id             TEXT NOT NULL,
                operation           TEXT NOT NULL,
                actor               TEXT NOT NULL,
                reason              TEXT,
                source_project      TEXT NOT NULL,
                destination_project TEXT,
                snapshot_json       TEXT NOT NULL,
                created_at          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_archived_tasks_task
                ON archived_tasks(task_id, created_at);
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
        if not seed_path or not os.path.exists(seed_path):
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


def _json_payload(raw: str) -> Any:
    """Parse payload JSON while preserving legacy scalar payloads."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


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
        t["activity"] = [dict(a) | {"payload": _json_payload(a["payload"])}
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


def protocol_envelope() -> Dict[str, Any]:
    return json.loads(json.dumps(PROTOCOL_ENVELOPE))


def check_protocol_compatibility(advertised: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not advertised:
        return {"compatible": True, "mode": "legacy_assumed",
                "warnings": ["agent did not advertise protocol; treating as pre-PROTO-2"]}
    version = advertised.get("version") or advertised.get("ixp_version")
    supported = PROTOCOL_ENVELOPE["compatible_versions"]
    if version not in supported:
        return {"compatible": False, "mode": "reject",
                "reason": f"unsupported protocol version {version!r}; supported={supported}"}
    return {"compatible": True, "mode": "exact", "version": version,
            "profile": advertised.get("profile")}


def register_agent(agent_id: str, runtime: str, model: str = "", lane: str = "",
                   task_id: str = "", ttl_s: int = 120,
                   control: Optional[Dict[str, Any]] = None,
                   protocol: Optional[Dict[str, Any]] = None,
                   principal_id: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    ttl_s = max(10, int(ttl_s or 120))
    compatibility = check_protocol_compatibility(protocol)
    stored_control = dict(control or {})
    if protocol:
        stored_control["protocol"] = protocol
    stored_control["protocol_compatibility"] = compatibility
    control_json = json.dumps(stored_control, sort_keys=True)
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
                               "control": control or {}, "protocol": protocol or {},
                               "protocol_compatibility": compatibility}, sort_keys=True), now))
    return {"agent_id": agent_id, "runtime": runtime, "model": model or None,
            "lane": lane or None, "task_id": task_id or None,
            "control": control or {}, "protocol": protocol or {},
            "protocol_compatibility": compatibility, "registered_at": now,
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


def _json_obj(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _host_row(row: sqlite3.Row, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    d = dict(row)
    runtimes = _json_obj(d.pop("runtimes_json", "[]"), [])
    limits = _json_obj(d.pop("limits_json", "{}"), {})
    capacity = _json_obj(d.pop("capacity_json", "{}"), {})
    ttl_s = int(d.get("heartbeat_ttl_s") or 60)
    expires_at = float(d.get("heartbeat_at") or 0) + ttl_s
    active = int(capacity.get("active_sessions") or 0)
    max_sessions = limits.get("max_sessions")
    try:
        max_sessions = int(max_sessions) if max_sessions is not None else None
    except Exception:
        max_sessions = None
    d.update({
        "runtimes": runtimes,
        "limits": limits,
        "capacity": capacity,
        "expires_at": expires_at,
        "stale": now >= expires_at or d.get("status") != "online",
        "available_sessions": (max(0, max_sessions - active)
                               if max_sessions is not None else None),
    })
    return d


def _wake_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["selector"] = _json_obj(d.pop("selector_json", "{}"), {})
    d["policy"] = _json_obj(d.pop("policy_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    return d


def _selector_runtime_for_agent(agent_id: str) -> str:
    aid = (agent_id or "").lower()
    if aid.startswith("claude"):
        return "claude-code"
    if aid.startswith("codex"):
        return "codex"
    if aid.startswith("cursor"):
        return "cursor"
    if aid.startswith("langgraph"):
        return "langgraph"
    if aid.startswith("openai"):
        return "openai-loop"
    return ""


def _runtime_matches_selector(runtime: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    want_runtime = (selector.get("runtime") or "").strip()
    want_lane = (selector.get("lane") or "").strip()
    want_caps = {str(c).strip() for c in selector.get("capabilities") or [] if str(c).strip()}
    have_runtime = (runtime.get("runtime") or "").strip()
    if want_runtime and have_runtime != want_runtime:
        return False
    lanes = [str(x).strip() for x in runtime.get("lanes") or [] if str(x).strip()]
    if want_lane and lanes and want_lane not in lanes:
        return False
    caps = {str(c).strip() for c in runtime.get("capabilities") or [] if str(c).strip()}
    if want_caps and not want_caps.issubset(caps):
        return False
    return True


def _host_can_handle(host: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    if host.get("stale"):
        return False
    if host.get("available_sessions") is not None and host["available_sessions"] <= 0:
        return False
    return any(_runtime_matches_selector(rt, selector) for rt in host.get("runtimes") or [])


def _eligible_hosts_in(c: sqlite3.Connection, selector: Dict[str, Any],
                       now: float) -> List[Dict[str, Any]]:
    rows = c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC").fetchall()
    hosts = [_host_row(r, now=now) for r in rows]
    return [h for h in hosts if _host_can_handle(h, selector)]


def register_host(inventory: Dict[str, Any], principal_id: str = "",
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Register or refresh an always-on Agent Host inventory record."""
    now = time.time()
    host_id = (inventory.get("host_id") or "").strip()
    if not host_id:
        return {"error": "host_id required"}
    runtimes = inventory.get("runtimes") or []
    limits = inventory.get("limits") or {}
    capacity = inventory.get("capacity") or {}
    if "active_sessions" in inventory and "active_sessions" not in capacity:
        capacity["active_sessions"] = inventory.get("active_sessions")
    ttl_s = max(10, int(inventory.get("heartbeat_ttl_s") or inventory.get("ttl_s") or 60))
    with _conn(project) as c:
        c.execute(
            "INSERT INTO agent_hosts(host_id, hostname, agent_host_version, repo_root, "
            "runtimes_json, limits_json, capacity_json, principal_id, registered_at, "
            "heartbeat_at, heartbeat_ttl_s, status, last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(host_id) DO UPDATE SET hostname=excluded.hostname, "
            "agent_host_version=excluded.agent_host_version, repo_root=excluded.repo_root, "
            "runtimes_json=excluded.runtimes_json, limits_json=excluded.limits_json, "
            "capacity_json=excluded.capacity_json, principal_id=excluded.principal_id, "
            "heartbeat_at=excluded.heartbeat_at, heartbeat_ttl_s=excluded.heartbeat_ttl_s, "
            "status=excluded.status, last_error=NULL",
            (host_id, inventory.get("hostname") or None,
             inventory.get("agent_host_version") or None, inventory.get("repo_root") or None,
             json.dumps(runtimes, sort_keys=True), json.dumps(limits, sort_keys=True),
             json.dumps(capacity, sort_keys=True), principal_id or None, now, now, ttl_s,
             "online", None),
        )
        payload = {"host_id": host_id, "runtimes": runtimes, "limits": limits,
                   "heartbeat_ttl_s": ttl_s}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "agent_host.registered",
                   json.dumps(payload, sort_keys=True), now))
        row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
    return _host_row(row, now=now)


def heartbeat_host(host_id: str, active_sessions: Optional[int] = None,
                   capacity: Optional[Dict[str, Any]] = None,
                   status: str = "online", last_error: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
        if not row:
            return {"error": "host not registered", "host_id": host_id}
        current = _json_obj(row["capacity_json"], {})
        if capacity:
            current.update(capacity)
        if active_sessions is not None:
            current["active_sessions"] = int(active_sessions)
        c.execute(
            "UPDATE agent_hosts SET heartbeat_at=?, capacity_json=?, status=?, last_error=? "
            "WHERE host_id=?",
            (now, json.dumps(current, sort_keys=True), status or "online",
             last_error or None, host_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "agent_host.heartbeat",
                   json.dumps({"host_id": host_id, "capacity": current,
                               "status": status or "online"}, sort_keys=True), now))
        row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
    return _host_row(row, now=now)


def list_agent_hosts(runtime: str = "", lane: str = "", capability: str = "",
                     include_stale: bool = False,
                     project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    selector = {"runtime": runtime or "", "lane": lane or "",
                "capabilities": [capability] if capability else []}
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC").fetchall()
    hosts = [_host_row(r, now=now) for r in rows]
    out = []
    for host in hosts:
        if host.get("stale") and not include_stale:
            continue
        if (runtime or lane or capability) and not any(
            _runtime_matches_selector(rt, selector) for rt in host.get("runtimes") or []
        ):
            continue
        out.append(host)
    return out


def host_status(host_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
        if not row:
            return {"error": "host not registered", "host_id": host_id}
        host = _host_row(row, now=now)
        counts = c.execute(
            "SELECT status, COUNT(*) n FROM wake_intents WHERE claimed_by_host=? GROUP BY status",
            (host_id,),
        ).fetchall()
    host["wake_counts"] = {r["status"]: r["n"] for r in counts}
    return host


def _insert_wake_intent(c: sqlite3.Connection, selector: Dict[str, Any],
                        reason: str, source: str, policy: Dict[str, Any],
                        task_id: Optional[str], principal_id: str, actor: str,
                        now: float, idem_key: str = "") -> Dict[str, Any]:
    deadline_s = (policy.get("deadline_seconds") or policy.get("claim_timeout_s") or
                  policy.get("ttl_s"))
    deadline = now + float(deadline_s) if deadline_s else None
    eligible = _eligible_hosts_in(c, selector, now)
    no_host_policy = (policy.get("no_eligible_host") or "wait").strip()
    status = "failed" if no_host_policy == "fail" and not eligible else "pending"
    result = ({"reason": "no_eligible_host", "eligible_host_count": 0}
              if status == "failed" else {})
    wake_id = "wake-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO wake_intents(wake_id, source, reason, selector_json, policy_json, "
        "status, requested_at, deadline, result_json, task_id, principal_id, idem_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (wake_id, source, reason, json.dumps(selector, sort_keys=True),
         json.dumps(policy, sort_keys=True), status, now, deadline,
         json.dumps(result, sort_keys=True), task_id, principal_id or None, idem_key or None),
    )
    payload = {"wake_id": wake_id, "source": source, "reason": reason,
               "selector": selector, "policy": policy, "status": status,
               "eligible_host_count": len(eligible)}
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "wake.requested", json.dumps(payload, sort_keys=True), now))
    if not eligible:
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "wake.no_eligible_host",
                   json.dumps({"wake_id": wake_id, "selector": selector,
                               "status": status}, sort_keys=True), now))
    row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    wake = _wake_row(row)
    wake["eligible_host_count"] = len(eligible)
    wake["eligible_hosts"] = [h["host_id"] for h in eligible]
    return wake


def request_wake(selector: Dict[str, Any], reason: str = "",
                 source: str = "", policy: Optional[Dict[str, Any]] = None,
                 task_id: Optional[str] = None, principal_id: str = "",
                 actor: str = "system", idem_key: str = "",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    policy = dict(policy or {})
    selector = dict(selector or {})
    if not selector.get("runtime") and selector.get("agent_id"):
        runtime = _selector_runtime_for_agent(str(selector.get("agent_id") or ""))
        if runtime:
            selector["runtime"] = runtime
    if not selector.get("runtime") and not selector.get("agent_id"):
        return {"error": "selector.runtime or selector.agent_id required"}
    payload = {"selector": selector, "reason": reason or "wake requested",
               "source": source or actor, "policy": policy, "task_id": task_id}
    with _conn(project) as c:
        hit = _idem_hit(c, "request_wake", idem_key, actor, payload)
        if hit is not None:
            return hit
        wake = _insert_wake_intent(
            c, selector=selector, reason=reason or "wake requested",
            source=source or actor, policy=policy, task_id=task_id,
            principal_id=principal_id, actor=actor, now=now, idem_key=idem_key)
        _idem_store(c, "request_wake", idem_key, actor, payload, wake)
        return wake


def list_wake_intents(status: str = "", host_id: str = "", runtime: str = "",
                      project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM wake_intents WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if host_id:
        q += " AND claimed_by_host=?"; params.append(host_id)
    q += " ORDER BY requested_at"
    with _conn(project) as c:
        wakes = [_wake_row(r) for r in c.execute(q, params).fetchall()]
    if runtime:
        wakes = [w for w in wakes if (w.get("selector") or {}).get("runtime") == runtime]
    return wakes


def claim_wake(host_id: str, wake_id: str, actor: str = "system",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        wake_row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
        if not wake_row:
            return {"claimed": False, "error": "wake not found", "wake_id": wake_id}
        wake = _wake_row(wake_row)
        if wake["status"] != "pending":
            return {"claimed": False, "reason": f"wake is {wake['status']}", "wake": wake}
        if wake.get("deadline") and wake["deadline"] <= now:
            result = {"reason": "deadline_expired", "deadline": wake["deadline"]}
            c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                      "WHERE wake_id=?",
                      (now, json.dumps(result, sort_keys=True), wake_id))
            return {"claimed": False, "reason": "deadline_expired", "wake_id": wake_id}
        host_row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
        if not host_row:
            return {"claimed": False, "reason": "host_not_registered", "host_id": host_id}
        host = _host_row(host_row, now=now)
        if not _host_can_handle(host, wake["selector"]):
            return {"claimed": False, "reason": "host_not_eligible",
                    "host_id": host_id, "wake_id": wake_id}
        cur = c.execute(
            "UPDATE wake_intents SET status='claimed', claimed_at=?, claimed_by_host=? "
            "WHERE wake_id=? AND status='pending'",
            (now, host_id, wake_id),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            return {"claimed": False, "reason": "lost_race", "wake": _wake_row(row)}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (wake.get("task_id"), actor, "wake.claimed",
                   json.dumps({"wake_id": wake_id, "host_id": host_id}, sort_keys=True), now))
        row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    return {"claimed": True, "wake": _wake_row(row)}


def complete_wake(wake_id: str, runner_session_id: str = "",
                  agent_id: str = "", result: Optional[Dict[str, Any]] = None,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    result = dict(result or {})
    success = bool(result.get("started") or runner_session_id or agent_id)
    status = "completed" if success else "failed"
    if "reason" not in result:
        result["reason"] = "started" if success else "launch_failed"
    with _conn(project) as c:
        row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
        if not row:
            return {"error": "wake not found", "wake_id": wake_id}
        wake = _wake_row(row)
        c.execute(
            "UPDATE wake_intents SET status=?, completed_at=?, runner_session_id=?, "
            "agent_id=?, result_json=? WHERE wake_id=?",
            (status, now, runner_session_id or None, agent_id or None,
             json.dumps(result, sort_keys=True), wake_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (wake.get("task_id"), actor,
                   "wake.completed" if status == "completed" else "wake.failed",
                   json.dumps({"wake_id": wake_id, "status": status,
                               "runner_session_id": runner_session_id or None,
                               "agent_id": agent_id or None,
                               "result": result}, sort_keys=True), now))
        row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    return _wake_row(row)


def cancel_wake(wake_id: str, reason: str = "cancelled", actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
        if not row:
            return {"error": "wake not found", "wake_id": wake_id}
        wake = _wake_row(row)
        if wake["status"] in ("completed", "failed", "cancelled"):
            return wake | {"note": "already terminal"}
        result = dict(wake.get("result") or {})
        result.update({"reason": reason, "cancelled_by": actor})
        c.execute("UPDATE wake_intents SET status='cancelled', completed_at=?, result_json=? "
                  "WHERE wake_id=?",
                  (now, json.dumps(result, sort_keys=True), wake_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (wake.get("task_id"), actor, "wake.cancelled",
                   json.dumps({"wake_id": wake_id, "reason": reason}, sort_keys=True), now))
        row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    return _wake_row(row)


def sweep_wake_intents(project: str = DEFAULT_PROJECT,
                       now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    failed = 0
    events: List[Dict[str, Any]] = []
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM wake_intents WHERE status IN ('pending','claimed') "
            "AND deadline IS NOT NULL AND deadline<=?",
            (now,),
        ).fetchall()
        for row in rows:
            wake = _wake_row(row)
            result = dict(wake.get("result") or {})
            result.update({"reason": "deadline_expired", "deadline": wake.get("deadline")})
            c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                      "WHERE wake_id=?",
                      (now, json.dumps(result, sort_keys=True), wake["wake_id"]))
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), "switchboard/wake", "wake.failed",
                       json.dumps({"wake_id": wake["wake_id"], "reason": "deadline_expired"},
                                  sort_keys=True), now))
            failed += 1
            events.append({"wake_id": wake["wake_id"], "status": "failed",
                           "reason": "deadline_expired"})
    return {"project": project, "failed": failed, "events": events}


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


RISK_ORDER = {"low": 1, "medium": 2, "med": 2, "high": 3, "critical": 4}
CAPABILITY_RE = re.compile(
    r"(?:requires?\s+capabilit(?:y|ies)|required\s+capabilit(?:y|ies)|capabilities)\s*[:=]\s*([^\n.;]+)",
    re.I,
)


def _risk_value(risk: str) -> int:
    return RISK_ORDER.get((risk or "").strip().lower(), 0)


def _task_required_capabilities(task: Dict[str, Any]) -> List[str]:
    dispatch_state = ((task.get("agent_state") or {}).get("dispatch") or {})
    raw = (dispatch_state.get("required_capabilities") or
           dispatch_state.get("capabilities") or [])
    caps = coerce_csv_list(raw)
    if not caps:
        text = "\n".join(str(task.get(k) or "") for k in (
            "description", "entry_criteria", "exit_criteria", "deliverable"))
        for m in CAPABILITY_RE.finditer(text):
            caps.extend(coerce_csv_list(m.group(1)))
    return sorted({c.strip().lower() for c in caps if c and c.strip()})


def _task_tally_snapshot(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    outcomes = [_outcome_row(r) for r in c.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
    return {"spend": _spend_summary(_spend_for_task(c, task_id, outcomes)),
            "outcomes": outcomes}


def _budget_status(max_budget_usd: Optional[float], spent_usd: float) -> Dict[str, Any]:
    remaining = max_budget_usd - spent_usd if max_budget_usd is not None else None
    if max_budget_usd is None:
        status = "not_limited"
    elif remaining is not None and remaining < 0:
        status = "over_budget"
    elif max_budget_usd and spent_usd >= max_budget_usd * 0.9:
        status = "tight"
    else:
        status = "ok"
    return {"budget_usd": max_budget_usd, "spent_usd": round(spent_usd, 6),
            "remaining_usd": round(remaining, 6) if remaining is not None else None,
            "status": status}


def _dispatch_score(task: Dict[str, Any], requested_lanes: set,
                    requested_caps: set, tally: Dict[str, Any],
                    max_budget_usd: Optional[float]) -> Dict[str, Any]:
    sort_order = int(task.get("sort_order") or 0)
    lane = (task.get("_wsId") or "").upper()
    required_caps = _task_required_capabilities(task)
    matched_caps = sorted(set(required_caps) & requested_caps)
    capability_fit = ((len(matched_caps) / len(required_caps)) if required_caps else 1.0)
    budget = _budget_status(max_budget_usd, float(tally["spend"]["cost_usd"] or 0.0))
    verified = len([o for o in tally.get("outcomes", []) if o.get("status") == "verified"])
    proposed = len([o for o in tally.get("outcomes", []) if o.get("status") == "proposed"])
    factors = {
        "blocking": 10000 if task.get("is_blocking") else 0,
        "sort_order": max(0, 1000 - min(sort_order, 1000)),
        "lane_affinity": 250 if requested_lanes and lane in requested_lanes else 0,
        "capability_fit": int(capability_fit * 200),
        "risk_fit": max(0, 120 - (_risk_value(task.get("risk_level") or "") * 20)),
        "budget_fit": 100 if budget["status"] in ("not_limited", "ok") else 0,
        "verified_outcome_signal": min(verified, 5) * 15,
        "pending_value_signal": min(proposed, 5) * 5,
    }
    return {"score": sum(factors.values()), "factors": factors,
            "required_capabilities": required_caps, "matched_capabilities": matched_caps,
            "budget": budget}


def _model_recommendation(task: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, str]:
    risk = _risk_value(task.get("risk_level") or "")
    budget_status = score["budget"]["status"]
    if risk >= 3:
        tier = "high"
    elif budget_status == "tight":
        tier = "small"
    elif score["required_capabilities"]:
        tier = "balanced"
    else:
        tier = "small"
    return {"model_tier": tier,
            "reason": f"risk={task.get('risk_level') or 'unspecified'}, "
                      f"budget={budget_status}, "
                      f"capabilities={','.join(score['required_capabilities']) or 'none'}"}


def claim_next(agent_id: str, lanes: Any = None,
               capabilities: Any = None,
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
    lanes = coerce_csv_list(lanes)
    capabilities = coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _risk_value(max_risk)
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
        skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
                   "capability_mismatch": 0, "risk": 0, "budget": 0}
        for t in tasks:
            if t["task_id"] in active_claims:
                skipped["active_claim"] += 1
                continue
            if t.get("status") not in ready_statuses:
                skipped["status"] += 1
                continue
            if lane_set and (t.get("_wsId") or "").upper() not in lane_set:
                skipped["lane"] += 1
                continue
            if not _deps_done(t, by_id):
                skipped["dependencies"] += 1
                continue
            required_caps = _task_required_capabilities(t)
            if required_caps and not set(required_caps).issubset(cap_set):
                skipped["capability_mismatch"] += 1
                continue
            if max_risk_value and _risk_value(t.get("risk_level") or "") > max_risk_value:
                skipped["risk"] += 1
                continue
            tally = _task_tally_snapshot(c, t["task_id"])
            score = _dispatch_score(t, lane_set, cap_set, tally, max_budget_usd)
            if score["budget"]["status"] == "over_budget":
                skipped["budget"] += 1
                continue
            eligible.append((score["score"], -int(t.get("sort_order") or 0), t["task_id"], t, score))
        if not eligible:
            response = {"claimed": False, "reason": "no_unblocked_work",
                        "retry_after_seconds": 60,
                        "cursor": c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
                        "dispatch_reason": {"policy": "score.v1", "skipped": skipped,
                                            "candidate_count": 0}}
            _idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        _, _, _, task, selected_score = sorted(
            eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
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
        dispatch_reason = {"policy": "score.v1",
                           "score": selected_score["score"],
                           "factors": selected_score["factors"],
                           "required_capabilities": selected_score["required_capabilities"],
                           "matched_capabilities": selected_score["matched_capabilities"],
                           "skipped": skipped,
                           "candidate_count": len(eligible)}
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task["task_id"], "agent_id": agent_id,
                         "dispatch_reason": dispatch_reason}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task["task_id"], actor, "task.claimed",
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task["task_id"],)).fetchone())
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task["task_id"]], "expires_at": expires_at},
            "budget": selected_score["budget"],
            "dispatch_reason": dispatch_reason,
            "recommendation": _model_recommendation(task, selected_score),
        }
        _idem_store(c, "claim_next", idem_key, actor, payload, response)
        return response


def complete_claim(claim_id: str, evidence: str = "", final_status: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    evidence_obj = _parse_evidence(evidence)
    requested_status = (final_status or evidence_obj.get("final_status") or evidence_obj.get("status") or "").strip()
    done_requested = requested_status.lower() == "done" or str(evidence_obj.get("done", "")).lower() in ("1", "true", "yes")
    if done_requested and not evidence_obj:
        return {"error": "evidence required for final_status=Done", "claim_id": claim_id}
    next_status = "Done" if done_requested else "In Review"
    pushed_at = evidence_obj.get("pushed_at")
    if pushed_at is None and evidence_obj.get("head_sha"):
        pushed_at = now
    merged_at = evidence_obj.get("merged_at")
    if merged_at is None and evidence_obj.get("merged_sha"):
        merged_at = now
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
                  (now, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (next_status, now, row["task_id"]))
        git_state = _upsert_git_state(c, row["task_id"], {
            "branch": evidence_obj.get("branch"),
            "head_sha": evidence_obj.get("head_sha"),
            "pushed_at": pushed_at,
            "pr_number": evidence_obj.get("pr_number"),
            "pr_url": evidence_obj.get("pr_url"),
            "merged_sha": evidence_obj.get("merged_sha"),
            "merged_at": merged_at,
            "in_main_content": True if evidence_obj.get("merged_sha") else None,
            "evidence": evidence_obj,
        })
        if next_status == "Done":
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.done",
                       json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                                   "source": "complete_claim"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.completed",
                   json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                               "next_status": next_status}, sort_keys=True), now))
    return {"completed": True, "claim_id": claim_id, "task_id": row["task_id"],
            "status": next_status, "git_state": git_state}


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


def mark_task_default_branch_commit(task_id: str, commit_sha: str,
                                    branch: str = "master", subject: str = "",
                                    actor: str = "default-branch-backfill",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Bootstrap-only provenance repair for direct default-branch commits.

    Normal flow remains complete_claim -> In Review -> PR merge webhook -> Done. This is a
    system/reconcile escape hatch for pre-flow dogfood commits that are already on the default
    branch and mention a task id in their commit subject.
    """
    if not commit_sha:
        return {"error": "commit_sha required", "task_id": task_id}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        if row["status"] == "Done":
            return {"skipped": True, "reason": "already_done", "task_id": task_id}
        if row["status"] != "In Review":
            return {"skipped": True, "reason": "status_not_in_review",
                    "task_id": task_id, "status": row["status"]}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (now, task_id))
        evidence = {"source": "default_branch_backfill", "commit_sha": commit_sha,
                    "branch": branch, "subject": subject}
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": commit_sha,
            "pushed_at": now,
            "merged_sha": commit_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": evidence,
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.default_branch_backfilled",
                   json.dumps(evidence, sort_keys=True), now))
    return {"task_id": task_id, "status": "Done", "git_state": git_state}


def backfill_default_branch_commits(commits: List[Dict[str, Any]],
                                    branch: str = "master",
                                    actor: str = "github-webhook",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Stamp In Review tasks referenced by commits that already reached the default branch."""
    direct_backfilled: List[str] = []
    direct_backfill_skipped: List[Dict[str, str]] = []
    seen = set()
    for commit in commits or []:
        message = commit.get("message") or commit.get("subject") or ""
        sha = commit.get("id") or commit.get("sha") or commit.get("commit_sha") or ""
        if not sha:
            continue
        for task_id in dict.fromkeys(TASK_ID_RE.findall(message)):
            key = (task_id, sha)
            if key in seen:
                continue
            seen.add(key)
            res = mark_task_default_branch_commit(
                task_id, sha, branch=branch, subject=message,
                actor=actor, project=project)
            if res.get("status") == "Done":
                direct_backfilled.append(task_id)
            elif res.get("skipped") or res.get("reason") or res.get("error"):
                direct_backfill_skipped.append({
                    "task_id": task_id,
                    "reason": res.get("reason") or res.get("error") or "skipped",
                })
    return {"direct_backfilled_tasks": list(dict.fromkeys(direct_backfilled)),
            "direct_backfill_skipped": direct_backfill_skipped}


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
        if outcome_id and not task_id:
            outcome = c.execute("SELECT task_id FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
            if outcome:
                task_id = outcome["task_id"]
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


def _jsonish(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"text": value}
    return {"value": value}


def _outcome_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["evidence"] = json.loads(out.pop("evidence_json") or "{}")
    out["value"] = json.loads(out.pop("value_json") or "{}")
    return out


def _kpi_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def _outcome_kpi_link_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def record_outcome(outcome_type: str, title: str,
                   task_id: Optional[str] = None, claim_id: Optional[str] = None,
                   epic_id: Optional[str] = None, status: str = "proposed",
                   verifier: str = "", verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   value: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    status = (status or "proposed").strip().lower()
    if status not in ("proposed", "verified", "rejected", "superseded"):
        return {"error": "invalid outcome status", "status": status}
    if not outcome_type or not title:
        return {"error": "outcome_type and title required"}
    now = time.time()
    outcome_id = "outcome-" + uuid.uuid4().hex[:16]
    verified_at = now if status == "verified" else None
    with _conn(project) as c:
        c.execute(
            "INSERT INTO outcomes(id, project, task_id, epic_id, claim_id, type, title, status, "
            "verifier, verification, evidence_json, value_json, created_at, verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, project, task_id or None, epic_id or None, claim_id or None,
             outcome_type, title, status, verifier or None, verification or None,
             json.dumps(_jsonish(evidence), sort_keys=True),
             json.dumps(_jsonish(value), sort_keys=True), now, verified_at),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "tally.outcome_recorded",
                   json.dumps({"outcome_id": outcome_id, "status": status,
                               "type": outcome_type, "title": title}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def verify_outcome(outcome_id: str, verifier: str, verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        merged_evidence = json.loads(row["evidence_json"] or "{}")
        merged_evidence.update(_jsonish(evidence))
        c.execute(
            "UPDATE outcomes SET status='verified', verifier=?, verification=?, "
            "evidence_json=?, verified_at=? WHERE id=?",
            (verifier or actor, verification or None,
             json.dumps(merged_evidence, sort_keys=True), now, outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_verified",
                   json.dumps({"outcome_id": outcome_id, "verifier": verifier or actor,
                               "verification": verification or None}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def reject_outcome(outcome_id: str, verifier: str, reason: str,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        evidence = json.loads(row["evidence_json"] or "{}")
        evidence["rejection_reason"] = reason
        c.execute(
            "UPDATE outcomes SET status='rejected', verifier=?, verification='rejected', "
            "evidence_json=? WHERE id=?",
            (verifier or actor, json.dumps(evidence, sort_keys=True), outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_rejected",
                   json.dumps({"outcome_id": outcome_id, "reason": reason}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def create_kpi(name: str, unit: str, direction: str,
               owner: str = "", baseline_value: Optional[float] = None,
               current_value: Optional[float] = None,
               target_value: Optional[float] = None,
               period: str = "", actor: str = "tally",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    direction = (direction or "").strip().lower()
    if direction not in ("increase", "decrease", "maintain"):
        return {"error": "direction must be increase, decrease, or maintain"}
    if not name or not unit:
        return {"error": "name and unit required"}
    now = time.time()
    kpi_id = "kpi-" + uuid.uuid4().hex[:16]
    if current_value is None:
        current_value = baseline_value
    with _conn(project) as c:
        c.execute(
            "INSERT INTO kpis(id, project, name, unit, direction, owner, baseline_value, "
            "current_value, target_value, period, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kpi_id, project, name, unit, direction, owner or None, baseline_value,
             current_value, target_value, period or None, now, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_created",
                   json.dumps({"kpi_id": kpi_id, "name": name, "unit": unit,
                               "direction": direction}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def update_kpi_value(kpi_id: str, current_value: float,
                     evidence: Optional[Dict[str, Any]] = None,
                     actor: str = "tally",
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not row:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute("UPDATE kpis SET current_value=?, updated_at=? WHERE id=?",
                  (current_value, now, kpi_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_updated",
                   json.dumps({"kpi_id": kpi_id, "current_value": current_value,
                               "evidence": _jsonish(evidence)}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def link_outcome_to_kpi(outcome_id: str, kpi_id: str,
                        contribution: Optional[float] = None,
                        contribution_unit: str = "",
                        confidence: str = "directional",
                        rationale: str = "",
                        actor: str = "tally",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    confidence = (confidence or "directional").strip().lower()
    if confidence not in ("measured", "estimated", "directional"):
        return {"error": "confidence must be measured, estimated, or directional"}
    now = time.time()
    link_id = "okpi-" + uuid.uuid4().hex[:16]
    with _conn(project) as c:
        outcome = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not outcome:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute(
            "INSERT INTO outcome_kpi_links(id, project, outcome_id, kpi_id, contribution, "
            "contribution_unit, confidence, rationale, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (link_id, project, outcome_id, kpi_id, contribution, contribution_unit or kpi["unit"],
             confidence, rationale or None, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (outcome["task_id"], actor, "tally.outcome_kpi_linked",
                   json.dumps({"link_id": link_id, "outcome_id": outcome_id, "kpi_id": kpi_id,
                               "contribution": contribution, "confidence": confidence},
                              sort_keys=True), now))
        row = c.execute("SELECT * FROM outcome_kpi_links WHERE id=?", (link_id,)).fetchone()
    return _outcome_kpi_link_row(row)


def _spend_for_task(c: sqlite3.Connection, task_id: str,
                    outcomes: List[Dict[str, Any]]) -> List[sqlite3.Row]:
    outcome_ids = [o["id"] for o in outcomes]
    claim_ids = [o["claim_id"] for o in outcomes if o.get("claim_id")]
    clauses = ["task_id=?"]
    params: List[Any] = [task_id]
    if outcome_ids:
        clauses.append("outcome_id IN (%s)" % ",".join("?" for _ in outcome_ids))
        params.extend(outcome_ids)
    if claim_ids:
        clauses.append("claim_id IN (%s)" % ",".join("?" for _ in claim_ids))
        params.extend(claim_ids)
    return c.execute("SELECT * FROM llm_spend WHERE " + " OR ".join(clauses), params).fetchall()


def _spend_summary(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    spend = {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}}
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
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
    return spend


def task_tally(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        outcome_rows = c.execute("SELECT * FROM outcomes WHERE task_id=? ORDER BY created_at",
                                 (task_id,)).fetchall()
        outcomes = [_outcome_row(r) for r in outcome_rows]
        rows = _spend_for_task(c, task_id, outcomes)
        links: List[Dict[str, Any]] = []
        if outcomes:
            outcome_ids = [o["id"] for o in outcomes]
            link_rows = c.execute(
                "SELECT l.*, k.name, k.unit, k.direction FROM outcome_kpi_links l "
                "JOIN kpis k ON k.id=l.kpi_id WHERE l.outcome_id IN (%s)"
                % ",".join("?" for _ in outcome_ids), outcome_ids).fetchall()
            links = [dict(r) for r in link_rows]
    spend = _spend_summary(rows)
    outcome_counts = {"verified": 0, "proposed": 0, "rejected": 0, "superseded": 0}
    by_outcome = {o["id"]: o for o in outcomes}
    for outcome in outcomes:
        outcome_counts[outcome["status"]] = outcome_counts.get(outcome["status"], 0) + 1
    verified_count = outcome_counts.get("verified", 0)
    cost_per_outcome = (round(spend["cost_usd"] / verified_count, 6)
                        if verified_count else None)
    kpi_groups: Dict[str, Dict[str, Any]] = {}
    for link in links:
        outcome = by_outcome.get(link["outcome_id"]) or {}
        group = kpi_groups.setdefault(link["kpi_id"], {
            "kpi_id": link["kpi_id"],
            "name": link["name"],
            "unit": link["unit"],
            "direction": link["direction"],
            "verified_contribution": 0.0,
            "links": [],
            "cost_per_contribution_unit": None,
        })
        link_payload = {k: link.get(k) for k in ("id", "outcome_id", "contribution",
                                                 "contribution_unit", "confidence", "rationale")}
        link_payload["outcome_status"] = outcome.get("status")
        group["links"].append(link_payload)
        if outcome.get("status") == "verified" and link.get("contribution") is not None:
            group["verified_contribution"] += float(link["contribution"] or 0.0)
    for group in kpi_groups.values():
        if group["verified_contribution"]:
            group["cost_per_contribution_unit"] = round(
                spend["cost_usd"] / group["verified_contribution"], 6)
    return {"task_id": task_id, "spend": spend,
            "unit_cost": {"cost_per_verified_outcome": cost_per_outcome},
            "outcomes": outcome_counts,
            "outcome_records": outcomes,
            "kpis": list(kpi_groups.values())}


def kpi_tally(kpi_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        rows = c.execute(
            "SELECT o.*, l.id link_id, l.contribution, l.contribution_unit, "
            "l.confidence link_confidence, l.rationale "
            "FROM outcome_kpi_links l JOIN outcomes o ON o.id=l.outcome_id "
            "WHERE l.kpi_id=? ORDER BY l.created_at",
            (kpi_id,),
        ).fetchall()
    outcomes = []
    verified_contribution = 0.0
    task_ids = set()
    for row in rows:
        outcome = _outcome_row(row)
        outcome["link"] = {
            "id": row["link_id"],
            "contribution": row["contribution"],
            "contribution_unit": row["contribution_unit"],
            "confidence": row["link_confidence"],
            "rationale": row["rationale"],
        }
        outcomes.append(outcome)
        if outcome["status"] == "verified" and row["contribution"] is not None:
            verified_contribution += float(row["contribution"] or 0.0)
        if outcome.get("task_id"):
            task_ids.add(outcome["task_id"])
    spend_rows = []
    for task_id in task_ids:
        with _conn(project) as c:
            task_outcomes = [_outcome_row(r) for r in c.execute(
                "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
            spend_rows.extend(_spend_for_task(c, task_id, task_outcomes))
    spend = _spend_summary(spend_rows)
    return {
        "kpi": _kpi_row(kpi),
        "spend": spend,
        "outcomes": outcomes,
        "verified_contribution": round(verified_contribution, 6),
        "unit_cost": {
            "cost_per_contribution_unit": (
                round(spend["cost_usd"] / verified_contribution, 6)
                if verified_contribution else None
            )
        },
    }


def _merge_spend_totals(target: Dict[str, Any], spend: Dict[str, Any]) -> None:
    target["cost_usd"] = round(float(target.get("cost_usd") or 0.0) +
                              float(spend.get("cost_usd") or 0.0), 6)
    target["total_tokens"] = int(target.get("total_tokens") or 0) + int(spend.get("total_tokens") or 0)
    by_source = target.setdefault("by_source", {})
    for source, bucket in (spend.get("by_source") or {}).items():
        dst = by_source.setdefault(source, {
            "cost_usd": 0.0,
            "total_tokens": 0,
            "confidence": bucket.get("confidence"),
        })
        dst["cost_usd"] = round(float(dst.get("cost_usd") or 0.0) +
                                float(bucket.get("cost_usd") or 0.0), 6)
        dst["total_tokens"] = int(dst.get("total_tokens") or 0) + int(bucket.get("total_tokens") or 0)
        if bucket.get("confidence"):
            dst["confidence"] = bucket["confidence"]


def project_tally(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Project-level economic surface for TALLY-3.

    This intentionally derives from task_tally/kpi_tally so the board UI and API present the
    same semantics as the lower-level OXP/Tally primitives: verified outcomes are the denominator,
    proposed outcomes stay visible but do not count, and spend remains separated by source.
    """
    tasks = list_tasks(project=project)
    totals = {
        "task_count": len(tasks),
        "tasks_with_spend": 0,
        "tasks_with_verified_outcomes": 0,
        "verified_outcomes": 0,
        "proposed_outcomes": 0,
        "rejected_outcomes": 0,
        "superseded_outcomes": 0,
        "verified_kpi_contribution": 0.0,
        "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
        "unit_cost": {
            "cost_per_verified_outcome": None,
            "cost_per_kpi_contribution_unit": None,
        },
    }
    by_workstream: Dict[str, Dict[str, Any]] = {}
    by_task: List[Dict[str, Any]] = []

    for task in tasks:
        tid = task["task_id"]
        tally = task_tally(tid, project=project)
        spend = tally.get("spend") or {}
        outcomes = tally.get("outcomes") or {}
        verified = int(outcomes.get("verified") or 0)
        proposed = int(outcomes.get("proposed") or 0)
        rejected = int(outcomes.get("rejected") or 0)
        superseded = int(outcomes.get("superseded") or 0)
        cost = float(spend.get("cost_usd") or 0.0)
        tokens = int(spend.get("total_tokens") or 0)
        kpi_groups = tally.get("kpis") or []
        kpi_contribution = round(sum(float(k.get("verified_contribution") or 0.0)
                                     for k in kpi_groups), 6)
        _merge_spend_totals(totals["spend"], spend)
        totals["verified_outcomes"] += verified
        totals["proposed_outcomes"] += proposed
        totals["rejected_outcomes"] += rejected
        totals["superseded_outcomes"] += superseded
        totals["verified_kpi_contribution"] = round(
            totals["verified_kpi_contribution"] + kpi_contribution, 6)
        if cost:
            totals["tasks_with_spend"] += 1
        if verified:
            totals["tasks_with_verified_outcomes"] += 1

        ws_id = task.get("_wsId") or task.get("workstream_id") or "UNKNOWN"
        ws = by_workstream.setdefault(ws_id, {
            "workstream_id": ws_id,
            "name": task.get("_wsName") or task.get("workstream_name") or ws_id,
            "task_count": 0,
            "tasks_with_spend": 0,
            "verified_outcomes": 0,
            "proposed_outcomes": 0,
            "verified_kpi_contribution": 0.0,
            "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
            "unit_cost": {"cost_per_verified_outcome": None},
        })
        ws["task_count"] += 1
        if cost:
            ws["tasks_with_spend"] += 1
        ws["verified_outcomes"] += verified
        ws["proposed_outcomes"] += proposed
        ws["verified_kpi_contribution"] = round(ws["verified_kpi_contribution"] + kpi_contribution, 6)
        _merge_spend_totals(ws["spend"], spend)

        if cost or tokens or verified or proposed or rejected or superseded or kpi_groups:
            by_task.append({
                "task_id": tid,
                "title": task.get("title"),
                "workstream_id": ws_id,
                "workstream_name": task.get("_wsName") or task.get("workstream_name"),
                "status": task.get("status"),
                "spend": spend,
                "outcomes": outcomes,
                "unit_cost": tally.get("unit_cost") or {},
                "verified_kpi_contribution": kpi_contribution,
                "kpis": kpi_groups,
            })

    if totals["verified_outcomes"]:
        totals["unit_cost"]["cost_per_verified_outcome"] = round(
            totals["spend"]["cost_usd"] / totals["verified_outcomes"], 6)
    if totals["verified_kpi_contribution"]:
        totals["unit_cost"]["cost_per_kpi_contribution_unit"] = round(
            totals["spend"]["cost_usd"] / totals["verified_kpi_contribution"], 6)
    for ws in by_workstream.values():
        if ws["verified_outcomes"]:
            ws["unit_cost"]["cost_per_verified_outcome"] = round(
                ws["spend"]["cost_usd"] / ws["verified_outcomes"], 6)

    with _conn(project) as c:
        kpi_ids = [r["id"] for r in c.execute("SELECT id FROM kpis ORDER BY name").fetchall()]
    kpis = []
    for kpi_id in kpi_ids:
        kt = kpi_tally(kpi_id, project=project)
        kpis.append({
            "kpi": kt.get("kpi"),
            "spend": kt.get("spend"),
            "outcomes": kt.get("outcomes"),
            "verified_contribution": kt.get("verified_contribution"),
            "unit_cost": kt.get("unit_cost"),
        })

    return {
        "project": project,
        "totals": totals,
        "by_workstream": sorted(by_workstream.values(),
                                key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                               x["workstream_id"])),
        "by_task": sorted(by_task, key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                                  x["task_id"])),
        "kpis": kpis,
    }


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
                       ack_timeout_seconds: Optional[float] = None,
                       signal: Optional[str] = None, priority: int = 0,
                       on_ack_timeout: str = "notify_sender",
                       principal_id: str = "", idem_key: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a directed message from one agent to another. Returns the message record."""
    now = time.time()
    if ack_deadline_minutes is None and ack_timeout_seconds is not None:
        ack_deadline_minutes = float(ack_timeout_seconds) / 60.0
    deadline = (now + ack_deadline_minutes * 60) if ack_deadline_minutes else None
    payload = {"from_agent": from_agent, "to_agent": to_agent, "message": message,
               "task_id": task_id, "requires_ack": requires_ack,
               "ack_deadline_minutes": ack_deadline_minutes,
               "ack_timeout_seconds": ack_timeout_seconds,
               "signal": signal, "priority": priority,
               "on_ack_timeout": on_ack_timeout}
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
        if requires_ack:
            monitor = _create_ack_monitor(c, msg_id, from_agent, to_agent, task_id,
                                          deadline, now, on_ack_timeout=on_ack_timeout)
            response["monitor_id"] = monitor["id"]
            response["monitor"] = monitor
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, from_agent, "message.sent", json.dumps(response, sort_keys=True), now))
        _idem_store(c, "send", idem_key, from_agent, payload, response)
        return response


def _monitor_row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    d = dict(r)
    for k in ("condition_json", "on_timeout_json", "result_json"):
        raw = d.pop(k, "{}")
        d[k[:-5] if k.endswith("_json") else k] = json.loads(raw or "{}")
    return d


def _create_ack_monitor(c: sqlite3.Connection, message_id: int, from_agent: str,
                        to_agent: str, task_id: Optional[str], deadline: Optional[float],
                        now: float, on_ack_timeout: str = "notify_sender") -> Dict[str, Any]:
    monitor_id = f"mon-{uuid.uuid4().hex[:16]}"
    condition = {"type": "message_ack", "message_id": message_id}
    action = (on_ack_timeout or "notify_sender").strip()
    if action not in ("notify_sender", "wake_target", "wake_or_operator_alert"):
        action = "notify_sender"
    on_timeout = {"action": action, "signal": "ack_timeout"}
    c.execute(
        "INSERT INTO coordination_monitors"
        "(id, kind, target_type, target_id, task_id, owner_agent, subject_agent, status, "
        "deadline, condition_json, on_timeout_json, result_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (monitor_id, "ack_deadline", "agent_message", str(message_id), task_id,
         from_agent, to_agent, "pending", deadline,
         json.dumps(condition, sort_keys=True), json.dumps(on_timeout, sort_keys=True),
         "{}", now, now),
    )
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, "switchboard/monitor", "monitor.created",
               json.dumps({"monitor_id": monitor_id, "kind": "ack_deadline",
                           "message_id": message_id, "deadline": deadline,
                           "owner_agent": from_agent, "subject_agent": to_agent},
                          sort_keys=True), now))
    return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                  (monitor_id,)).fetchone()) or {}


def _load_monitor_for_message(c: sqlite3.Connection, message_id: int) -> Optional[Dict[str, Any]]:
    return _monitor_row(c.execute(
        "SELECT * FROM coordination_monitors WHERE kind='ack_deadline' "
        "AND target_type='agent_message' AND target_id=? ORDER BY created_at DESC LIMIT 1",
        (str(message_id),),
    ).fetchone())


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
                msg = dict(r) | {"note": "already acked"}
                msg["monitor"] = _load_monitor_for_message(c, message_id)
                return msg
            return {"error": "message not found", "id": message_id}
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        mon = _load_monitor_for_message(c, message_id)
        if mon and mon.get("status") in ("pending", "fired"):
            c.execute(
                "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                (now, now, now,
                 json.dumps({"acked_at": now, "ack_response": response}, sort_keys=True),
                 mon["id"]),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (r["task_id"], "switchboard/monitor", "monitor.resolved",
                       json.dumps({"monitor_id": mon["id"], "message_id": message_id,
                                   "reason": "acked"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (r["task_id"], actor, "message.acked",
                   json.dumps({"message_id": message_id, "response": response}, sort_keys=True), now))
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
    return out


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
        if not r:
            return None
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
        return out


def list_pending_acks(agent_id: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Unacked required messages plus their durable monitor state."""
    q = ("SELECT * FROM agent_messages WHERE requires_ack=1 AND acked_at IS NULL")
    params: List[Any] = []
    if agent_id:
        q += " AND (from_agent=? OR to_agent=?)"
        params.extend([agent_id, agent_id])
    q += " ORDER BY COALESCE(ack_deadline, 9999999999999), priority DESC, id"
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        out = []
        for r in rows:
            msg = dict(r)
            msg["monitor"] = _load_monitor_for_message(c, int(r["id"]))
            out.append(msg)
        return out


def list_coordination_monitors(status: str = "", kind: str = "",
                               project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM coordination_monitors WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if kind:
        q += " AND kind=?"; params.append(kind)
    q += " ORDER BY COALESCE(deadline, 9999999999999), created_at"
    with _conn(project) as c:
        return [_monitor_row(r) or {} for r in c.execute(q, params).fetchall()]


def resolve_monitor(monitor_id: str, reason: str = "manual",
                    actor: str = "system",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "resolved":
            return mon | {"note": "already resolved"}
        result = dict(mon.get("result") or {})
        result.update({"resolved_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.resolved",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}


def cancel_monitor(monitor_id: str, reason: str = "cancelled",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "cancelled":
            return mon | {"note": "already cancelled"}
        result = dict(mon.get("result") or {})
        result.update({"cancelled_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.cancelled",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}


def sweep_coordination_monitors(project: str = DEFAULT_PROJECT,
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Evaluate durable monitors. Designed for a Switchboard-owned timer or explicit tool call."""
    now = time.time() if now is None else float(now)
    checked = resolved = fired = 0
    events: List[Dict[str, Any]] = []
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM coordination_monitors WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        for row in rows:
            checked += 1
            mon = _monitor_row(row) or {}
            if mon.get("kind") != "ack_deadline":
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
                continue
            msg = c.execute("SELECT * FROM agent_messages WHERE id=?",
                            (int(mon.get("target_id") or 0),)).fetchone()
            if not msg:
                result = {"reason": "target_missing"}
                c.execute(
                    "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                events.append({"monitor_id": mon["id"], "status": "cancelled",
                               "reason": "target_missing"})
                continue
            if msg["acked_at"] is not None:
                result = {"acked_at": msg["acked_at"], "ack_response": msg["ack_response"]}
                c.execute(
                    "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (mon.get("task_id"), "switchboard/monitor", "monitor.resolved",
                           json.dumps({"monitor_id": mon["id"], "message_id": msg["id"],
                                       "reason": "acked"}, sort_keys=True), now))
                resolved += 1
                events.append({"monitor_id": mon["id"], "status": "resolved",
                               "message_id": msg["id"]})
                continue
            deadline = mon.get("deadline")
            if deadline is not None and deadline <= now:
                action = (mon.get("on_timeout") or {}).get("action") or "notify_sender"
                result = {"reason": "ack_timeout", "deadline": deadline, "fired_at": now,
                          "on_timeout": action}
                c.execute(
                    "UPDATE coordination_monitors SET status='fired', fired_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                payload = {"monitor_id": mon["id"], "message_id": msg["id"],
                           "from_agent": msg["from_agent"], "to_agent": msg["to_agent"],
                           "deadline": deadline}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "monitor.timeout",
                           json.dumps(payload, sort_keys=True), now))
                notice = (f"Ack timeout for message {msg['id']} to {msg['to_agent']} "
                          f"on task {msg['task_id'] or '(none)'}.")
                cur = c.execute(
                    "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, "
                    "requires_ack, ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("switchboard/monitor", msg["from_agent"], msg["task_id"], notice,
                     0, None, now, "ack_timeout", 100, None, None),
                )
                notice_payload = {"id": cur.lastrowid, "from_agent": "switchboard/monitor",
                                  "to_agent": msg["from_agent"], "task_id": msg["task_id"],
                                  "message": notice, "requires_ack": False,
                                  "signal": "ack_timeout", "priority": 100,
                                  "sent_at": now}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "message.sent",
                           json.dumps(notice_payload, sort_keys=True), now))
                wake = None
                if action in ("wake_target", "wake_or_operator_alert"):
                    selector = {"agent_id": msg["to_agent"]}
                    runtime = _selector_runtime_for_agent(msg["to_agent"])
                    if runtime:
                        selector["runtime"] = runtime
                    wake = _insert_wake_intent(
                        c, selector=selector, reason="ack_timeout",
                        source=f"monitor:{mon['id']}",
                        policy={"no_eligible_host": "wait",
                                "operator_alert": action == "wake_or_operator_alert"},
                        task_id=msg["task_id"], principal_id="",
                        actor="switchboard/monitor", now=now,
                        idem_key=f"ack-timeout:{mon['id']}")
                    result["wake_id"] = wake["wake_id"]
                    result["wake_status"] = wake["status"]
                    c.execute(
                        "UPDATE coordination_monitors SET result_json=? WHERE id=?",
                        (json.dumps(result, sort_keys=True), mon["id"]),
                    )
                fired += 1
                event = {"monitor_id": mon["id"], "status": "fired",
                         "message_id": msg["id"], "notice_id": cur.lastrowid}
                if wake:
                    event["wake_id"] = wake["wake_id"]
                    event["wake_status"] = wake["status"]
                events.append(event)
            else:
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
    wake_sweep = sweep_wake_intents(project=project, now=now)
    return {"project": project, "checked": checked, "resolved": resolved,
            "fired": fired, "events": events, "wake_sweep": wake_sweep}


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


TASK_MOVE_TABLES = (
    "activity",
    "task_git_state",
    "task_summaries",
    "llm_spend",
    "outcomes",
    "task_claims",
    "file_leases",
    "resource_leases",
    "decisions",
)
AUTOINCREMENT_TASK_TABLES = {"activity", "llm_spend", "decisions"}


def _table_columns(c: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_row(c: sqlite3.Connection, table: str, row: Dict[str, Any],
                skip_columns: Optional[set] = None) -> None:
    skip_columns = skip_columns or set()
    cols = [col for col in _table_columns(c, table) if col in row and col not in skip_columns]
    if not cols:
        return
    placeholders = ",".join("?" for _ in cols)
    c.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[col] for col in cols],
    )


def _rows_for_task(c: sqlite3.Connection, table: str, task_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE task_id=?",
                                       (task_id,)).fetchall()]


def _task_snapshot_in(c: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        return None
    snapshot: Dict[str, Any] = {"task": dict(task)}
    for table in TASK_MOVE_TABLES:
        snapshot[table] = _rows_for_task(c, table, task_id)
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        snapshot["outcome_kpi_links"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                outcome_ids,
            ).fetchall()
        ]
    else:
        snapshot["outcome_kpi_links"] = []
    kpi_ids = sorted({r["kpi_id"] for r in snapshot.get("outcome_kpi_links", [])
                      if r.get("kpi_id")})
    if kpi_ids:
        placeholders = ",".join("?" for _ in kpi_ids)
        snapshot["kpis"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM kpis WHERE id IN ({placeholders})", kpi_ids,
            ).fetchall()
        ]
    else:
        snapshot["kpis"] = []
    snapshot["agent_messages"] = _rows_for_task(c, "agent_messages", task_id)
    snapshot["coordination_monitors"] = _rows_for_task(c, "coordination_monitors", task_id)
    return snapshot


def _active_task_state_in(c: sqlite3.Connection, task_id: str, now: float) -> Dict[str, Any]:
    active_claims = [dict(r) for r in c.execute(
        "SELECT id, agent_id, expires_at FROM task_claims "
        "WHERE task_id=? AND status='active' AND expires_at>?",
        (task_id, now),
    ).fetchall()]
    active_resource_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, resource_type, names, claimed_at, ttl_seconds FROM resource_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + ttl_seconds > ?",
        (task_id, now),
    ).fetchall()]
    active_file_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, files, claimed_at, ttl_minutes FROM file_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + (ttl_minutes * 60) > ?",
        (task_id, now),
    ).fetchall()]
    return {"claims": active_claims, "resource_leases": active_resource_leases,
            "file_leases": active_file_leases}


def _insert_archive_in(c: sqlite3.Connection, task_id: str, operation: str, actor: str,
                       reason: str, source_project: str, destination_project: str,
                       snapshot: Dict[str, Any], now: float) -> str:
    archive_id = "archive-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO archived_tasks(archive_id, task_id, operation, actor, reason, "
        "source_project, destination_project, snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (archive_id, task_id, operation, actor, reason or None, source_project,
         destination_project or None, json.dumps(snapshot, sort_keys=True), now),
    )
    return archive_id


def _delete_task_related_in(c: sqlite3.Connection, task_id: str, snapshot: Dict[str, Any]) -> None:
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        c.execute(f"DELETE FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                  outcome_ids)
    for table in (
        "activity",
        "task_git_state",
        "task_summaries",
        "llm_spend",
        "outcomes",
        "task_claims",
        "file_leases",
        "resource_leases",
        "decisions",
        "agent_messages",
        "coordination_monitors",
    ):
        c.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))


def _apply_task_id(row: Dict[str, Any], old_task_id: str, new_task_id: str) -> Dict[str, Any]:
    out = dict(row)
    if out.get("task_id") == old_task_id:
        out["task_id"] = new_task_id
    return out


def _missing_dependencies(depends_on: List[str], project: str) -> List[str]:
    return [dep for dep in depends_on if not get_task(dep, project=project)]


def get_archived_task(archive_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        row = c.execute("SELECT * FROM archived_tasks WHERE archive_id=?",
                        (archive_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["snapshot"] = json.loads(out.pop("snapshot_json") or "{}")
        return out


def archive_task(task_id: str, reason: str = "", actor: str = "system",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not has_project(project):
        return {"error": f"unknown project: {project}", "project": project}
    now = time.time()
    with _conn(project) as c:
        snapshot = _task_snapshot_in(c, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id, "project": project}
        active = _active_task_state_in(c, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project, "active": active}
        archive_id = _insert_archive_in(
            c, task_id, "archive", actor, reason, project, "", snapshot, now)
        _delete_task_related_in(c, task_id, snapshot)
    return {"archived": True, "archive_id": archive_id, "task_id": task_id,
            "project": project, "reason": reason or None}


def move_task(task_id: str, project_from: str, project_to: str, reason: str = "",
              actor: str = "system", new_task_id: str = "",
              dependency_policy: str = "fail") -> Dict[str, Any]:
    if not has_project(project_from):
        return {"error": f"unknown source project: {project_from}", "project": project_from}
    if not has_project(project_to):
        return {"error": f"unknown destination project: {project_to}", "project": project_to}
    if project_from == project_to:
        return {"error": "source and destination projects must differ",
                "project": project_from, "task_id": task_id}
    now = time.time()
    new_task_id = (new_task_id or task_id).strip()
    dependency_policy = (dependency_policy or "fail").strip().lower()
    if dependency_policy not in {"fail", "clear"}:
        return {"error": "dependency_policy must be 'fail' or 'clear'",
                "dependency_policy": dependency_policy}

    with _conn(project_from) as source:
        snapshot = _task_snapshot_in(source, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id,
                    "project": project_from}
        active = _active_task_state_in(source, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project_from, "active": active}

    task_row = dict(snapshot["task"])
    depends_on = json.loads(task_row.get("depends_on") or "[]")
    missing_deps = _missing_dependencies(depends_on, project_to)
    cleared_deps: List[str] = []
    if missing_deps:
        if dependency_policy == "fail":
            return {"error": "destination is missing dependency id(s)",
                    "task_id": task_id, "project_from": project_from,
                    "project_to": project_to, "missing_dependencies": missing_deps,
                    "hint": "create dependencies first or pass dependency_policy='clear'"}
        cleared_deps = missing_deps
        depends_on = [dep for dep in depends_on if dep not in set(missing_deps)]

    try:
        with _conn(project_to) as dest:
            if dest.execute("SELECT 1 FROM tasks WHERE task_id=?",
                            (new_task_id,)).fetchone():
                return {"error": "destination task id already exists",
                        "task_id": new_task_id, "project_to": project_to}
            outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
            if outcome_ids:
                placeholders = ",".join("?" for _ in outcome_ids)
                conflicts = [r["id"] for r in dest.execute(
                    f"SELECT id FROM outcomes WHERE id IN ({placeholders})",
                    outcome_ids,
                ).fetchall()]
                if conflicts:
                    return {"error": "destination outcome id conflict",
                            "project_to": project_to, "outcome_ids": conflicts}
            moved_task = _apply_task_id(task_row, task_id, new_task_id)
            moved_task["depends_on"] = json.dumps(depends_on)
            moved_task["updated_at"] = now
            _insert_row(dest, "tasks", moved_task)
            for table in TASK_MOVE_TABLES:
                skip = {"id"} if table in AUTOINCREMENT_TASK_TABLES else set()
                for row in snapshot.get(table, []):
                    moved_row = _apply_task_id(row, task_id, new_task_id)
                    if table == "outcomes":
                        moved_row["project"] = project_to
                    _insert_row(dest, table, moved_row, skip_columns=skip)
            for row in snapshot.get("kpis", []):
                if dest.execute("SELECT 1 FROM kpis WHERE id=?", (row["id"],)).fetchone():
                    continue
                moved_kpi = dict(row)
                moved_kpi["project"] = project_to
                _insert_row(dest, "kpis", moved_kpi)
            for row in snapshot.get("outcome_kpi_links", []):
                moved_link = dict(row)
                moved_link["project"] = project_to
                _insert_row(dest, "outcome_kpi_links", moved_link)
            dest.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (new_task_id, actor, "task.moved_in", json.dumps({
                    "from_project": project_from,
                    "original_task_id": task_id,
                    "task_id": new_task_id,
                    "reason": reason or None,
                    "cleared_dependencies": cleared_deps,
                }, sort_keys=True), now),
            )
    except sqlite3.IntegrityError as e:
        return {"error": "destination insert failed", "detail": str(e),
                "task_id": task_id, "project_to": project_to}

    with _conn(project_from) as source:
        source_snapshot = _task_snapshot_in(source, task_id)
        if not source_snapshot:
            return {"moved": True, "warning": "source task already absent after destination copy",
                    "task_id": task_id, "new_task_id": new_task_id,
                    "project_from": project_from, "project_to": project_to}
        archive_id = _insert_archive_in(
            source, task_id, "move_out", actor, reason, project_from,
            project_to, source_snapshot, now)
        _delete_task_related_in(source, task_id, source_snapshot)

    return {"moved": True, "archive_id": archive_id, "task_id": task_id,
            "new_task_id": new_task_id, "project_from": project_from,
            "project_to": project_to, "cleared_dependencies": cleared_deps}


def get_meta(key: str, default=None, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def set_meta(key: str, value, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


def create_project(name: str, project_id: str = "", label: str = "", pretitle: str = "",
                   actor: str = "system", seed_path: str = "") -> Dict[str, Any]:
    """Create a physically isolated project board and register it for routing.

    Dynamic projects mirror the built-ins: one row in the lightweight registry, one SQLite
    file for that board's actual task/activity state. The returned id is the value callers pass
    as project="..." to all normal board tools.
    """
    clean_name = (name or "").strip()
    pid = normalize_project_id(project_id or clean_name)
    if not clean_name and not pid:
        return {"error": "project name or project_id required"}
    if not PROJECT_ID_VALID_RE.match(pid):
        return {"error": "invalid project id; use 2-63 chars: lowercase letters, digits, '-' or '_'",
                "project_id": pid}
    if pid in BUILTIN_PROJECTS:
        return {"error": f"reserved built-in project id: {pid}", "project_id": pid}

    existing = _dynamic_projects().get(pid)
    if existing:
        init_db(pid)
        seed_if_empty(pid)
        return {"created": False, "project": {"id": pid, "label": existing["label"],
                "pretitle": existing.get("pretitle", ""), "db": existing["db"],
                "seed": existing.get("seed")}}

    base_dir = os.environ.get("PM_DYNAMIC_PROJECTS_DIR") or os.path.dirname(PROJECT_REGISTRY_DB_PATH)
    os.makedirs(base_dir, exist_ok=True)
    db_path = os.path.join(base_dir, f"{pid}.db")
    project_label = (label or clean_name or pid).strip()
    project_pretitle = (pretitle or "").strip()
    seed = (seed_path or "").strip() or None
    now = time.time()

    init_project_registry()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO projects(id, label, pretitle, db_path, seed_path, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, project_label, project_pretitle, db_path, seed, now, actor),
        )
    try:
        init_db(pid)
        set_meta("project", project_label, project=pid)
        set_meta("people", DEFAULT_PEOPLE, project=pid)
        if project_pretitle:
            set_meta("pretitle", project_pretitle, project=pid)
        if seed:
            seed_if_empty(pid)
    except Exception:
        with _registry_conn() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
        raise

    return {"created": True, "project": {"id": pid, "label": project_label,
            "pretitle": project_pretitle, "db": db_path, "seed": seed}}


def get_working_agreement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Canonical connect-time rules for agents in this workspace."""
    override = get_meta("working_agreement", {}, project=project) or {}
    default = {
        "project": project,
        "protocol": protocol_envelope(),
        "canonical_main_sha": get_meta("canonical_main_sha", None, project=project),
        "branch_convention": "claude/<TASK-ID>-<slug>",
        "definition_of_done": "verified complete with recorded evidence; code tasks should include branch/head_sha/PR or merged_sha when available",
        "done_policy": {
            "mode": "agent_verified",
            "agent_may_set_done": True,
            "requires_evidence": True,
            "code_tasks_should_include_git_evidence": True,
        },
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
        "agent_completion_rule": "complete_claim(..., final_status='Done') may set Done when evidence proves completion; omit final_status to stop at In Review for review-first work",
    }
    agreement = {**default, **override, "project": project}
    if "done_policy" not in override:
        agreement["done_policy"] = default["done_policy"]
        agreement["definition_of_done"] = default["definition_of_done"]
        agreement["agent_completion_rule"] = default["agent_completion_rule"]
    return agreement


def update_canonical_main_sha(sha: str, actor: str = "github-webhook",
                              project: str = DEFAULT_PROJECT) -> None:
    if not sha:
        return
    set_meta("canonical_main_sha", sha, project=project)
    append_activity("git.main_advanced", actor, {"canonical_main_sha": sha},
                    task_id=None, project=project)


def _git_ok(args: List[str]) -> bool:
    try:
        return subprocess.run(["git", *args], cwd=os.path.dirname(__file__),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except Exception:
        return False


def _git_checks_available() -> bool:
    return _git_ok(["rev-parse", "--is-inside-work-tree"])


def _github_pr(repo: str, pr_number: int, token: str = "") -> Optional[Dict[str, Any]]:
    if not repo or not pr_number:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _external_reconcile_findings(tasks: List[Dict[str, Any]],
                                 git_states: Dict[str, Dict[str, Any]],
                                 canonical_main_sha: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    findings: List[Dict[str, Any]] = []
    checks = {"git_reachability": "not_configured", "github_prs": "not_configured"}
    if canonical_main_sha and _git_checks_available():
        checks["git_reachability"] = "checked"
        main_ref = canonical_main_sha
        for task in tasks:
            task_id = task["task_id"]
            state = git_states.get(task_id, {})
            for field, severity in (("head_sha", "medium"), ("merged_sha", "high")):
                sha = state.get(field)
                if not sha:
                    continue
                if not _git_ok(["cat-file", "-e", f"{sha}^{{commit}}"]):
                    findings.append({"severity": severity, "task_id": task_id,
                                     "code": f"{field}_not_found",
                                     "detail": f"Recorded {field} is not present in the local git object database."})
                    continue
                if field == "merged_sha" and not _git_ok(["merge-base", "--is-ancestor", sha, main_ref]):
                    findings.append({"severity": "high", "task_id": task_id,
                                     "code": "merged_sha_not_on_canonical_main",
                                     "detail": "Recorded merged_sha is not reachable from canonical main."})

    repo = os.environ.get("PM_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("PM_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    pr_tasks = [t for t in tasks if git_states.get(t["task_id"], {}).get("pr_number")]
    if repo and pr_tasks:
        checks["github_prs"] = "checked" if token else "checked_unauthenticated"
        for task in pr_tasks:
            state = git_states.get(task["task_id"], {})
            pr = _github_pr(repo, int(state.get("pr_number") or 0), token=token)
            if not pr:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "pr_state_unavailable",
                                 "detail": "Could not fetch recorded PR state from GitHub."})
                continue
            merged = bool(pr.get("merged_at"))
            if task.get("status") == "Done" and not merged:
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_pr_not_merged",
                                 "detail": "Task is Done but the recorded GitHub PR is not merged."})
            merge_sha = pr.get("merge_commit_sha")
            if merged and state.get("merged_sha") and merge_sha and state["merged_sha"] != merge_sha:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "merged_sha_mismatch",
                                 "detail": "Recorded merged_sha differs from GitHub PR merge_commit_sha."})
    return findings, checks


SEVERITY_VALUE = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _severity_value(severity: str) -> int:
    return SEVERITY_VALUE.get((severity or "").strip().lower(), 0)


def _reconcile_signature(findings: List[Dict[str, Any]]) -> str:
    material = [{
        "severity": f.get("severity") or "",
        "task_id": f.get("task_id") or "",
        "code": f.get("code") or "",
        "detail": f.get("detail") or "",
    } for f in sorted(findings, key=lambda x: (
        x.get("task_id") or "", x.get("code") or "", x.get("severity") or ""))]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _format_reconcile_alert(project: str, findings: List[Dict[str, Any]],
                            signature: str, limit: int = 12) -> str:
    lines = [
        f"Reconcile alert for project `{project}`: {len(findings)} actionable finding(s).",
        f"signature={signature}",
    ]
    for f in findings[:limit]:
        task = f.get("task_id") or "board"
        lines.append(f"- [{f.get('severity')}] {task} {f.get('code')}: {f.get('detail')}")
    if len(findings) > limit:
        lines.append(f"- ... {len(findings) - limit} more; run reconcile(project={project!r}) for full detail.")
    lines.append("Treat this as a Switchboard-owned drift interrupt: fix provenance, release stale claims, or document the exception.")
    return "\n".join(lines)


def reconcile(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Local drift report for board provenance.

    Board-internal checks always run. When a canonical main SHA and local git checkout are
    available, reconcile also verifies recorded SHAs against git reachability. If GitHub repo
    config is present, PR records are checked through the GitHub API.
    """
    now = time.time()
    agreement = get_working_agreement(project)
    done_policy = agreement.get("done_policy") or {}
    agent_done_ok = bool(done_policy.get("agent_may_set_done"))
    findings: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    git_states: Dict[str, Dict[str, Any]] = {}
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        for row in rows:
            task = _task_row(row)
            git_state = _load_git_state(c, task["task_id"])
            tasks.append(task)
            git_states[task["task_id"]] = git_state
            status = task.get("status")
            if status == "Done" and not git_state.get("merged_sha"):
                has_done_evidence = c.execute(
                    "SELECT 1 FROM activity WHERE task_id=? AND kind='task.done' LIMIT 1",
                    (task["task_id"],),
                ).fetchone()
                if not (agent_done_ok and has_done_evidence):
                    findings.append({"severity": "high", "task_id": task["task_id"],
                                     "code": "done_without_merged_sha",
                                     "detail": "Task is Done but has no recorded merge SHA or agent completion evidence."})
            if status == "In Review" and not (git_state.get("branch") or git_state.get("pr_url")):
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "review_without_provenance",
                                 "detail": "Task is In Review but lacks branch/PR evidence."})
            if status == "In Progress" and not git_state.get("head_sha"):
                findings.append({"severity": "low", "task_id": task["task_id"],
                                 "code": "progress_without_pushed_head",
                                 "detail": "Task is In Progress with no reported pushed head SHA."})
            _upsert_git_state(c, task["task_id"], {"last_reconciled_at": now})
        stale_task_claims = c.execute(
            "SELECT id, task_id, agent_id, expires_at FROM task_claims "
            "WHERE status='active' AND expires_at<=? ORDER BY expires_at",
            (now,),
        ).fetchall()
        for claim in stale_task_claims:
            findings.append({"severity": "medium", "task_id": claim["task_id"],
                             "code": "stale_task_claim",
                             "detail": f"Active task claim {claim['id']} by {claim['agent_id']} expired without completion or abandon."})
        stale_file_leases = c.execute(
            "SELECT id, task_id, agent_id, claimed_at, ttl_minutes FROM file_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at"
        ).fetchall()
        for lease in stale_file_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_minutes"] or 0) * 60
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_file_lease",
                                 "detail": f"File lease {lease['id']} by {lease['agent_id']} expired without release."})
        stale_resource_leases = c.execute(
            "SELECT id, task_id, agent_id, resource_type, claimed_at, ttl_seconds FROM resource_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at"
        ).fetchall()
        for lease in stale_resource_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_seconds"] or 0)
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_resource_lease",
                                 "detail": f"{lease['resource_type']} lease {lease['id']} by {lease['agent_id']} expired without release."})
        cursor = c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    if not agreement.get("canonical_main_sha"):
        findings.append({"severity": "medium", "task_id": None,
                         "code": "missing_canonical_main_sha",
                         "detail": "No canonical main SHA recorded yet; wait for a default-branch push webhook or set meta."})
    external_findings, external_checks = _external_reconcile_findings(
        tasks, git_states, agreement.get("canonical_main_sha") or "")
    findings.extend(external_findings)
    append_activity("reconcile.completed", "reconcile",
                    {"findings": len(findings)}, task_id=None, project=project)
    return {"project": project, "ok": not findings, "findings": findings,
            "activity_cursor": cursor, "checked_at": now,
            "external_checks": external_checks}


def run_reconcile_alerts(project: str = DEFAULT_PROJECT,
                         alert_to: str = "switchboard/operator",
                         actor: str = "switchboard/reconcile",
                         min_severity: str = "medium",
                         dedupe_window_s: int = 3600,
                         now: Optional[float] = None) -> Dict[str, Any]:
    """Run reconcile and send a deduped directed alert for actionable findings.

    The dedupe key is project + severity floor + finding signature + time bucket, so a
    persistent unresolved issue alerts at most once per bucket while a new drift shape alerts
    immediately.
    """
    now = time.time() if now is None else float(now)
    alert_to = (alert_to or "switchboard/operator").strip()
    min_severity = (min_severity or "medium").strip().lower()
    floor = _severity_value(min_severity)
    if floor <= 0:
        min_severity = "medium"
        floor = _severity_value(min_severity)
    dedupe_window_s = max(60, int(dedupe_window_s or 3600))
    report = reconcile(project=project)
    findings = [f for f in report["findings"]
                if _severity_value(str(f.get("severity") or "")) >= floor]
    if not findings:
        return {"project": project, "ok": True, "alert_sent": False, "deduped": False,
                "finding_count": 0, "min_severity": min_severity,
                "checked_at": report["checked_at"], "external_checks": report["external_checks"]}

    signature = _reconcile_signature(findings)
    window = int(now // dedupe_window_s)
    idem_key = f"reconcile-alert:{project}:{min_severity}:{alert_to}:{window}:{signature}"
    payload = {"project": project, "alert_to": alert_to, "min_severity": min_severity,
               "dedupe_window_s": dedupe_window_s, "signature": signature,
               "finding_count": len(findings)}
    with _conn(project) as c:
        hit = _idem_hit(c, "reconcile_alert", idem_key, actor, payload)
    if hit is not None:
        if "error" in hit:
            return hit
        out = dict(hit)
        out["alert_sent"] = False
        out["deduped"] = True
        return out

    message = _format_reconcile_alert(project, findings, signature)
    msg = send_agent_message(
        from_agent=actor,
        to_agent=alert_to,
        task_id=None,
        message=message,
        requires_ack=False,
        signal="reconcile_alert",
        priority=90,
        idem_key=f"{idem_key}:message",
        project=project,
    )
    response = {"project": project, "ok": False, "alert_sent": True,
                "deduped": False, "message_id": msg["id"],
                "finding_count": len(findings), "min_severity": min_severity,
                "signature": signature, "dedupe_window_s": dedupe_window_s,
                "checked_at": report["checked_at"],
                "external_checks": report["external_checks"],
                "findings": findings}
    with _conn(project) as c:
        _idem_store(c, "reconcile_alert", idem_key, actor, payload, response)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "reconcile.alert",
                   json.dumps({k: v for k, v in response.items() if k != "findings"},
                              sort_keys=True), now))
    return response


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
