#!/usr/bin/env python3
"""Scheduled jobs (Phase 4 — see docs/AGENT_ROADMAP.md). Run by systemd timers, NOT the
workflow engine. Each job is a plain function; the timer invokes this module.

  python jobs.py weekly_digest    # generate the digest + deliver via notify (Slack+Email)

Loads /opt/projectplanner/.env via the systemd EnvironmentFile (same as the web app).
"""
import sys

import digest
import notify
import store


def weekly_digest():
    """Generate the chief-of-staff brief and deliver it. Dry-run channels just log."""
    d = digest.generate_digest()
    proj = store.get_meta("project") or "the plan"
    subject = f"{proj} — weekly digest"
    results = notify.send(subject, d["content"])
    print(f"digest #{d['id']} generated; notify: {results}")
    return results


def poll_inbox():
    """Poll the Live Inbox mailbox (IMAP) and queue triaged messages. No-op until configured."""
    import gmail_source
    res = gmail_source.poll()
    print(f"inbox poll: {res}")
    return res


JOBS = {"weekly_digest": weekly_digest, "poll_inbox": poll_inbox}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "weekly_digest"
    JOBS.get(name, weekly_digest)()
