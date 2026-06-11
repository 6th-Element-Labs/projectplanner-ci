"""Notify primitive (Phase 4 — see docs/AGENT_ROADMAP.md).

Two channels: Slack (incoming webhook) + Email (SMTP — works with a Gmail app password
or any relay). An UNCONFIGURED channel is DRY-RUN: it logs what it *would* send and
returns sent=false, so the whole digest/scheduler path ships and is testable BEFORE any
creds land. Flipping it live is a one-line .env change. (Gmail-API send/read via OAuth is
a later upgrade for the Live Inbox #9; for sending, SMTP is enough.)

Config (.env, all optional):
  PM_SLACK_WEBHOOK_URL
  PM_SMTP_HOST  PM_SMTP_PORT(=587)  PM_SMTP_USER  PM_SMTP_PASSWORD
  PM_SMTP_FROM (defaults to PM_SMTP_USER)  PM_NOTIFY_EMAIL_TO
"""
import logging
import os
import smtplib
import ssl
from email.mime.text import MIMEText

import httpx

log = logging.getLogger("notify")


def _slack(text):
    url = (os.environ.get("PM_SLACK_WEBHOOK_URL") or "").strip()
    if not url:
        log.info("[dry-run] slack: %s", text[:120])
        return {"channel": "slack", "sent": False, "dry_run": True}
    r = httpx.post(url, json={"text": text}, timeout=20)
    return {"channel": "slack", "sent": r.status_code < 300, "status": r.status_code}


def _email(subject, text, to=None):
    host = (os.environ.get("PM_SMTP_HOST") or "").strip()
    to = (to or os.environ.get("PM_NOTIFY_EMAIL_TO") or "").strip()
    frm = (os.environ.get("PM_SMTP_FROM") or os.environ.get("PM_SMTP_USER") or "").strip()
    if not host or not to or not frm:
        log.info("[dry-run] email '%s' -> %s", subject, to or "(no recipient)")
        return {"channel": "email", "sent": False, "dry_run": True}
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    port = int(os.environ.get("PM_SMTP_PORT", "587"))
    user, pw = os.environ.get("PM_SMTP_USER"), os.environ.get("PM_SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        if user and pw:
            s.login(user, pw)
        s.sendmail(frm, [a.strip() for a in to.split(",") if a.strip()], msg.as_string())
    return {"channel": "email", "sent": True, "to": to}


def send(subject, text, channels=("slack", "email")):
    out = []
    for ch in channels:
        try:
            out.append(_slack(text) if ch == "slack" else _email(subject, text))
        except Exception as e:
            log.warning("notify %s failed: %s", ch, e)
            out.append({"channel": ch, "sent": False, "error": str(e)})
    return out


def status():
    return {
        "slack": bool((os.environ.get("PM_SLACK_WEBHOOK_URL") or "").strip()),
        "email": bool((os.environ.get("PM_SMTP_HOST") or "").strip()
                      and (os.environ.get("PM_NOTIFY_EMAIL_TO") or "").strip()),
    }
