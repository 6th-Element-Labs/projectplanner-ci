"""rag_store.py — incremental RAG corpus (leaf store). Extracted verbatim from store.py (ARCH-5)."""
import json
import time
import os
import sqlite3
import hashlib
import uuid
import copy
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.core import *     # noqa: F401,F403
from db.schema import *   # noqa: F401,F403
from db.connection import *  # noqa: F401,F403

__all__ = [
    "add_rag_chunk",
    "all_rag_chunks",
    "rag_docs_max_id",
    "all_rag_rows",
    "update_rag_embedding",
]


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
