"""Live Inbox (Phase 5.5 / autonomous — see docs/AGENT_ROADMAP.md).

The Gmail-poll source (gmail_source.py) feeds each inbound message to `process()`, which
runs the Phase-5 ingest+triage core and — in AUTONOMOUS mode (default) — APPLIES the
implied changes immediately (audited "Maxwell (email)") and EMAILS the sender back
(answers questions, confirms what changed). The Inbox tab is then a LOG of what the agent
did, not a confirm gate. Set PM_INBOX_AUTONOMOUS=0 to fall back to a review queue.
Source-agnostic — takes a simulated email today and Slack later.
"""
import email.utils
import os

import intake
import notify
import store


def _autonomous():
    return (os.environ.get("PM_INBOX_AUTONOMOUS", "1").strip().lower() not in ("0", "false", "no"))


def _self_addrs():
    """Our own addresses — never email ourselves (and skip them in reply-all)."""
    out = set()
    for k in ("PM_IMAP_USER", "PM_SMTP_USER", "PM_SMTP_FROM"):
        v = (os.environ.get(k) or "").strip().lower()
        if v:
            out.add(v)
    return out


def _learn_contacts(*raw_header_values):
    """Record every name<->email seen on the thread so the agent can resolve names later."""
    for raw in raw_header_values:
        for name, addr in email.utils.getaddresses([raw or ""]):
            if addr and "@" in addr:
                store.upsert_contact(addr, (name or "").strip())


def _re_subject(subject):
    s = (subject or "your message").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _quoted_history(headers, text):
    """Standard quoted-original block so full message history is retained in the reply."""
    h = headers or {}
    who = h.get("from") or "the sender"
    when = h.get("date") or ""
    head = f"On {when}, {who} wrote:" if when else f"{who} wrote:"
    quoted = "\n".join("> " + ln for ln in (text or "").splitlines())
    return f"\n\n{head}\n{quoted}"


def _recipients(sender, headers, agent_recipients):
    """Decide to/cc. Agent override (set_recipients) wins; else reply-all of the thread.
    The original sender is ALWAYS kept copied — Maxwell acts on their behalf."""
    self_addrs = _self_addrs()
    sender_addr = email.utils.parseaddr(sender or "")[1].strip()

    def clean(lst):
        out, seen = [], set()
        for a in lst:
            a = (a or "").strip()
            al = a.lower()
            if a and "@" in a and al not in self_addrs and al not in seen:
                seen.add(al)
                out.append(a)
        return out

    if agent_recipients and agent_recipients.get("to"):
        to = clean(agent_recipients.get("to") or [])
        cc = clean(agent_recipients.get("cc") or [])
    else:  # reply-all: sender + original To + Cc
        pool = [sender_addr]
        for hk in ("to", "cc"):
            pool += [addr for _n, addr in email.utils.getaddresses([(headers or {}).get(hk) or ""])]
        to, cc = clean(pool), []

    low = {x.lower() for x in to} | {x.lower() for x in cc}
    if sender_addr and sender_addr.lower() not in self_addrs and sender_addr.lower() not in low:
        cc.append(sender_addr)
    cc = [a for a in cc if a.lower() not in {x.lower() for x in to}]
    return to, cc


def _compose_reply(summary, applied):
    lines = [summary or "Processed your message."]
    u, c = applied.get("updated", []), applied.get("created", [])
    if u or c:
        lines.append("")
        if u:
            lines.append("Applied — updated: " + ", ".join(u))
        if c:
            lines.append("Applied — created: " + ", ".join(c))
    lines += ["", "— Maxwell · Project Maxwell assistant (plan.taikunai.com)"]
    return "\n".join(lines)


def process(source, external_id, sender, subject, text, headers=None):
    """Dedupe -> ingest+triage -> (autonomous) apply + reply-all, else queue. Returns the item.
    headers={from,to,cc,date,message_id} drives recipient routing + threading."""
    if store.inbox_exists(source, external_id):
        return None
    headers = headers or {}
    headers.setdefault("from", sender)
    _learn_contacts(headers.get("from") or sender, headers.get("to"), headers.get("cc"))
    result = intake.ingest_and_triage("email", subject or source, text,
                                      applied_mode=_autonomous(), headers=headers)
    triage = {"proposals": result.get("proposals", []), "new_tasks": result.get("new_tasks", []),
              "sources": result.get("sources", [])}
    applied, reply_res, status, to, cc = {}, None, "pending", [], []
    if _autonomous():
        applied = apply(triage["proposals"], triage["new_tasks"])
        status = "applied"
        to, cc = _recipients(sender, headers, result.get("recipients"))
        if to:
            try:
                body = _compose_reply(result.get("summary"), applied) + _quoted_history(headers, text)
                mid = headers.get("message_id")
                reply_res = notify.reply(to, _re_subject(subject), body, cc=cc,
                                         in_reply_to=mid, references=mid)
            except Exception as e:
                reply_res = {"error": str(e)}
    triage["applied"] = applied
    triage["reply"] = reply_res
    triage["recipients"] = {"to": to, "cc": cc}
    item_id = store.add_inbox_item(source, external_id, sender, subject, result.get("summary"), triage)
    store.set_inbox_status(item_id, status)
    return store.get_inbox_item(item_id)


def apply(proposals, new_tasks):
    """Apply a set of proposals + new tasks. Audited as 'Maxwell (email)'."""
    out = {"updated": [], "created": [], "failed": []}
    for p in (proposals or []):
        tid = p.get("task_id")
        fields = {k: v for k, v in p.items() if k not in ("task_id", "rationale")}
        if tid and fields and store.update_task(tid, fields, actor="Maxwell (email)"):
            out["updated"].append(tid)
        elif tid:
            out["failed"].append(tid)
    for nt in (new_tasks or []):
        body = {k: v for k, v in nt.items() if k != "rationale"}
        t = store.create_task(body, actor="Maxwell (email)")
        (out["created"] if t else out["failed"]).append((t or nt).get("task_id") or nt.get("workstream_id"))
    return out
