#!/usr/bin/env python3
"""Scheduled jobs (Phase 4 — see docs/AGENT_ROADMAP.md). Run by systemd timers, NOT the
workflow engine. Each job is a plain function; the timer invokes this module.

  python jobs.py weekly_digest    # generate the digest + deliver via notify (Slack+Email)
  python jobs.py sweep_monitors   # evaluate Switchboard durable coordination monitors
  python jobs.py reconcile_alerts # run reconcile and send deduped drift alerts
  python jobs.py coordinator_audit
                                  # emit T0 read-only ranked coordinator plans
  python jobs.py background_job <job_name>
                                   # run a checkpointed background job (RECON-10)

Loads /opt/projectplanner/.env via the systemd EnvironmentFile (same as the web app).
"""
import json
import os
import subprocess
import sys
import fcntl
from contextlib import contextmanager
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


@contextmanager
def _single_flight_lock(path: str):
    """Nonblocking process lock for manual runs that bypass systemd's flock."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def weekly_digest():
    """Generate the chief-of-staff brief and deliver it. Dry-run channels just log."""
    d = digest.generate_digest()
    proj = store.get_meta("project") or "the plan"
    subject = f"{proj} — weekly digest"
    # UI-14: resolve this project's digest recipients (falls back to the global list if unset).
    results = notify.send(subject, d["content"], project=store.DEFAULT_PROJECT, kind="digest")
    print(f"digest #{d['id']} generated; notify: {results}")
    return results


def poll_inbox():
    """Poll the Live Inbox mailbox (IMAP) and queue triaged messages. No-op until configured."""
    import inbox_source
    res = inbox_source.poll()
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
    import narration_cutover
    if narration_cutover.event_primary_enabled():
        # NARRATE-14: the event-driven path is primary and owns publishing. The legacy queue must
        # not publish from the second path during/after cutover ("do not publish from both paths").
        print("narrate_pending: skipped (PM_NARRATION_EVENT_PRIMARY on; event path is primary)")
        return 0
    import narrate as narrate_mod
    total = deliverables = 0
    for project_id in store.project_ids():
        store.init_db(project_id)
        # BUG-44: run_deliverables computes full cross-project mission status for
        # every deliverable before its fingerprint can self-skip. Doing that on
        # every nominally idle 45s drain kept one production CPU saturated. A
        # task create/status transition already enqueues pending_narrations, so
        # use that durable queue as the cheap project-level dirty signal.
        pending = store.list_pending_narrations(project=project_id)
        results = narrate_mod.run_pending(project=project_id)
        # NARRATE-3: only inspect deliverable fingerprints when this project had
        # material narration work. Explicit/manual run_deliverables remains
        # available for deliverable-only edits and repair/backfill operations.
        # ``or results`` closes the small race where a row is enqueued between
        # the cheap pre-read and run_pending's own queue read.
        deliv = narrate_mod.run_deliverables(project=project_id) if (pending or results) else []
        print(f"  [{project_id}] narrated {len(results)} task(s), {len(deliv)} deliverable(s)")
        total += len(results)
        deliverables += len(deliv)
    print(f"narrate_pending: {total} task(s), {deliverables} deliverable(s)")
    return total


def narrate_events():
    """NARRATE-14 event-driven narration recovery sweep — the SLOW backstop timer.

    Drains every project's durable narration outbox through the NARRATE-9 worker and the
    compare-and-swap publish boundary. In production the post-commit wake accelerator (registered in
    the web process) is the primary, near-real-time trigger; this timer only catches missed/failed
    wakes, so it runs on a slow cadence and is idempotent. No-op unless PM_NARRATION_EVENT_PRIMARY is
    enabled, so the timer is safe to install before the operator flips the cutover on."""
    import narration_cutover
    result = narration_cutover.run_recovery_sweep()
    if not result.get("enabled"):
        print("narrate_events: skipped (PM_NARRATION_EVENT_PRIMARY off; legacy path is primary)")
        return result
    for project_id, tally in (result.get("projects") or {}).items():
        print(f"  [{project_id}] {json.dumps(tally, sort_keys=True)}")
    print(f"narrate_events: swept {len(result.get('projects') or {})} project(s)")
    return result


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
    lock_path = os.environ.get("PM_RECON_ALERT_LOCK_PATH", "/tmp/projectplanner-reconcile-alerts.lock")
    with _single_flight_lock(lock_path) as acquired:
        if not acquired:
            print(f"reconcile_alerts: skipped overlap lock={lock_path}")
            return {"skipped": True, "reason": "overlap", "lock_path": lock_path}
        projects = _configured_projects("PM_RECON_ALERT_PROJECTS", "all")
        alert_to = os.environ.get("PM_RECON_ALERT_TO", "switchboard/operator")
        min_severity = os.environ.get("PM_RECON_ALERT_MIN_SEVERITY", "medium")
        dedupe_s = int(os.environ.get("PM_RECON_ALERT_DEDUPE_SECONDS", "3600"))
        incremental = (os.environ.get("PM_RECON_INCREMENTAL", "1").strip().lower()
                       not in ("0", "false", "no", "off"))
        sent = deduped = findings = 0
        results = []
        for project_id in projects:
            store.init_db(project_id)
            store.seed_if_empty(project_id)
            res = store.run_reconcile_alerts(
                project=project_id, alert_to=alert_to,
                min_severity=min_severity, dedupe_window_s=dedupe_s,
                incremental=incremental)
            # Keep the scheduled job's aggregate response bounded.  A reconcile report
            # can contain hundreds of richly annotated findings; retaining every full
            # report until all projects finish pushed the 1 GB production VM into its
            # 180 MB cgroup MemoryHigh on every cycle.  The detailed report is already
            # persisted/audited by store.run_reconcile_alerts; this coordinator needs
            # only the small per-project outcome.
            results.append({
                "project": project_id,
                "ok": bool(res.get("ok")),
                "finding_count": int(res.get("finding_count") or 0),
                "alert_sent": bool(res.get("alert_sent")),
                "deduped": bool(res.get("deduped")),
                "message_id": res.get("message_id"),
            })
            sent += 1 if res.get("alert_sent") else 0
            deduped += 1 if res.get("deduped") else 0
            findings += int(res.get("finding_count") or 0)
            print(f"  [{project_id}] findings={res.get('finding_count', 0)} "
                  f"alert_sent={res.get('alert_sent')} deduped={res.get('deduped')} "
                  f"message_id={res.get('message_id')}")
        print(f"reconcile_alerts: projects={len(projects)} findings={findings} "
              f"sent={sent} deduped={deduped} alert_to={alert_to}")
        return {"projects": projects, "findings": findings, "sent": sent,
                "deduped": deduped, "incremental": incremental, "results": results}


def coordinator_audit():
    """Run the COORD-2 T0 observer across explicitly selected projects.

    The observation/planning core opens every board with SQLite mode=ro + query_only.
    Its only persistent effect is one bounded ``coordinator.audit.plan`` activity per
    selected project when PM_COORDINATOR_AUDIT_LOG is enabled (the default).
    """
    import coordinator_audit as audit_mod

    projects = _configured_projects("PM_COORDINATOR_AUDIT_PROJECTS", "switchboard")
    persist = audit_mod.enabled_from_env("PM_COORDINATOR_AUDIT_LOG", True)
    max_recommendations = int(os.environ.get("PM_COORDINATOR_AUDIT_MAX_RECOMMENDATIONS", "100"))
    reconcile_stale_seconds = int(
        os.environ.get("PM_COORDINATOR_AUDIT_RECONCILE_STALE_SECONDS", "900"))
    actor = (os.environ.get("PM_COORDINATOR_AUDIT_ACTOR") or
             "switchboard/coordinator-t0").strip()
    result = audit_mod.audit_projects(
        projects,
        actor=actor,
        persist=persist,
        max_recommendations=max_recommendations,
        reconcile_stale_seconds=reconcile_stale_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    if not result.get("ok"):
        raise RuntimeError("coordinator audit failed closed; inspect the emitted receipt")
    return result


def claim_gate_prs():
    """Post SESSION-12 claim-gate commit statuses for open fleet PRs (CI-7).

    VM verification (`Switchboard CI / VM gate`) runs on projectplanner-ci via the
    scratchpad verify workflow; this job is claim-gate-only (no git/checkout).
    """
    cmd = [sys.executable, str(Path(__file__).parent / "scripts" / "switchboard_pr_gate.py"),
           "--once-open-prs"]
    subprocess.run(cmd, check=True, cwd=Path(__file__).parent)


def dispatch_ci():
    """Rollback-only operator CLI for the retired primary pull route."""
    import ci_verify_dispatch
    args = sys.argv[2:] if len(sys.argv) > 2 else ["--help"]
    raise SystemExit(ci_verify_dispatch.main(args))


def dispatch_scratchpad():
    """Operator CLI: validate/dispatch scratchpad CI for one PR (see ci_scratchpad_dispatch.py)."""
    import ci_scratchpad_dispatch
    args = sys.argv[2:] if len(sys.argv) > 2 else ["--help"]
    raise SystemExit(ci_scratchpad_dispatch.main(args))


def merge_coordinator_plan():
    """Run the Switchboard merge-coordinator once (HARDEN-72 / CI-5, Lever 6).

    Computes a dependency-ordered, back-pressured merge plan for open PRs and records it as a
    `ci.merge_plan` activity. Safe by default: it only PLANS. Set
    ``SWITCHBOARD_MERGE_COORDINATOR_ARM=1`` to also enable GitHub auto-merge on the released
    PRs (in dependency order) — flip that on only after watching the logged plans look right.
    """
    cmd = [sys.executable, str(Path(__file__).parent / "merge_coordinator.py")]
    if os.environ.get("SWITCHBOARD_MERGE_COORDINATOR_ARM", "").lower() in ("1", "true", "yes"):
        cmd.append("--arm")
    subprocess.run(cmd, check=True, cwd=Path(__file__).parent)


def background_job(job_name: str = "", project: str = ""):
    """Run a checkpointed background job from the catalog (RECON-10)."""
    import background_jobs
    job_name = job_name or os.environ.get("PM_BACKGROUND_JOB", "")
    project = project or os.environ.get("PM_BACKGROUND_JOB_PROJECT", "switchboard")
    if not job_name:
        raise SystemExit("usage: python jobs.py background_job <job_name>")
    result = background_jobs.run_background_job(project, job_name, actor="jobs/background_job")
    print(json.dumps(result.get("summary") or result, indent=2, sort_keys=True))
    return result


JOBS = {"weekly_digest": weekly_digest, "poll_inbox": poll_inbox,
        "summarize_pending": summarize_pending, "narrate_pending": narrate_pending,
        "narrate_events": narrate_events,
        "sweep_monitors": sweep_monitors,
        "reconcile_alerts": reconcile_alerts,
        "coordinator_audit": coordinator_audit,
        "claim_gate_prs": claim_gate_prs,
        "dispatch_ci": dispatch_ci,
        "dispatch_scratchpad": dispatch_scratchpad,
        "merge_coordinator_plan": merge_coordinator_plan,
        "background_job": background_job}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "weekly_digest"
    fn = JOBS.get(name, weekly_digest)
    if name == "background_job":
        fn(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        fn()
