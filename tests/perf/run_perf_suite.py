#!/usr/bin/env python3
"""Perf suite orchestrator + CI gate.

Runs the HTTP asset audit and (optionally) the Playwright load probe,
scores both against tests/perf/budgets.json, writes a JSON receipt and a
ranked markdown report, and exits non-zero on any budget breach so it works
as a merge gate.

  python tests/perf/run_perf_suite.py --base-url https://plan.taikunai.com
  python tests/perf/run_perf_suite.py --base-url http://localhost:8110 \
      --storage-state .auth/board.json          # authed board
  python tests/perf/run_perf_suite.py --audit-only   # no browser (fast CI)
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import asset_audit  # noqa: E402
import icon_usage  # noqa: E402


def _kb(b: int) -> int:
    return b // 1024


def score(audit: dict, probe: dict | None, budgets: dict) -> list[dict]:
    breaches = []

    def check(ok, name, got, limit):
        breaches.append({"check": name, "got": got, "limit": limit,
                         "pass": bool(ok)})

    ab = budgets["assets"]
    biggest = max((a.get("transfer_bytes", 0) for a in audit["assets"]), default=0)
    check(_kb(biggest) <= ab["max_single_asset_kb"], "assets.max_single_asset_kb",
          _kb(biggest), ab["max_single_asset_kb"])
    uncompressed = [f for f in audit["findings"] if f["kind"] == "uncompressed"]
    check(ab["allow_uncompressed_text"] or not uncompressed,
          "assets.no_uncompressed_text", len(uncompressed), 0)
    check(audit["icons"]["waste_ratio"] <= ab["icon_waste_ratio_max"],
          "assets.icon_waste_ratio", audit["icons"]["waste_ratio"],
          ab["icon_waste_ratio_max"])

    if probe:
        for cond in ("cold", "throttled"):
            m = probe["conditions"].get(cond, {})
            if "error" in m or not m:
                continue
            for key, budkey in (("lcp", "lcp_ms"), ("load", "load_ms"),
                                ("tbt", "tbt_ms")):
                if budkey in budgets.get(cond, {}):
                    check(m[key] <= budgets[cond][budkey],
                          f"{cond}.{budkey}", m[key], budgets[cond][budkey])
            if "transfer_kb" in budgets.get(cond, {}):
                check(_kb(m["transfer"]) <= budgets[cond]["transfer_kb"],
                      f"{cond}.transfer_kb", _kb(m["transfer"]),
                      budgets[cond]["transfer_kb"])
            if "request_count" in budgets.get(cond, {}):
                check(m["request_count"] <= budgets[cond]["request_count"],
                      f"{cond}.request_count", m["request_count"],
                      budgets[cond]["request_count"])
    return breaches


def font_coverage(probe: dict | None) -> dict:
    """Verify a subset font would cover every icon seen in the live DOM."""
    if not probe or not probe.get("icons_seen"):
        return {"checked": False}
    safe_set = icon_usage.used_icons()["all"]
    seen = set(probe["icons_seen"])
    missing = sorted(seen - safe_set)
    return {"checked": True, "dom_icons": len(seen),
            "subset_covers": len(seen - set(missing)),
            "missing_from_subset": missing,
            "safe_to_ship_subset": not missing}


def render_report(audit, probe, breaches, coverage) -> str:
    L = ["# Load performance report", "", f"Target: `{audit['base_url']}`", ""]
    status = "✅ PASS" if all(b["pass"] for b in breaches) else "❌ FAIL"
    L += [f"**Budget gate: {status}**", ""]

    L += ["## Budget checks", "", "| check | got | limit | |", "|---|---|---|---|"]
    for b in sorted(breaches, key=lambda x: x["pass"]):
        L.append(f"| {b['check']} | {b['got']} | {b['limit']} | "
                 f"{'✅' if b['pass'] else '❌'} |")
    L.append("")

    if probe:
        L += ["## Load timing (median of N runs)", "",
              "| condition | LCP | FCP | load | TBT | req | transfer |",
              "|---|---|---|---|---|---|---|"]
        for cond, m in probe["conditions"].items():
            if "error" in m:
                L.append(f"| {cond} | error: {m['error'][:40]} | | | | | |"); continue
            L.append(f"| {cond} | {m['lcp']}ms | {m['fcp']}ms | {m['load']}ms | "
                     f"{m['tbt']}ms | {m['request_count']} | {_kb(m['transfer'])}KB |")
        L.append("")

    ic = audit["icons"]
    L += ["## Icon font bloat", "",
          f"- Defined in shipped font: **{ic['defined']}**",
          f"- Actually used (safe superset): **{ic['used']}**",
          f"- Waste: **{int(ic['waste_ratio']*100)}%**", ""]
    if coverage.get("checked"):
        L.append(f"- Runtime DOM icons observed: {coverage['dom_icons']}; "
                 f"subset covers all: "
                 f"{'YES ✅' if coverage['safe_to_ship_subset'] else 'NO ❌ ' + str(coverage['missing_from_subset'])}")
        L.append("")

    top = sorted((a for a in audit["assets"] if "transfer_bytes" in a),
                 key=lambda a: a["transfer_bytes"], reverse=True)[:8]
    L += ["## Heaviest assets", "", "| asset | KB | enc | cache |", "|---|---|---|---|"]
    for a in top:
        L.append(f"| `{a['path']}` | {_kb(a['transfer_bytes'])} | {a['encoding']} | "
                 f"{'immutable' if a['immutable'] else (a['cache_control'] or '-')} |")
    L.append("")
    if audit["findings"]:
        L += ["## Findings", ""]
        for f in audit["findings"]:
            L.append(f"- **{f['severity']}** · {f['kind']} · `{f.get('path','')}` "
                     f"{f.get('bytes','')}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://plan.taikunai.com")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--storage-state", default=None)
    ap.add_argument("--audit-only", action="store_true",
                    help="skip the browser probe (fast, no Playwright)")
    ap.add_argument("--out-dir", default=str(ROOT / ".artifacts"))
    args = ap.parse_args()

    budgets = json.loads((HERE / "budgets.json").read_text())
    audit = asset_audit.audit(args.base_url)

    probe = None
    if not args.audit_only:
        import load_probe
        probe = load_probe.probe(args.base_url.rstrip("/") + "/",
                                 args.runs, args.storage_state)

    breaches = score(audit, probe, budgets)
    coverage = font_coverage(probe)

    # Build-time proof the subset font covers every used icon, if it's built.
    import subset_icons
    if subset_icons.OUT_DIR.joinpath("tabler-icons.subset.woff2").is_file():
        cov = subset_icons.verify_coverage()
        breaches.append({"check": "subset.font_covers_used_icons",
                         "got": f"{len(cov['missing'])} missing", "limit": 0,
                         "pass": cov["ok"]})
        coverage["cmap_missing"] = cov["missing"]

    passed = all(b["pass"] for b in breaches) and coverage.get("safe_to_ship_subset", True)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "switchboard.perf_audit.v1", "base_url": args.base_url,
               "passed": passed, "breaches": breaches, "audit": audit,
               "probe": probe, "font_coverage": coverage}
    (out / "perf-audit-receipt.json").write_text(json.dumps(receipt, indent=2))
    report = render_report(audit, probe, breaches, coverage)
    (out / "perf-report.md").write_text(report)

    print(report)
    print(f"\nreceipt: {out / 'perf-audit-receipt.json'}")
    fails = [b for b in breaches if not b["pass"]]
    print(f"gate: {'PASS' if passed else 'FAIL'}  "
          f"({len(fails)} budget breach{'es' if len(fails)!=1 else ''})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
