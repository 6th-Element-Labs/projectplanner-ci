"""IMAP source adapter for the Live Inbox (Phase 5.5 — see docs/AGENT_ROADMAP.md).

Reads plan@taikunai.com via IMAP (app password — the same simple model as the Phase-4
SMTP sender, no OAuth), routes each message to a PROJECT, and feeds it to inbox.process.
DISABLED (no-op) until PM_IMAP_* is set, so it ships safe before the mailbox exists. Run by
the projectplanner-inbox timer (jobs.py poll_inbox).

One mailbox, many boards (UI-13): a message is routed to a project by, in order,
  1. plus-addressing — plan+<project>@taikunai.com on any recipient (zero-config, same mailbox);
  2. the sender-domain map — per-project associations edited from Settings → Communications
     (UI-14) merged over the PM_INBOX_ROUTES env bootstrap (e.g. 'totalenergy.com=maxwell');
Messages without exactly one explicit route are moved to the mailbox quarantine folder. They do
not enter a project database or invoke ingestion, triage, replies, dispatch, embeddings, or LLMs.

Config (.env, all optional):
  PM_IMAP_HOST(=imap.gmail.com)  PM_IMAP_USER  PM_IMAP_PASSWORD(app password)
  PM_INBOX_ROUTES      comma list of 'domain=project' (e.g. 'totalenergy.com=maxwell'); a sender
                       whose domain matches (or is a subdomain of) an entry routes to that project.
"""
import email
import html
import imaplib
import logging
import os
import re
from email.header import decode_header

import attachments
import inbox
import scripts.switchboard_path  # noqa: F401
from switchboard.integrations import inbox_routing
from switchboard.domain.projects.context import ProjectContext

log = logging.getLogger("inbox_source")


def _quarantine(m, message_id, reason):
    """Move an unroutable message outside INBOX without touching any project state."""
    folder = (os.environ.get("PM_INBOX_QUARANTINE_FOLDER") or "Switchboard-Quarantine").strip()
    try:
        m.create(folder)  # idempotent on common IMAP servers; ALREADYEXISTS is harmless
        typ, _ = m.copy(message_id, folder)
        if str(typ).upper() != "OK":
            raise RuntimeError(f"IMAP quarantine copy failed: {typ}")
        typ, _ = m.store(message_id, "+FLAGS", "(\\Deleted)")
        if str(typ).upper() != "OK":
            raise RuntimeError(f"IMAP source-delete flag failed: {typ}")
    except Exception:
        # fetch() marks mail seen. Restore UNSEEN on any quarantine failure so a
        # transient mailbox error cannot silently strand the message.
        try:
            m.store(message_id, "-FLAGS", "(\\Seen)")
        except Exception:
            pass
        raise
    log.warning("Live Inbox: quarantined message %s (%s)", message_id, reason)


def _decode(s):
    if not s:
        return ""
    return "".join((b.decode(enc or "utf-8", "ignore") if isinstance(b, bytes) else b)
                   for b, enc in decode_header(s))


def _html2text(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return html.unescape(h).strip()


def _body(msg):
    """Prefer text/plain; fall back to HTML-stripped text if that's all there is."""
    if not msg.is_multipart():
        return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "ignore")
    plain, html_body = "", ""
    for part in msg.walk():
        if "attachment" in str(part.get("Content-Disposition")).lower():
            continue
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html"):
            text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "ignore")
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html_body:
                html_body = text
    return plain or _html2text(html_body)


def _message_text(msg):
    """Body + every attachment's extracted text — and EXPLICITLY note any we can't read
    (fail-and-fix-early: surface the gap in-line so the agent + reply flag it)."""
    parts = [_body(msg)]
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        text = attachments.extract(fn, part.get_content_type(), data)
        if text and text.strip():
            parts.append(f"\n\n--- ATTACHMENT: {_decode(fn)} ---\n{text.strip()}")
        else:
            parts.append(f"\n\n--- ATTACHMENT: {_decode(fn)} — COULD NOT EXTRACT TEXT "
                         f"({part.get_content_type()}, {len(data)} bytes); flag this to the sender ---")
    return "\n".join(p for p in parts if p)


def poll(max_msgs=20):
    host = (os.environ.get("PM_IMAP_HOST") or "imap.gmail.com").strip()
    user = (os.environ.get("PM_IMAP_USER") or "").strip()
    pw = os.environ.get("PM_IMAP_PASSWORD")
    if not (user and pw):
        log.info("Live Inbox: IMAP not configured (PM_IMAP_USER/PASSWORD) — skipping")
        return {"polled": 0, "queued": 0, "disabled": True}
    m = imaplib.IMAP4_SSL(host)
    try:
        m.login(user, pw)
        m.select("INBOX")
        _typ, data = m.search(None, "UNSEEN")
        ids = (data[0].split() if data and data[0] else [])[:max_msgs]
        self_addr = (os.environ.get("PM_IMAP_USER") or "").lower()
        queued = quarantined = 0
        quarantine_reasons = {}
        for i in ids:
            _typ, msg_data = m.fetch(i, "(RFC822)")   # fetching marks \Seen -> not re-polled
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode(msg.get("From"))
            if self_addr and self_addr in (sender or "").lower():
                continue   # never process our own outbound — no self-reply loops
            to, cc = _decode(msg.get("To")), _decode(msg.get("Cc"))
            # Route by recipient plus-address / sender-domain map; Delivered-To / X-Original-To
            # carry the +tag that Gmail keeps when plan+<project>@ is delivered to plan@. Join the
            # header groups with commas so email.utils.getaddresses parses them as one address list.
            recipients = ", ".join(p for p in [
                to, cc,
                ", ".join(msg.get_all("Delivered-To") or []),
                ", ".join(msg.get_all("X-Original-To") or []),
            ] if p)
            decision = inbox_routing.route_decision(sender, recipients)
            if not decision.accepted:
                _quarantine(m, i, decision.reason)
                quarantined += 1
                quarantine_reasons[decision.reason] = quarantine_reasons.get(decision.reason, 0) + 1
                continue
            project = decision.project
            subject = _decode(msg.get("Subject"))
            mid = msg.get("Message-ID") or f"{subject}:{msg.get('Date')}"
            headers = {
                "from": sender,
                "to": to,
                "cc": cc,
                "date": _decode(msg.get("Date")),
                "message_id": msg.get("Message-ID"),
            }
            # route() already resolved and validated the project against its routing
            # index; preserve that one decision instead of consulting the registry again.
            project_context = ProjectContext(project_id=project, source="inbox-routing")
            if inbox.process("email", mid, sender, subject, _message_text(msg),
                             headers=headers, project_context=project_context):
                queued += 1
        return {"polled": len(ids), "queued": queued, "quarantined": quarantined,
                "quarantine_reasons": quarantine_reasons, "disabled": False}
    finally:
        try:
            m.logout()
        except Exception:
            pass
