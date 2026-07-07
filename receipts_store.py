"""receipts_store.py — coordination receipts (leaf store). Extracted verbatim from store.py (ARCH-5)."""
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
    "get_coordination_receipt",
    "list_coordination_receipts",
]


def get_coordination_receipt(project: str = DEFAULT_PROJECT,
                             receipt_id: str = "") -> Dict[str, Any]:
    """Fetch one projected coordination receipt by stable receipt id."""
    import coordination_receipts
    return coordination_receipts.get_coordination_receipt(project, receipt_id)


def list_coordination_receipts(project: str = DEFAULT_PROJECT, *,
                               task_id: str = "",
                               agent_id: str = "",
                               limit: int = 50) -> Dict[str, Any]:
    """List projected coordination receipts for a project."""
    import coordination_receipts
    return coordination_receipts.list_coordination_receipts(
        project, task_id=task_id, agent_id=agent_id, limit=limit)
