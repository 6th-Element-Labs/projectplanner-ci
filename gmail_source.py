"""Gmail-poll source for the Live Inbox (Phase 5.5 — see docs/AGENT_ROADMAP.md).

Reads plan@taikunai.com via IMAP (app password — the same simple model as the Phase-4
SMTP sender, no OAuth), allow-lists senders, and feeds each new message to inbox.process.
DISABLED (no-op) until PM_IMAP_* is set, so it ships safe before the mailbox exists. Run by
the projectplanner-inbox timer (jobs.py poll_inbox).

Config (.env, all optional):
  PM_IMAP_HOST(=imap.gmail.com)  PM_IMAP_USER  PM_IMAP_PASSWORD(app password)
  PM_INBOX_ALLOWLIST   comma list; a message is accepted if its From contains any entry
                       (empty = accept all — tighten in prod).
"""
import email
import imaplib
import logging
import os
from email.header import decode_header

import inbox

log = logging.getLogger("gmail_source")


def _allow(sender):
    al = (os.environ.get("PM_INBOX_ALLOWLIST") or "").strip()
    if not al:
        return True
    s = (sender or "").lower()
    return any(a.strip().lower() in s for a in al.split(",") if a.strip())


def _decode(s):
    if not s:
        return ""
    return "".join((b.decode(enc or "utf-8", "ignore") if isinstance(b, bytes) else b)
                   for b, enc in decode_header(s))


def _body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "ignore")
        return ""
    return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "ignore")


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
        queued = 0
        for i in ids:
            _typ, msg_data = m.fetch(i, "(RFC822)")   # fetching marks \Seen -> not re-polled
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode(msg.get("From"))
            if not _allow(sender):
                continue
            subject = _decode(msg.get("Subject"))
            mid = msg.get("Message-ID") or f"{subject}:{msg.get('Date')}"
            if inbox.process("email", mid, sender, subject, _body(msg)):
                queued += 1
        return {"polled": len(ids), "queued": queued, "disabled": False}
    finally:
        try:
            m.logout()
        except Exception:
            pass
