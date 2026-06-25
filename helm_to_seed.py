#!/usr/bin/env python3
"""Convert the Helm epic plan (docs/EPICS.md's structured form) into a
projectplanner seed_plan.json so it loads as a SECOND project (helm.db) and
renders in the board exactly like Maxwell. One-off generator; the committed
output (seeds/helm_seed_plan.json) is what ships — this script is not needed at
runtime.

Source: seeds/helm_plan_source.json  (the .final object from the Helm workflow:
19 epics → workstreams, ~186 tasks). Run:  python helm_to_seed.py
"""
import json
import os

HERE = os.path.dirname(__file__)
SRC = os.environ.get("HELM_PLAN_SRC", os.path.join(HERE, "seeds", "helm_plan_source.json"))
if not os.path.exists(SRC):
    SRC = "/tmp/helm_plan.json"  # fallback to the workflow scratch file
OUT = os.path.join(HERE, "seeds", "helm_seed_plan.json")
GENERATED = "2026-06-25"

# Helm task status -> projectplanner canonical enum.
STATUS = {"done": "Done", "in-progress": "In Progress", "blocked": "Blocked", "todo": "Not Started"}


def split_risk(s: str):
    """A collision-risk string is usually 'X — RESOLVED by Y'. Split the head as
    the risk and the remainder as the mitigation so the risk table reads cleanly."""
    for sep in (" — ", " - "):
        if sep in s:
            head, tail = s.split(sep, 1)
            return head.strip(), tail.strip()
    return s.strip(), ""


def main():
    data = json.load(open(SRC))
    final = data.get("final", data)
    epics = final["epics"]
    waves = {w["wave"]: w for w in final.get("waves", [])}

    workstreams = []
    n_tasks = 0
    all_ids = set()
    crit = []
    for e in epics:
        wave = e.get("wave", 1)
        ws = {"workstream_id": e["id"], "name": e["title"], "tasks": []}
        for idx, t in enumerate(e.get("tasks", [])):
            all_ids.add(t["id"])
            n_tasks += 1
            ws["tasks"].append({
                "task_id": t["id"],
                "title": t["title"],
                "description": e.get("outcome", ""),
                "owner_org": None,
                "owner_person_or_role": None,
                "phase": f"Wave {wave}",
                "effort_days": None,
                "depends_on": t.get("deps", []),
                "entry_criteria": None,
                "exit_criteria": e.get("definitionOfDone"),
                "deliverable": None,
                "risk_level": None,
                "is_blocking": bool(t.get("blocking")),
                "status": STATUS.get(t.get("status"), "Not Started"),
                "start_date": None, "finish_date": None,
                "duration_days": None, "start_day": None,
                "sort_order": wave * 1000 + idx,
            })
            if t.get("blocking"):
                crit.append({"task_id": t["id"], "workstream": e["id"], "why": t["title"]})
        workstreams.append(ws)

    # Milestones: one per wave (its rationale = the gate).
    milestones = []
    for wv in sorted(waves):
        w = waves[wv]
        milestones.append({
            "name": f"Wave {wv} complete — {', '.join(w.get('epicIds', []))}",
            "target_week": f"Wave {wv}",
            "gate_criteria": w.get("rationale", ""),
        })

    risks = []
    for c in final.get("collisionRisks", []):
        risk, mit = split_risk(c)
        risks.append({"risk": risk, "likelihood": "—", "impact": "—", "mitigation": mit})

    n_done = sum(1 for e in epics for t in e.get("tasks", []) if t.get("status") == "done")
    exec_summary = (
        "Helm is a web-first marine chartplotter — a modern successor to OpenCPN that reuses its "
        "battle-tested navigation core (the GPL `model/` library, run headless) behind a clean web/mobile "
        "client, and fuses charts + satellite + weather + AIS + routing onto one offline-first screen.\n\n"
        f"This plan decomposes the product into {len(epics)} discrete epics across 4 dependency-ordered waves "
        f"({n_tasks} tasks, {n_done} already shipped). The architecture is one cohesive C++ engine on the boat "
        "(the safety core — nav, AIS, S-52 charts, alarms) ringed by independent web-native services (places, "
        "weather, AI, routing) that degrade gracefully and never gate navigation. Each epic owns its own files "
        "so parallel streams don't collide; Wave 1's SHELL epic is the keystone that unblocks the wave-2 fan-out."
    )

    seed = {
        "project": "Helm — Marine Navigation Companion",
        "generated": GENERATED,
        "schedule_start": None,
        "schedule_note": "Wave-ordered & dependency-driven, not date-bound. Wave 1 (foundations + the SHELL unblocker) "
                         "is largely shipped; Wave 2 fans out into parallel reference-client streams.",
        "owner_orgs": [],
        "rollups": {"total_workstreams": len(workstreams), "total_tasks": n_tasks, "total_effort_days": 0},
        "executive_summary": exec_summary,
        "timeline_note": "Waves are dependency order, not a calendar. Wave 1 = unblocked now; higher waves wait on deps.",
        "workstreams": workstreams,
        "critical_path": crit,
        "milestones": milestones,
        "consolidated_risks": risks,
        "consolidated_decisions": [],
    }

    # --- validate before writing ---
    unresolved = []
    for ws in workstreams:
        for t in ws["tasks"]:
            for dep in t["depends_on"]:
                if dep not in all_ids:
                    unresolved.append(f"{t['task_id']} -> {dep}")
    os.makedirs(os.path.join(HERE, "seeds"), exist_ok=True)
    json.dump(seed, open(OUT, "w"), indent=1)
    print(f"wrote {OUT}")
    print(f"  workstreams={len(workstreams)} tasks={n_tasks} done={n_done} "
          f"blocking={len(crit)} risks={len(risks)} milestones={len(milestones)}")
    if unresolved:
        print(f"  WARNING {len(unresolved)} unresolved deps (likely cross-wave forward refs, still valid ids):")
        for u in unresolved[:10]:
            print("   ", u)
    else:
        print("  all depends_on ids resolve ✓")


if __name__ == "__main__":
    main()
