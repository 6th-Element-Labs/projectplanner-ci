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


def _dispatch_enabled():
    return (os.environ.get("PM_INBOX_DISPATCH", "1").strip().lower() not in ("0", "false", "no"))


def _dispatch_dev(targets, applied):
    """Fire the dev-agent dispatches the email asked for. 'NEW' -> the task(s) just created.
    Each runs the Claude Code runner (code change -> branch -> PR; never main)."""
    if not (_dispatch_enabled() and targets):
        return []
    created = list((applied or {}).get("created") or [])
    resolved = []
    for tg in targets:
        resolved.extend([tg] if (tg and tg != "NEW") else created)
    import dispatch as dispatch_mod
    out = []
    for tid in dict.fromkeys(resolved):  # dedupe, keep order
        if not store.get_task(tid):
            continue
        dr = dispatch_mod.dispatch(tid, actor="Maxwell (email)")
        out.append({"task_id": tid, "dispatched": dr.get("dispatched"),
                    "job_id": dr.get("job_id"), "error": dr.get("error") or dr.get("reason")})
    return out


def _dispatch_note(dispatched):
    ok = [d for d in (dispatched or []) if d.get("dispatched")]
    fail = [d for d in (dispatched or []) if not d.get("dispatched")]
    parts = []
    if ok:
        parts.append("Dispatched to Claude Code: " + ", ".join(
            f"{d['task_id']} (job {d.get('job_id')})" for d in ok)
            + " — a PR link will post to each task when it's done.")
    parts += [f"Could not dispatch {d['task_id']}: {d.get('error') or 'unknown'}." for d in fail]
    return ("\n\n" + "\n".join(parts)) if parts else ""


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


def _compose_review_reply(summary, proposals, new_tasks):
    """Reply for REVIEW mode (PM_INBOX_AUTONOMOUS=0): nothing applied — tell the forwarder
    what was QUEUED for their confirmation, with a link to the Action Queue."""
    lines = [summary or "I reviewed your message."]
    parts = []
    for p in (proposals or []):
        chg = p.get("status") or ("reschedule" if (p.get("start_date") or p.get("finish_date")) else "update")
        parts.append("  • %s → %s%s" % (p.get("task_id"), chg,
                                        (" — " + p["rationale"]) if p.get("rationale") else ""))
    for nt in (new_tasks or []):
        parts.append("  • NEW (%s): %s" % (nt.get("workstream_id"), nt.get("title")))
    if parts:
        lines += ["", "Queued %d change(s) + %d new task(s) for your confirmation — NOTHING applied yet:"
                  % (len(proposals or []), len(new_tasks or []))]
        lines += parts
    else:
        lines += ["", "No plan changes proposed — filed for reference."]
    lines += ["", "Review, edit & confirm in the Action Queue: https://plan.taikunai.com",
              "", "— Maxwell · Project Maxwell assistant"]
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
    applied, reply_res, status, to, cc, dispatched = {}, None, "pending", [], [], []
    if _autonomous():
        applied = apply(triage["proposals"], triage["new_tasks"])
        status = "applied"
        dispatched = _dispatch_dev(result.get("dispatch_targets"), applied)
        to, cc = _recipients(sender, headers, result.get("recipients"))
        if to:
            try:
                body = (_compose_reply(result.get("summary"), applied)
                        + _dispatch_note(dispatched) + _quoted_history(headers, text))
                mid = headers.get("message_id")
                reply_res = notify.reply(to, _re_subject(subject), body, cc=cc,
                                         in_reply_to=mid, references=mid)
            except Exception as e:
                reply_res = {"error": str(e)}
    else:
        # Review mode: nothing applied. Send a private heads-up to the FORWARDER only
        # (not reply-all — we don't email the original thread about un-confirmed proposals).
        sender_addr = email.utils.parseaddr(sender or "")[1].strip()
        if sender_addr and sender_addr.lower() not in _self_addrs():
            to = [sender_addr]
            try:
                body = (_compose_review_reply(result.get("summary"), triage["proposals"],
                                              triage["new_tasks"]) + _quoted_history(headers, text))
                mid = headers.get("message_id")
                reply_res = notify.reply(to, _re_subject(subject), body, cc=cc,
                                         in_reply_to=mid, references=mid)
            except Exception as e:
                reply_res = {"error": str(e)}
    triage["applied"] = applied
    triage["reply"] = reply_res
    triage["recipients"] = {"to": to, "cc": cc}
    triage["dispatched_dev"] = dispatched
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
