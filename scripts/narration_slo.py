#!/usr/bin/env python3
"""NARRATE-14 narration SLO / reconciliation report.

Reads the durable narration outbox + receipt ledger for one or more projects and computes the
production SLO signals the rollout is judged against, without loading narration text or scanning
row-by-row beyond the join it needs:

- **freshness** — request-to-delivery latency (receipt.created_at - outbox.requested_at) for
  delivered narratives, reported as p50/p95/max. Exit criterion: p95 <= 60s.
- **outcomes** — delivered / fallback / error counts + fallback rate.
- **cost reconciliation** — total spend in the window vs the per-project budget ceiling, and the
  spend split by mode (deterministic / llm / fallback), so cost and fallback receipts reconcile.
- **queue health** — actionable depth, dead-letter count, oldest actionable age (idle-CPU proxy:
  a near-zero actionable depth on a quiet board means the event path is not busy-polling).

Usage:
    python scripts/narration_slo.py [--project switchboard] [--window-seconds 86400] [--json]
    python scripts/narration_slo.py --all           # every registered project

Exit code is non-zero when any SLO target is breached, so it can back a drill/alert check.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import narration_generate  # noqa: E402
import narration_outbox  # noqa: E402

# SLO targets (NARRATE-14 exit criteria; overridable via CLI).
FRESHNESS_P95_TARGET_S = 60.0
DEFAULT_WINDOW_S = 86400.0


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 3)


def slo_report(project: str, *, window_seconds: float = DEFAULT_WINDOW_S,
               now: float | None = None,
               freshness_p95_target_s: float = FRESHNESS_P95_TARGET_S) -> dict:
    """Bounded SLO snapshot for one project. Every query is an indexed aggregate or a keyed join."""
    now = time.time() if now is None else now
    since = now - window_seconds
    with narration_outbox._conn(project) as c:
        # Freshness: join each delivered receipt back to its outbox request time.
        fresh_rows = c.execute(
            "SELECT r.created_at - o.requested_at AS age FROM narration_receipts r "
            "JOIN narration_outbox o ON o.event_id = r.event_id "
            "WHERE r.outcome='delivered' AND r.created_at >= ? AND o.requested_at IS NOT NULL",
            (since,),
        ).fetchall()
        outcome_rows = c.execute(
            "SELECT outcome, COUNT(*) AS n FROM narration_receipts WHERE created_at >= ? "
            "GROUP BY outcome", (since,),
        ).fetchall()
        mode_rows = c.execute(
            "SELECT mode, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost "
            "FROM narration_receipts WHERE created_at >= ? GROUP BY mode", (since,),
        ).fetchall()
        queue_rows = c.execute(
            "SELECT attempt_state, COUNT(*) AS n FROM narration_outbox GROUP BY attempt_state"
        ).fetchall()
        oldest = c.execute(
            "SELECT MIN(requested_at) FROM narration_outbox "
            "WHERE attempt_state IN ('pending','retry_wait')"
        ).fetchone()[0]

    ages = [float(r["age"]) for r in fresh_rows if r["age"] is not None and r["age"] >= 0]
    outcomes = {r["outcome"]: int(r["n"]) for r in outcome_rows}
    delivered = outcomes.get("delivered", 0)
    fallback = outcomes.get("fallback", 0)
    error = outcomes.get("error", 0)
    attempts = delivered + fallback + error
    states = {r["attempt_state"]: int(r["n"]) for r in queue_rows}
    actionable = states.get("pending", 0) + states.get("retry_wait", 0)
    dead_letters = states.get("dead_letter", 0)

    total_cost = round(sum(float(r["cost"] or 0.0) for r in mode_rows), 6)
    budget = narration_generate.budget_config(project)
    budget_ceiling = float(budget.get("daily_cost_usd") or 0.0)

    freshness = {
        "delivered_samples": len(ages),
        "p50_seconds": _percentile(ages, 50),
        "p95_seconds": _percentile(ages, 95),
        "max_seconds": round(max(ages), 3) if ages else 0.0,
    }
    slo = {
        "freshness_p95_ok": (freshness["p95_seconds"] <= freshness_p95_target_s) if ages else True,
        "freshness_p95_target_seconds": freshness_p95_target_s,
        "no_dead_letters": dead_letters == 0,
        "cost_within_budget": (total_cost <= budget_ceiling) if budget_ceiling else True,
    }
    slo["all_ok"] = all(v for k, v in slo.items() if k.endswith("_ok") or k.startswith("no_")
                        or k.startswith("cost_"))
    return {
        "project": project,
        "generated_at": now,
        "window_seconds": window_seconds,
        "freshness": freshness,
        "outcomes": {"attempts": attempts, "delivered": delivered, "fallback": fallback,
                     "error": error,
                     "fallback_rate": round((fallback + error) / attempts, 4) if attempts else 0.0},
        "cost": {"total_cost_usd": total_cost, "budget_ceiling_usd": budget_ceiling,
                 "by_mode": [{"mode": r["mode"], "count": int(r["n"]),
                              "cost_usd": round(float(r["cost"] or 0.0), 6)} for r in mode_rows]},
        "queue": {"actionable": actionable, "dead_letters": dead_letters,
                  "oldest_actionable_age_seconds": round(now - oldest, 2) if oldest else 0.0},
        "slo": slo,
    }


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="NARRATE-14 narration SLO / reconciliation report")
    ap.add_argument("--project", default="switchboard", help="project id (default: switchboard)")
    ap.add_argument("--all", action="store_true", help="report every registered project")
    ap.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_S)
    ap.add_argument("--freshness-p95-target", type=float, default=FRESHNESS_P95_TARGET_S)
    ap.add_argument("--json", action="store_true", help="emit raw JSON only")
    args = ap.parse_args(argv)

    import store
    projects = store.project_ids() if args.all else [args.project]
    reports = []
    for project in projects:
        try:
            store.init_db(project)
            reports.append(slo_report(project, window_seconds=args.window_seconds,
                                      freshness_p95_target_s=args.freshness_p95_target))
        except Exception as exc:
            reports.append({"project": project, "error": f"{type(exc).__name__}: {exc}"})

    print(json.dumps(reports if args.all else reports[0], indent=None if args.json else 2,
                     sort_keys=True))
    breached = any(not (r.get("slo") or {}).get("all_ok", True) for r in reports)
    return 1 if breached else 0


if __name__ == "__main__":
    raise SystemExit(main())
