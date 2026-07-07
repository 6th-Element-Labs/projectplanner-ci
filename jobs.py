#!/usr/bin/env python3
"""Scheduled jobs (Phase 4 — see docs/AGENT_ROADMAP.md). Run by systemd timers, NOT the
workflow engine. Each job is a plain function; the timer invokes this module.

  python jobs.py weekly_digest    # generate the digest + deliver via notify (Slack+Email)
  python jobs.py sweep_monitors   # evaluate Switchboard durable coordination monitors
  python jobs.py reconcile_alerts # run reconcile and send deduped drift alerts
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
    for project_id in store.project_ids():
        results = summarize_mod.run_pending(project=project_id)
        print(f"  [{project_id}] summarized {len(results)} task(s)")
        total += len(results)
    print(f"summarize_pending: {total} total")
    return total


def narrate_pending():
    """Drain the CEO-voice narration queue for all projects (NARRATE-2; short systemd timer,
    ~PM_NARRATE_INTERVAL). Separate from summarize_pending: different store, audience, and
    trigger (status transitions via pending_narrations, not any-activity). The fingerprint
    guard means idle cycles make zero LLM calls. See docs/CEO-NARRATOR-CONTRACT.md."""
    import narrate as narrate_mod
    total = deliverables = 0
    for project_id in store.project_ids():
        store.init_db(project_id)
        results = narrate_mod.run_pending(project=project_id)
        # NARRATE-3: re-narrate deliverable headers whose brief fingerprint moved this cycle.
        deliv = narrate_mod.run_deliverables(project=project_id)
        print(f"  [{project_id}] narrated {len(results)} task(s), {len(deliv)} deliverable(s)")
        total += len(results)
        deliverables += len(deliv)
    print(f"narrate_pending: {total} task(s), {deliverables} deliverable(s)")
    return total


def sweep_monitors():
    """Evaluate durable coordination monitors for every project.

    This is deliberately a Switchboard job, not a Codex-thread reminder: monitor state lives
    in SQLite, and this job only advances that durable state.
    """
    total_checked = total_fired = total_resolved = 0
    for project_id in store.project_ids():
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


def _configured_projects(env_name: str, default: str):
    raw = os.environ.get(env_name, default).strip() or default
    if raw.lower() in ("all", "*"):
        return store.project_ids()
    projects = store.coerce_csv_list(raw)
    unknown = [p for p in projects if not store.has_project(p)]
    if unknown:
        raise ValueError(f"unknown project(s) for {env_name}: {', '.join(unknown)}")
    return projects


def reconcile_alerts():
    """Run reconcile and send deduped actionable drift alerts.

    Defaults to every registered project so merge-provenance backfill keeps dynamic boards
    (Helm, Vulkan, etc.) unblocked even when a repo webhook is missing or delayed.
    Set PM_RECON_ALERT_PROJECTS=switchboard (or a comma list) only when deliberately
    narrowing the scheduled surface.
    """
    projects = _configured_projects("PM_RECON_ALERT_PROJECTS", "all")
    alert_to = os.environ.get("PM_RECON_ALERT_TO", "switchboard/operator")
    min_severity = os.environ.get("PM_RECON_ALERT_MIN_SEVERITY", "medium")
    dedupe_s = int(os.environ.get("PM_RECON_ALERT_DEDUPE_SECONDS", "3600"))
    sent = deduped = findings = 0
    results = []
    for project_id in projects:
        store.init_db(project_id)
        store.seed_if_empty(project_id)
        res = store.run_reconcile_alerts(
            project=project_id, alert_to=alert_to,
            min_severity=min_severity, dedupe_window_s=dedupe_s)
        results.append(res)
        sent += 1 if res.get("alert_sent") else 0
        deduped += 1 if res.get("deduped") else 0
        findings += int(res.get("finding_count") or 0)
        print(f"  [{project_id}] findings={res.get('finding_count', 0)} "
              f"alert_sent={res.get('alert_sent')} deduped={res.get('deduped')} "
              f"message_id={res.get('message_id')}")
    print(f"reconcile_alerts: projects={len(projects)} findings={findings} "
          f"sent={sent} deduped={deduped} alert_to={alert_to}")
    return {"projects": projects, "findings": findings, "sent": sent,
            "deduped": deduped, "results": results}


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


def ci_gate_prs():
    """Run the VM-backed Switchboard PR CI gate once.

    This is the Actions-equivalent fallback for cases where GitHub records
    `startup_failure` before any workflow job exists. It posts a commit status
    to each open PR head SHA, so the PR still carries a visible pass/fail gate.
    """
    cmd = [sys.executable, str(Path(__file__).parent / "scripts" / "switchboard_pr_gate.py"),
           "--once-open-prs"]
    subprocess.run(cmd, check=True, cwd=Path(__file__).parent)


JOBS = {"weekly_digest": weekly_digest, "poll_inbox": poll_inbox,
        "summarize_pending": summarize_pending, "narrate_pending": narrate_pending,
        "sweep_monitors": sweep_monitors,
        "reconcile_alerts": reconcile_alerts,
        "backfill_default_branch_provenance": backfill_default_branch_provenance,
        "ci_gate_prs": ci_gate_prs}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "weekly_digest"
    JOBS.get(name, weekly_digest)()
