#!/usr/bin/env python3
"""Scheduled jobs (Phase 4 — see docs/AGENT_ROADMAP.md). Run by systemd timers, NOT the
workflow engine. Each job is a plain function; the timer invokes this module.

  python jobs.py weekly_digest    # generate the digest + deliver via notify (Slack+Email)
  python jobs.py sweep_monitors   # evaluate Switchboard durable coordination monitors
  python jobs.py backfill_default_branch_provenance
                                   # bootstrap direct-default commit provenance

Loads /opt/projectplanner/.env via the systemd EnvironmentFile (same as the web app).
"""
import os
import re
import subprocess
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

TASK_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")


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


def sweep_monitors():
    """Evaluate durable coordination monitors for every project.

    This is deliberately a Switchboard job, not a Codex-thread reminder: monitor state lives
    in SQLite, and this job only advances that durable state.
    """
    total_checked = total_fired = total_resolved = 0
    for project_id in store.PROJECTS:
        store.init_db(project_id)
        res = store.sweep_coordination_monitors(project=project_id)
        print(f"  [{project_id}] checked={res['checked']} fired={res['fired']} "
              f"resolved={res['resolved']}")
        total_checked += res["checked"]
        total_fired += res["fired"]
        total_resolved += res["resolved"]
    print(f"sweep_monitors: checked={total_checked} fired={total_fired} "
          f"resolved={total_resolved}")
    return {"checked": total_checked, "fired": total_fired, "resolved": total_resolved}


def _default_branch_commits(ref: str, limit: int):
    cmd = ["git", "log", f"--max-count={int(limit)}", "--format=%H%x00%s", ref]
    out = subprocess.check_output(cmd, cwd=Path(__file__).parent, text=True)
    commits = []
    for line in out.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        commits.append({"sha": sha.strip(), "subject": subject.strip()})
    return commits


def backfill_default_branch_provenance(project_id: str = "", ref: str = "",
                                       limit: int = 0, dry_run: bool = False,
                                       commits=None):
    """Stamp provenance for legacy direct-to-default commits that mention task ids.

    This is a bootstrap repair path for dogfood history before PR-only flow was enforced.
    It only marks existing In Review tasks Done; all other statuses are skipped.
    """
    project_id = project_id or os.environ.get("PM_BACKFILL_PROJECT", "switchboard")
    ref = ref or os.environ.get("PM_BACKFILL_REF", "HEAD")
    limit = int(limit or os.environ.get("PM_BACKFILL_LIMIT", "200"))
    store.init_db(project_id)
    store.seed_if_empty(project_id)
    if os.environ.get("PM_BACKFILL_DRY_RUN", "").lower() in ("1", "true", "yes"):
        dry_run = True
    commits = commits if commits is not None else _default_branch_commits(ref, limit)
    seen = set()
    results = []
    for commit in commits:
        sha = commit.get("sha") or commit.get("commit_sha") or ""
        subject = commit.get("subject") or commit.get("message") or ""
        for task_id in TASK_ID_RE.findall(subject):
            key = (task_id, sha)
            if key in seen:
                continue
            seen.add(key)
            if not store.get_task(task_id, project=project_id):
                continue
            if dry_run:
                results.append({"task_id": task_id, "commit_sha": sha,
                                "subject": subject, "dry_run": True})
            else:
                results.append(store.mark_task_default_branch_commit(
                    task_id, sha, branch=ref, subject=subject,
                    actor="default-branch-backfill", project=project_id))
    applied = len([r for r in results if r.get("status") == "Done"])
    skipped = len([r for r in results if r.get("skipped")])
    print(f"backfill_default_branch_provenance[{project_id}@{ref}]: "
          f"candidates={len(results)} applied={applied} skipped={skipped} dry_run={dry_run}")
    for r in results:
        print(f"  {r}")
    return {"project": project_id, "ref": ref, "candidates": len(results),
            "applied": applied, "skipped": skipped, "dry_run": dry_run,
            "results": results}


JOBS = {"weekly_digest": weekly_digest, "poll_inbox": poll_inbox,
        "summarize_pending": summarize_pending, "sweep_monitors": sweep_monitors,
        "backfill_default_branch_provenance": backfill_default_branch_provenance}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "weekly_digest"
    JOBS.get(name, weekly_digest)()
