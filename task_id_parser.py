"""Neutral task-id extraction from PR metadata (shared by webhooks and reconcile)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

# Task ids are <WORKSTREAM>-<number>, and a workstream may itself contain hyphens
# (QA-L-2, OFFLINE-L-1 on the Helm North-Star boards). The first segment still needs
# 2+ chars so unit tokens like S-52 / A-1 are not mistaken for task ids.
_TASKID_CORE = r"[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*-\d+"
_CLOSES_RE = re.compile(r"\b(?:closes?|fixes?|resolves?)\s+(" + _TASKID_CORE + r")\b", re.I)
_TASKID_RE = re.compile(r"\b(" + _TASKID_CORE + r")\b", re.I)


def _dedupe_upper(ids: List[str]) -> List[str]:
    return list(dict.fromkeys((i or "").upper() for i in ids if i))


def extract_task_ids(text: str) -> List[str]:
    return _dedupe_upper(_TASKID_RE.findall(text or ""))


def closing_task_ids(text: str) -> List[str]:
    return _dedupe_upper([m.group(1) for m in _CLOSES_RE.finditer(text or "")])


def task_ids_for_pr(pr: Mapping[str, Any], *, commit_messages: str = "") -> List[str]:
    """Collect task ids referenced by a PR's title, body, branch, labels, and commits."""
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    branch = str((pr.get("head") or {}).get("ref") or "")
    labels = " ".join(
        str(label.get("name") or label)
        for label in (pr.get("labels") or [])
        if label
    )
    explicit_closes = closing_task_ids(f"{title}\n{body}")
    branch_or_title = extract_task_ids(f"{title}\n{branch}\n{labels}\n{commit_messages}")
    return _dedupe_upper(explicit_closes + branch_or_title)
