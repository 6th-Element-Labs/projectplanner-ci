"""Operator recovery surface for fail-closed shared-mailbox quarantine.

Automatic intake remains project-explicit: this module only exposes message headers
until an authorized operator deliberately selects a project.  The selected message is
then passed through the existing email intake pipeline and moved to a processed mailbox
folder.  Raw message bodies never cross the listing API.
"""
from __future__ import annotations

import email
import hashlib
import html
import imaplib
import os
import re
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

import attachments
import inbox
from switchboard.domain.projects.context import ProjectContext
from switchboard.integrations import inbox_routing


class SensitiveMessageError(ValueError):
    """Raised when an operator tries to send credential-looking mail to the LLM path."""


_SENSITIVE_SUBJECT = re.compile(
    r"\b(secret|password|credential|api[ -]?key|access[ -]?token|private[ -]?key)\b",
    re.IGNORECASE,
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    return "".join(
        part.decode(encoding or "utf-8", "ignore") if isinstance(part, bytes) else part
        for part, encoding in decode_header(value)
    )


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _body(message: Message) -> str:
    if not message.is_multipart():
        return (message.get_payload(decode=True) or b"").decode(
            message.get_content_charset() or "utf-8", "ignore"
        )
    plain = ""
    html_body = ""
    for part in message.walk():
        if "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        text = (part.get_payload(decode=True) or b"").decode(
            part.get_content_charset() or "utf-8", "ignore"
        )
        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not html_body:
            html_body = text
    return plain or _html_to_text(html_body)


def _message_text(message: Message) -> str:
    parts = [_body(message)]
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        text = attachments.extract(filename, part.get_content_type(), data)
        if text and text.strip():
            parts.append(f"\n\n--- ATTACHMENT: {_decode(filename)} ---\n{text.strip()}")
        else:
            parts.append(
                f"\n\n--- ATTACHMENT: {_decode(filename)} — COULD NOT EXTRACT TEXT "
                f"({part.get_content_type()}, {len(data)} bytes); flag this to the sender ---"
            )
    return "\n".join(part for part in parts if part)


def _folder() -> str:
    return (os.environ.get("PM_INBOX_QUARANTINE_FOLDER") or "Switchboard-Quarantine").strip()


def _processed_folder() -> str:
    return (os.environ.get("PM_INBOX_PROCESSED_FOLDER") or "Switchboard-Processed").strip()


def _connect():
    user = (os.environ.get("PM_IMAP_USER") or "").strip()
    password = os.environ.get("PM_IMAP_PASSWORD")
    if not (user and password):
        return None
    host = (os.environ.get("PM_IMAP_HOST") or "imap.gmail.com").strip()
    mailbox = imaplib.IMAP4_SSL(host)
    mailbox.login(user, password)
    return mailbox


def _raw_payload(fetch_data: Any) -> bytes:
    for part in fetch_data or []:
        if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
            return part[1]
    return b""


def _recipients(message: Message) -> str:
    return ", ".join(
        value
        for value in (
            _decode(message.get("To")),
            _decode(message.get("Cc")),
            ", ".join(message.get_all("Delivered-To") or []),
            ", ".join(message.get_all("X-Original-To") or []),
        )
        if value
    )


def _token(message: Message) -> str:
    identity = _decode(message.get("Message-ID")).strip() or "\0".join(
        _decode(message.get(key)).strip()
        for key in ("From", "To", "Cc", "Subject", "Date")
    )
    return hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()[:32]


def _received_at(message: Message) -> float | None:
    try:
        value = parsedate_to_datetime(_decode(message.get("Date")))
        if isinstance(value, datetime):
            return value.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _metadata(message: Message) -> dict[str, Any]:
    sender = _decode(message.get("From"))
    recipients = _recipients(message)
    decision = inbox_routing.route_decision(sender, recipients)
    subject = _decode(message.get("Subject")) or "(no subject)"
    return {
        "token": _token(message),
        "sender": sender,
        "to": _decode(message.get("To")),
        "cc": _decode(message.get("Cc")),
        "subject": subject,
        "date": _decode(message.get("Date")),
        "received_at": _received_at(message),
        "reason": decision.reason if not decision.accepted else "route_now_available",
        "sensitive": bool(_SENSITIVE_SUBJECT.search(subject)),
    }


def list_quarantined(limit: int = 50) -> dict[str, Any]:
    """List safe message metadata from the mailbox quarantine, newest first."""
    mailbox = _connect()
    if mailbox is None:
        return {"configured": False, "folder": _folder(), "count": 0, "items": []}
    try:
        status, _ = mailbox.select(_folder(), readonly=True)
        if str(status).upper() != "OK":
            return {"configured": True, "folder": _folder(), "count": 0, "items": []}
        status, data = mailbox.search(None, "ALL")
        if str(status).upper() != "OK":
            raise RuntimeError(f"IMAP quarantine search failed: {status}")
        ids = data[0].split() if data and data[0] else []
        items = []
        for message_id in reversed(ids[-max(1, min(int(limit), 200)):]):
            status, payload = mailbox.fetch(
                message_id,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID DELIVERED-TO X-ORIGINAL-TO)])",
            )
            if str(status).upper() != "OK":
                continue
            raw = _raw_payload(payload)
            if raw:
                items.append(_metadata(email.message_from_bytes(raw)))
        return {"configured": True, "folder": _folder(), "count": len(ids), "items": items}
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def _move_processed(mailbox, message_id) -> None:
    folder = _processed_folder()
    mailbox.create(folder)
    status, _ = mailbox.copy(message_id, folder)
    if str(status).upper() != "OK":
        raise RuntimeError(f"IMAP processed copy failed: {status}")
    status, _ = mailbox.store(message_id, "+FLAGS", "(\\Deleted)")
    if str(status).upper() != "OK":
        raise RuntimeError(f"IMAP quarantine delete flag failed: {status}")
    mailbox.expunge()


def process_quarantined(token: str, project_context: ProjectContext) -> dict[str, Any]:
    """Run one selected quarantined message through the existing project pipeline."""
    mailbox = _connect()
    if mailbox is None:
        raise RuntimeError("IMAP inbox is not configured")
    try:
        status, _ = mailbox.select(_folder())
        if str(status).upper() != "OK":
            raise LookupError("mailbox quarantine is unavailable")
        status, data = mailbox.search(None, "ALL")
        if str(status).upper() != "OK":
            raise RuntimeError(f"IMAP quarantine search failed: {status}")
        ids = data[0].split() if data and data[0] else []
        for message_id in ids:
            status, payload = mailbox.fetch(message_id, "(RFC822)")
            if str(status).upper() != "OK":
                continue
            raw = _raw_payload(payload)
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            metadata = _metadata(message)
            if metadata["token"] != token:
                continue
            if metadata["sensitive"]:
                raise SensitiveMessageError(
                    "credential-looking email remains quarantined and cannot be sent to the plan agent"
                )
            sender = metadata["sender"]
            subject = metadata["subject"]
            external_id = _decode(message.get("Message-ID")) or f"{subject}:{metadata['date']}"
            headers = {
                "from": sender,
                "to": metadata["to"],
                "cc": metadata["cc"],
                "date": metadata["date"],
                "message_id": _decode(message.get("Message-ID")),
            }
            item = inbox.process(
                "email", external_id, sender, subject, _message_text(message),
                headers=headers, project_context=project_context,
            )
            _move_processed(mailbox, message_id)
            return {"processed": True, "deduped": item is None, "item": item, "message": metadata}
        raise LookupError("quarantined message not found")
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass
