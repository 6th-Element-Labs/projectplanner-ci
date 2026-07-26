"""Outbound delivery adapter for newly-created authoritative Needs-you items."""
from __future__ import annotations

import os
import logging
from typing import Any, Mapping
from urllib.parse import quote

import notify
from switchboard.storage.repositories.activity import append_activity

log = logging.getLogger("attention_push")


def _deep_link(project: str, attention_id: str) -> str:
    base = (os.environ.get("PM_PUBLIC_BASE_URL") or "https://plan.taikunai.com").rstrip("/")
    return (
        f"{base}/?project={quote(project, safe='')}"
        f"&attention={quote(attention_id, safe='')}#tab-needs"
    )


def deliver(item: Mapping[str, Any], *, project: str,
            actor: str = "attention-push") -> dict[str, Any]:
    """Synchronously deliver one queue item and always persist an audit outcome."""
    attention_id = str(item.get("attention_id") or item.get("source_id") or "").strip()
    if not attention_id:
        raise ValueError("attention_id is required")
    title = str(item.get("title") or item.get("summary") or "Needs you").strip()
    summary = str(item.get("summary") or title).strip()
    task_id = str(item.get("task_id") or "").strip()
    session_id = str(
        item.get("runner_session_id") or item.get("work_session_id") or ""
    ).strip()
    deep_link = _deep_link(project, attention_id)
    subject = f"[{project}] Needs you: {title}"[:240]
    text = "\n".join(filter(None, (
        title,
        summary if summary != title else "",
        f"Task: {task_id}" if task_id else "",
        f"Session: {session_id}" if session_id else "",
        f"Open exact item: {deep_link}",
    )))
    results = notify.send(subject, text, ("slack", "email"), project, "notify")
    delivered = any(bool(result.get("sent")) for result in results)
    event = "attention.push_delivered" if delivered else "attention.push_missed"
    receipt = {
        "schema": "switchboard.attention_push_receipt.v1",
        "project": project,
        "attention_id": attention_id,
        "source": str(item.get("source") or ""),
        "task_id": task_id or None,
        "runner_session_id": session_id or None,
        "deep_link": deep_link,
        "delivered": delivered,
        "results": results,
    }
    try:
        append_activity(event, actor, receipt, task_id=task_id or None, project=project)
    except Exception as exc:
        # The authoritative queue item has already committed. Preserve it and
        # expose the audit failure on the producer response instead of turning
        # a successful provider write into an ambiguous retry.
        receipt["audit_error"] = f"{type(exc).__name__}: {exc}"
        log.error("attention push audit failed for %s: %s", attention_id, exc)
    return receipt


def deliver_attention_request(request: Mapping[str, Any], *, project: str,
                              actor: str) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "")
    return deliver({
        "attention_id": f"provider:{request_id}",
        "source_id": f"provider:{request_id}",
        "source": "provider",
        "task_id": request.get("task_id"),
        "runner_session_id": request.get("runner_session_id"),
        "work_session_id": request.get("work_session_id"),
        "title": request.get("prompt") or "Provider question",
        "summary": request.get("prompt") or "Provider question",
    }, project=project, actor=actor)


def deliver_runner_fault(session: Mapping[str, Any], *, project: str,
                         actor: str) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    fault = metadata.get("progress_fault") if isinstance(
        metadata.get("progress_fault"), dict
    ) else {}
    runner_session_id = str(session.get("runner_session_id") or "")
    summary = str(fault.get("message") or f"Runner {runner_session_id} needs attention")
    return deliver({
        "attention_id": f"runner-progress:{runner_session_id}",
        "source_id": f"runner-progress:{runner_session_id}",
        "source": "runner",
        "task_id": session.get("task_id") or fault.get("task_id"),
        "runner_session_id": runner_session_id,
        "title": summary,
        "summary": summary,
    }, project=project, actor=actor)


__all__ = ["deliver", "deliver_attention_request", "deliver_runner_fault"]
