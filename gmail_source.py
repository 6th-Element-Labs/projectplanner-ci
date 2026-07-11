"""Gmail-poll source for the Live Inbox (Phase 5.5 — see docs/AGENT_ROADMAP.md).

Reads plan@taikunai.com via IMAP (app password — the same simple model as the Phase-4
SMTP sender, no OAuth), routes each message to a PROJECT, and feeds it to inbox.process.
DISABLED (no-op) until PM_IMAP_* is set, so it ships safe before the mailbox exists. Run by
the projectplanner-inbox timer (jobs.py poll_inbox).

One mailbox, many boards (UI-13): a message is routed to a project by, in order,
  1. plus-addressing — plan+<project>@taikunai.com on any recipient (zero-config, same mailbox);
  2. the sender-domain map — per-project associations edited from Settings → Communications
     (UI-14) merged over the PM_INBOX_ROUTES env bootstrap (e.g. 'totalenergy.com=maxwell');
  3. otherwise the global PM_INBOX_ALLOWLIST gate -> the default project (today's behavior, unchanged).
A routing match (1 or 2) also ACCEPTS the message; unmatched senders still pass through the
allowlist so nothing that arrives today stops arriving. Each project's inbox/corpus is isolated
in its own DB file, so a routed message lands only on that board.

Config (.env, all optional):
  PM_IMAP_HOST(=imap.gmail.com)  PM_IMAP_USER  PM_IMAP_PASSWORD(app password)
  PM_INBOX_ALLOWLIST   comma list; an UNMAPPED message is accepted if its From contains any
                       entry (empty = accept all — tighten in prod). Routing (below) bypasses it.
  PM_INBOX_ROUTES      comma list of 'domain=project' (e.g. 'totalenergy.com=maxwell'); a sender
                       whose domain matches (or is a subdomain of) an entry routes to that project.
"""
import email
import email.utils
import html
import imaplib
import logging
import os
import re
from email.header import decode_header

import attachments
import inbox
import store

log = logging.getLogger("gmail_source")


def _allow(sender):
    al = (os.environ.get("PM_INBOX_ALLOWLIST") or "").strip()
    if not al:
        return True
    s = (sender or "").lower()
    return any(a.strip().lower() in s for a in al.split(",") if a.strip())


def _routes_map():
    """Domain→project routing map, merged from two sources:
      1. PM_INBOX_ROUTES='domain=project, ...' — deploy-level bootstrap (domains lowercased,
         leading @ stripped; malformed entries skipped);
      2. the web-managed per-project associations (UI-14, comms.persisted_routes()).
    The web map is authoritative on conflict — it's the surface an operator edits live, so an
    association made from Settings → Communications takes effect with no .env edit."""
    out = {}
    for part in (os.environ.get("PM_INBOX_ROUTES") or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        dom, proj = part.split("=", 1)
        dom, proj = dom.strip().lstrip("@").lower(), proj.strip()
        if dom and proj:
            out[dom] = proj
    try:
        import comms
        for dom, proj in comms.persisted_routes().items():
            if out.get(dom) not in (None, proj):
                log.info("inbox routing: web association %s -> %s overrides PM_INBOX_ROUTES -> %s",
                         dom, proj, out[dom])
            out[dom] = proj
    except Exception as e:  # never let a config read stop the poller from routing on the env map
        log.warning("inbox routing: could not load web-managed routes (%s); using env map only", e)
    return out


def _plus_project(recipients, valid):
    """A recipient of the form plan+<project>@taikunai.com routes to <project> (if it's a real
    project). recipients is a raw header string of To/Cc/Delivered-To addresses."""
    for _n, addr in email.utils.getaddresses([recipients or ""]):
        local = (addr or "").split("@", 1)[0]
        if "+" in local:
            tag = local.split("+", 1)[1].strip().lower()
            if tag in valid:
                return tag
    return None


def _domain_project(sender, valid):
    """Map the sender's From-domain to a project via PM_INBOX_ROUTES. Matches an exact domain
    or any subdomain of a listed domain. Returns None (no route) if unmapped or the mapped
    project doesn't exist — a bad map entry is a loud warning, never a silent misroute."""
    addr = email.utils.parseaddr(sender or "")[1].lower()
    dom = addr.split("@", 1)[1] if "@" in addr else ""
    if not dom:
        return None
    routes = _routes_map()
    proj = routes.get(dom)
    if not proj:
        for rdom, rproj in routes.items():
            if dom == rdom or dom.endswith("." + rdom):
                proj = rproj
                break
    if not proj:
        return None
    if proj not in valid:
        log.warning("inbox routing: PM_INBOX_ROUTES maps %s -> unknown project %r; ignoring", dom, proj)
        return None
    return proj


def _route(sender, recipients):
    """Resolve (accept, project) for one inbound message. A plus-address or sender-domain route
    wins and accepts the message; otherwise fall back to the global allowlist -> default project."""
    valid = set(store.project_ids())
    proj = _plus_project(recipients, valid)
    if proj:
        return True, proj
    proj = _domain_project(sender, valid)
    if proj:
        return True, proj
    return _allow(sender), store.DEFAULT_PROJECT


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
        queued = 0
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
            accept, project = _route(sender, recipients)
            if not accept:
                continue
            subject = _decode(msg.get("Subject"))
            mid = msg.get("Message-ID") or f"{subject}:{msg.get('Date')}"
            headers = {
                "from": sender,
                "to": to,
                "cc": cc,
                "date": _decode(msg.get("Date")),
                "message_id": msg.get("Message-ID"),
            }
            if inbox.process("email", mid, sender, subject, _message_text(msg),
                             headers=headers, project=project):
                queued += 1
        return {"polled": len(ids), "queued": queued, "disabled": False}
    finally:
        try:
            m.logout()
        except Exception:
            pass
