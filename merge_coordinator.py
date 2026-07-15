"""HARDEN-72 (CI-5 / Lever 6) — Switchboard merge-coordinator.

A fleet of agents opening PRs against one master needs two things beyond a green
gate to land without blocking each other:

* **Work partitioning** — already provided by ``claim_files`` (store/mcp_server):
  agents lease disjoint file sets so their branches don't collide in the first place.
* **Ordered, back-pressured landing** — *this* module. Given the set of open PRs, it
  decides which are eligible to merge *now* and in what order, so that:
    - a PR only lands after the PRs its task ``depends_on`` have landed
      (dependency order — no merging a consumer before its dependency);
    - only green, provenance-backed, conflict-free PRs are released; and
    - no more than ``max_in_flight`` land per pass, and **nothing** is released while
      the box is saturated (backpressure — protects the 2-vCPU prod box, HARDEN-32).

The *physical* merge is done by GitHub auto-merge + the native merge queue
(HARDEN-68 / HARDEN-70); this coordinator decides **what to release into that
pipeline**. It is intentionally FastAPI-free and side-effect-free at its core
(pure ``plan_merges``) so it is unit-testable without the web app or GitHub, and a
thin ``coordinate`` driver arms merges only when explicitly told to (``dry_run``
defaults to True — the coordinator never merges anything by accident).

See docs/CI-STRATEGY.md and the ``deliverable-ci-concurrency`` deliverable (L6/L7).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

SCHEMA = "switchboard.merge_coordinator.v1"

DEFAULT_MAX_IN_FLIGHT = 3

# Deferral reason codes (stable — surfaced in logs/activity and asserted in tests).
REASON_DRAFT = "draft"
REASON_NO_PROVENANCE = "no_provenance"
REASON_NOT_GREEN = "not_green"
REASON_CONFLICTS = "conflicts"
REASON_BLOCKED = "blocked_by_dependency"
HOLD_CAPACITY = "backpressure_capacity"
HOLD_SATURATED = "backpressure_saturated"


@dataclass
class PRCandidate:
    """One open PR as the coordinator sees it. ``task_ids`` are the board tasks the PR
    claims (from ``task_id_parser``); ``gate_state`` is the VM-gate commit-status state
    on the head SHA; ``claim_backed`` is the SESSION-12 provenance verdict."""
    number: int
    head_sha: str = ""
    task_ids: Sequence[str] = field(default_factory=tuple)
    gate_state: str = "missing"          # success|pending|failure|error|missing
    claim_backed: bool = False
    mergeable: Optional[bool] = None      # GitHub mergeable flag (None = unknown)
    draft: bool = False
    base: str = "master"
    title: str = ""

    def as_ref(self) -> Dict[str, Any]:
        return {"pr": self.number, "sha": self.head_sha,
                "task_ids": list(self.task_ids), "base": self.base}


def max_in_flight_from_env(default: int = DEFAULT_MAX_IN_FLIGHT) -> int:
    """Backpressure cap from ``SWITCHBOARD_MERGE_MAX_IN_FLIGHT`` (>=0), else default."""
    raw = (os.environ.get("SWITCHBOARD_MERGE_MAX_IN_FLIGHT") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(0, val)


def is_box_saturated(saturation_signals: Optional[Dict[str, Any]]) -> bool:
    """Interpret a ``saturation_signals.compute_saturation_signals`` snapshot as a
    hold/no-hold for merging. Any red/critical alert (or an explicit ``saturated``
    flag / ``status`` of red|critical) means: release nothing this pass. Defensive —
    an unrecognized shape is treated as *not* saturated (fail-open on merging is fine;
    the green gate and merge queue still protect correctness)."""
    if not isinstance(saturation_signals, dict):
        return False
    if saturation_signals.get("saturated") is True:
        return True
    status = str(saturation_signals.get("status") or "").strip().lower()
    if status in ("red", "critical"):
        return True
    alerts = saturation_signals.get("alerts")
    if isinstance(alerts, list):
        for alert in alerts:
            sev = str((alert or {}).get("severity") or "").strip().lower() \
                if isinstance(alert, dict) else ""
            if sev in ("red", "critical"):
                return True
    return False


def _blocking_deps(candidate: PRCandidate, task_deps: Dict[str, Sequence[str]],
                   open_task_ids: Set[str]) -> List[str]:
    """Dependency task ids that must land before this PR: a ``depends_on`` target
    whose PR is still open (in ``open_task_ids``) and isn't one of this PR's own
    tasks. A dep that's already merged (absent from ``open_task_ids``) never blocks."""
    own = {str(t) for t in candidate.task_ids}
    blocking: List[str] = []
    for tid in candidate.task_ids:
        for dep in task_deps.get(str(tid), ()) or ():
            dep = str(dep)
            if dep and dep in open_task_ids and dep not in own and dep not in blocking:
                blocking.append(dep)
    return blocking


def plan_merges(candidates: Iterable[PRCandidate], *,
                task_deps: Optional[Dict[str, Sequence[str]]] = None,
                open_task_ids: Optional[Iterable[str]] = None,
                max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
                in_flight: int = 0,
                saturated: bool = False) -> Dict[str, Any]:
    """Decide which open PRs to release into the merge pipeline now.

    Returns ``{schema, release, hold, defer, capacity, ...}``:
      * ``release`` — ordered PRs eligible to land this pass (dependency-satisfied,
        green, backed, conflict-free), truncated to the backpressure capacity.
      * ``hold``    — eligible PRs held back purely by backpressure (capacity or
        saturation) — they'll release on a later pass.
      * ``defer``   — PRs not eligible yet, each with a ``reason`` (and ``blocked_by``
        for dependency waits).

    Eligible PRs have all dependencies satisfied *by construction* (a dep that were an
    open sibling PR would put this PR in ``defer``), so among the eligible set there
    are no inter-dependencies — a deterministic sort by PR number is a valid landing
    order."""
    task_deps = {str(k): list(v or ()) for k, v in (task_deps or {}).items()}
    cands = list(candidates)
    if open_task_ids is None:
        open_ids: Set[str] = set()
        for c in cands:
            open_ids.update(str(t) for t in c.task_ids)
    else:
        open_ids = {str(t) for t in open_task_ids}

    eligible: List[PRCandidate] = []
    defer: List[Dict[str, Any]] = []
    for c in cands:
        if c.draft:
            defer.append({**c.as_ref(), "reason": REASON_DRAFT})
            continue
        if not c.claim_backed:
            defer.append({**c.as_ref(), "reason": REASON_NO_PROVENANCE})
            continue
        if c.gate_state != "success":
            defer.append({**c.as_ref(), "reason": REASON_NOT_GREEN,
                          "gate_state": c.gate_state})
            continue
        if c.mergeable is False:
            defer.append({**c.as_ref(), "reason": REASON_CONFLICTS})
            continue
        blocking = _blocking_deps(c, task_deps, open_ids)
        if blocking:
            defer.append({**c.as_ref(), "reason": REASON_BLOCKED,
                          "blocked_by": blocking})
            continue
        eligible.append(c)

    eligible.sort(key=lambda c: c.number)

    capacity = 0 if saturated else max(0, int(max_in_flight) - max(0, int(in_flight)))
    release_cands = eligible[:capacity]
    held_cands = eligible[capacity:]
    hold_reason = HOLD_SATURATED if saturated else HOLD_CAPACITY

    release = [{**c.as_ref(), "order": i} for i, c in enumerate(release_cands)]
    hold = [{**c.as_ref(), "reason": hold_reason} for c in held_cands]

    return {
        "schema": SCHEMA,
        "saturated": bool(saturated),
        "max_in_flight": int(max_in_flight),
        "in_flight": int(in_flight),
        "capacity": capacity,
        "eligible_count": len(eligible),
        "release": release,
        "hold": hold,
        "defer": defer,
        "counts": {"candidates": len(cands), "release": len(release),
                   "hold": len(hold), "defer": len(defer)},
    }


def format_plan(plan: Dict[str, Any]) -> str:
    """One-line-per-PR human summary for logs / operator digest."""
    lines = [
        f"merge plan: {plan['counts']['release']} release / "
        f"{plan['counts']['hold']} hold / {plan['counts']['defer']} defer "
        f"(cap={plan['capacity']}, in_flight={plan['in_flight']}, "
        f"saturated={plan['saturated']})"
    ]
    for r in plan.get("release", []):
        lines.append(f"  RELEASE #{r['pr']} [{','.join(r.get('task_ids') or []) or '-'}]")
    for h in plan.get("hold", []):
        lines.append(f"  HOLD    #{h['pr']} ({h['reason']})")
    for d in plan.get("defer", []):
        extra = f" -> {','.join(d.get('blocked_by'))}" if d.get("blocked_by") else ""
        lines.append(f"  DEFER   #{d['pr']} ({d['reason']}{extra})")
    return "\n".join(lines)


def coordinate(candidates: Iterable[PRCandidate], *,
               task_deps: Optional[Dict[str, Sequence[str]]] = None,
               open_task_ids: Optional[Iterable[str]] = None,
               max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
               in_flight: int = 0,
               saturated: bool = False,
               arm_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
               dry_run: bool = True) -> Dict[str, Any]:
    """Plan, then (only when ``dry_run`` is False and ``arm_fn`` is given) arm each
    released PR for merge **in dependency order**. ``arm_fn`` is the physical action
    — e.g. enable GitHub auto-merge on the PR — injected so the core stays testable
    and so nothing merges unless a caller opts in. Returns the plan augmented with an
    ``armed`` list recording each action's outcome. Safe by default: with the default
    ``dry_run=True`` this is a pure planning call."""
    plan = plan_merges(candidates, task_deps=task_deps, open_task_ids=open_task_ids,
                       max_in_flight=max_in_flight, in_flight=in_flight,
                       saturated=saturated)
    armed: List[Dict[str, Any]] = []
    if not dry_run and arm_fn is not None:
        for ref in plan["release"]:
            try:
                result = arm_fn(ref)
                armed.append({"pr": ref["pr"], "ok": True, "result": result})
            except Exception as exc:  # one PR's arm failure must not abort the batch
                armed.append({"pr": ref["pr"], "ok": False, "error": str(exc)})
    plan["armed"] = armed
    plan["dry_run"] = bool(dry_run)
    return plan


# --------------------------------------------------------------------------------------
# Collection layer — turn raw open PRs + board state into candidates. Access to GitHub
# and the board is injected so this stays unit-testable; ``main`` wires the real helpers.
# --------------------------------------------------------------------------------------

def collect_candidates(open_prs: Iterable[Dict[str, Any]], *,
                       gate_state_fn: Callable[[Dict[str, Any], str], str],
                       backed_fn: Callable[[Dict[str, Any], Sequence[str]], bool],
                       task_ids_fn: Callable[[Dict[str, Any]], Sequence[str]],
                       ) -> List[PRCandidate]:
    """Build ``PRCandidate``s from GitHub PR dicts. ``gate_state_fn(pr, sha)`` returns the
    VM-gate commit-status state, ``backed_fn(pr, task_ids)`` the SESSION-12 provenance
    verdict, ``task_ids_fn(pr)`` the claimed task ids — all injected so this is testable
    without GitHub or the board."""
    out: List[PRCandidate] = []
    for pr in open_prs:
        head = pr.get("head") or {}
        sha = str(head.get("sha") or "")
        task_ids = [str(t) for t in (task_ids_fn(pr) or [])]
        out.append(PRCandidate(
            number=int(pr.get("number")),
            head_sha=sha,
            task_ids=task_ids,
            gate_state=gate_state_fn(pr, sha),
            claim_backed=bool(backed_fn(pr, task_ids)),
            mergeable=pr.get("mergeable"),
            draft=bool(pr.get("draft")),
            base=str((pr.get("base") or {}).get("ref") or "master"),
            title=str(pr.get("title") or ""),
        ))
    return out


def open_task_ids(candidates: Iterable[PRCandidate]) -> Set[str]:
    """Every task id that currently has an open PR — a dependency in this set is unlanded."""
    ids: Set[str] = set()
    for c in candidates:
        ids.update(str(t) for t in c.task_ids)
    return ids


def build_task_deps(candidates: Iterable[PRCandidate], *,
                    get_deps_fn: Callable[[str], Sequence[str]]) -> Dict[str, List[str]]:
    """``{task_id: [dep_task_id, ...]}`` for every task on a candidate PR. ``get_deps_fn``
    resolves one task's ``depends_on`` (e.g. from the board); errors resolve to no deps."""
    deps: Dict[str, List[str]] = {}
    for c in candidates:
        for tid in c.task_ids:
            tid = str(tid)
            if tid in deps:
                continue
            try:
                deps[tid] = [str(d) for d in (get_deps_fn(tid) or [])]
            except Exception:
                deps[tid] = []
    return deps


def count_armed_prs(prs: Iterable[Dict[str, Any]]) -> int:
    """Open PRs with GitHub auto-merge already enabled = merges currently *in flight*
    (armed but not yet landed). Used as the live backpressure ``in_flight`` count so the
    lease is a true mutex sized by ``max_in_flight`` — not the static env guess it was."""
    return sum(1 for pr in prs if (pr or {}).get("auto_merge"))


def _load_gate():
    """Import scripts/switchboard_pr_gate.py for its GitHub helpers (one GitHub client for
    the whole gate — do not build a second one; ADR-0006 subtraction rule)."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent / "scripts" / "switchboard_pr_gate.py"
    spec = importlib.util.spec_from_file_location("switchboard_pr_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: Optional[List[str]] = None) -> int:
    """Dry-run merge plan for the primary repo (safe by default). Arms auto-merge on the
    released PRs, in dependency order, only with ``--arm``. This is the runnable surface of
    the coordinator; a systemd timer can invoke it just like the PR gate."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Switchboard dependency-ordered, "
                                                 "back-pressured merge coordinator (CI-5 / L6).")
    parser.add_argument("--repo", default="")
    parser.add_argument("--project", default=os.environ.get("SWITCHBOARD_CI_PROJECT", "switchboard"))
    parser.add_argument("--max-in-flight", type=int, default=max_in_flight_from_env())
    parser.add_argument("--in-flight", type=int, default=-1,
                        help="Backpressure in-flight count. Default (-1) = auto: count open PRs "
                             "with auto-merge already armed (a true landing mutex). Pass an "
                             "explicit value to override.")
    parser.add_argument("--saturated", action="store_true",
                        help="Force backpressure hold (release nothing). Without it the box "
                             "saturation signal is consulted.")
    parser.add_argument("--arm", action="store_true",
                        help="Actually enable GitHub auto-merge on released PRs (in order). "
                             "Off by default — the coordinator only plans unless told to act.")
    parser.add_argument("--json", action="store_true", help="Emit the plan as JSON.")
    args = parser.parse_args(argv)

    import pr_provenance_gate
    import store
    import task_id_parser
    gate = _load_gate()
    repo = args.repo or gate._repo()
    token = gate._token()
    if not token:
        print("ERROR: set PM_GITHUB_TOKEN, GITHUB_TOKEN, or SWITCHBOARD_CI_GITHUB_TOKEN.")
        return 2
    context = os.environ.get("SWITCHBOARD_CI_STATUS_CONTEXT", gate.DEFAULT_CONTEXT)

    prs = gate.list_open_prs(repo, token=token)
    # Live backpressure: how many merges are already armed-and-unlanded. With max_in_flight=1
    # this makes the coordinator a strict landing lease — exactly one PR is rebased-and-arming
    # at a time, so the others stay at their base and never stampede the gate.
    in_flight = count_armed_prs(prs) if args.in_flight < 0 else args.in_flight

    def gate_state_fn(pr, sha):
        st = gate.latest_status(repo, sha, context, token=token)
        return (st or {}).get("state") or "missing"

    def backed_fn(pr, task_ids):
        verdict = pr_provenance_gate.evaluate_pr_provenance(
            pr, repo=repo, mode="warn", record_activity=False)
        # "Backed" = the provenance gate wouldn't block it (covered / exempt / non-code).
        return not verdict.get("would_block")

    def task_ids_fn(pr):
        return task_id_parser.task_ids_for_pr(pr)

    candidates = collect_candidates(prs, gate_state_fn=gate_state_fn,
                                    backed_fn=backed_fn, task_ids_fn=task_ids_fn)

    def get_deps_fn(tid):
        try:
            task = store.get_task(tid, project=args.project)
            return (task or {}).get("depends_on") or []
        except Exception:
            return []

    deps = build_task_deps(candidates, get_deps_fn=get_deps_fn)

    arm_fn = None
    if args.arm:
        # Lease action for each released PR (<= capacity, dependency-ordered):
        #   1. update-branch — rebase onto the base tip so the PR gets ONE fresh against-tip CI
        #      run before it can land. Branch protection is strict:false, so without this a
        #      behind PR could auto-merge untested against current master.
        #   2. enable auto-merge (squash) — it lands the moment that fresh run is green.
        # Only released PRs are rebased; held/deferred behind PRs stay put and never stampede
        # the gate. This supersedes the blanket scripts/auto_update_prs.py sweep.
        def arm_fn(ref):  # noqa: E306 - local by design
            number = int(ref["pr"])
            updated = _update_branch(gate, repo, number, token)
            armed = _enable_auto_merge(gate, repo, number, token)
            return {"pr": number, "updated": updated, "armed": armed}

    plan = coordinate(candidates, task_deps=deps, open_task_ids=open_task_ids(candidates),
                      max_in_flight=args.max_in_flight, in_flight=in_flight,
                      saturated=args.saturated, arm_fn=arm_fn, dry_run=not args.arm)

    print(json.dumps(plan, sort_keys=True) if args.json else format_plan(plan))
    try:
        store.append_activity("ci.merge_plan", "switchboard-ci/merge-coordinator",
                              {"schema": SCHEMA, "repo": repo, **plan}, project=args.project)
    except Exception:
        pass
    return 0


def _update_branch(gate, repo: str, number: int, token: str) -> Dict[str, Any]:
    """Rebase a released PR onto the base tip via GitHub 'update-branch' (merge base → head),
    forcing a fresh against-tip CI run before it lands. Only the coordinator's released PRs
    are rebased — that is the lease that replaces the blanket ``auto_update_prs`` sweep, so
    behind PRs waiting their turn don't each burn a gate run. ``gh`` runs with check=False;
    a benign 422 (already current / transient conflict) is reported in the returncode, not
    raised — a genuinely conflicting PR is filtered out earlier by ``plan_merges`` (mergeable)."""
    import subprocess
    env = dict(os.environ, GH_TOKEN=token)
    proc = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{number}/update-branch"],
        text=True, capture_output=True, env=env, check=False)
    return {"pr": number, "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:200]}


def _enable_auto_merge(gate, repo: str, number: int, token: str) -> Dict[str, Any]:
    """Enable GitHub auto-merge (squash) on a PR via `gh` — queue-and-forget landing.
    Kept tiny and separate so ``main`` reads cleanly and tests can stub it."""
    import subprocess
    env = dict(os.environ, GH_TOKEN=token)
    proc = subprocess.run(
        ["gh", "pr", "merge", str(number), "--repo", repo, "--squash", "--auto"],
        text=True, capture_output=True, env=env, check=False)
    return {"pr": number, "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:200]}


if __name__ == "__main__":
    raise SystemExit(main())
