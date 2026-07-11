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


def _addrs(v):
    """Normalize a str/list of addresses to a deduped list (case-insensitive)."""
    items = v if isinstance(v, (list, tuple)) else str(v or "").split(",")
    out, seen = [], set()
    for a in items:
        a = (a or "").strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _recipients(project, kind):
    """UI-14: per-project digest/notify recipients, or [] if the project set none. Isolated in a
    try/except so a comms/config read never breaks the global send path."""
    if not project:
        return []
    try:
        import comms
        return comms.recipients_for(project, kind or "notify")
    except Exception as e:
        log.warning("notify: per-project recipients unavailable for %s (%s); using global", project, e)
        return []


def _email(subject, text, to=None, cc=None, in_reply_to=None, references=None,
           project=None, kind="notify"):
    host = (os.environ.get("PM_SMTP_HOST") or "").strip()
    # Precedence: explicit `to` > this project's recipients (UI-14) > global PM_NOTIFY_EMAIL_TO.
    to_list = _addrs(to) or _recipients(project, kind) or _addrs(os.environ.get("PM_NOTIFY_EMAIL_TO"))
    cc_list = [a for a in _addrs(cc) if a.lower() not in {x.lower() for x in to_list}]
    frm = (os.environ.get("PM_SMTP_FROM") or os.environ.get("PM_SMTP_USER") or "").strip()
    if not host or not to_list or not frm:
        log.info("[dry-run] email '%s' -> to=%s cc=%s", subject, to_list or "(none)", cc_list)
        return {"channel": "email", "sent": False, "dry_run": True, "to": to_list, "cc": cc_list}
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    port = int(os.environ.get("PM_SMTP_PORT", "587"))
    user, pw = os.environ.get("PM_SMTP_USER"), os.environ.get("PM_SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        if user and pw:
            s.login(user, pw)
        s.sendmail(frm, to_list + cc_list, msg.as_string())
    return {"channel": "email", "sent": True, "to": to_list, "cc": cc_list}


def send(subject, text, channels=("slack", "email"), project=None, kind="notify"):
    """Send to Slack + Email. When `project` is given, email resolves that project's per-project
    recipients (UI-14) before falling back to the global list; `kind` is 'notify' or 'digest'."""
    out = []
    for ch in channels:
        try:
            out.append(_slack(text) if ch == "slack"
                       else _email(subject, text, project=project, kind=kind))
        except Exception as e:
            log.warning("notify %s failed: %s", ch, e)
            out.append({"channel": ch, "sent": False, "error": str(e)})
    return out


def reply(to, subject, text, cc=None, in_reply_to=None, references=None):
    """Email a reply with optional cc + RFC threading headers. Dry-run if SMTP unset."""
    return _email(subject, text, to=to, cc=cc, in_reply_to=in_reply_to, references=references)


def status():
    return {
        "slack": bool((os.environ.get("PM_SLACK_WEBHOOK_URL") or "").strip()),
        "email": bool((os.environ.get("PM_SMTP_HOST") or "").strip()
                      and (os.environ.get("PM_NOTIFY_EMAIL_TO") or "").strip()),
        "inbox_send": bool((os.environ.get("PM_SMTP_HOST") or "").strip()),
    }
