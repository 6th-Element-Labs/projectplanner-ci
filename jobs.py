#!/usr/bin/env python3
"""Scheduled jobs (Phase 4 — see docs/AGENT_ROADMAP.md). Run by systemd timers, NOT the
workflow engine. Each job is a plain function; the timer invokes this module.

  python jobs.py weekly_digest    # generate the digest + deliver via notify (Slack+Email)

Loads /opt/projectplanner/.env via the systemd EnvironmentFile (same as the web app).
"""
import os
import sys
from pathlib import Path

# Load .env so a manual run works like the systemd EnvironmentFile path does.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import digest  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402


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


def summarize_pending():
    """Summarize task activity trails for all projects (runs every 15 min via systemd timer).
    PM_SUMMARIZE_MODEL controls the model (default: taikun-chat; set taikun-haiku for cheapest)."""
    import summarize as summarize_mod
    total = 0
    for project_id in store.PROJECTS:
        results = summarize_mod.run_pending(project=project_id)
        print(f"  [{project_id}] summarized {len(results)} task(s)")
        total += len(results)
    print(f"summarize_pending: {total} total")
    return total


JOBS = {"weekly_digest": weekly_digest, "poll_inbox": poll_inbox,
        "summarize_pending": summarize_pending}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "weekly_digest"
    JOBS.get(name, weekly_digest)()
