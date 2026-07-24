#!/usr/bin/env python3
"""Measure the REAL populated board's load cost (not the login shell).

Boots the actual board app on a temp DB in dev-open auth mode, seeds a
representative task load, and measures cold/throttled load timing for the
authed board — the render path a hard refresh actually pays. Also collects
every icon the live board renders, the ultimate check that the subset font
covers real usage.

Self-contained: temp DBs, own uvicorn on a random port, torn down after.
    .venv/bin/python tests/perf/probe_board.py [--tasks 60] [--runs 3]
"""
from __future__ import annotations
import argparse
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src", ROOT / "tests" / "perf"):
    sys.path.insert(0, str(_p))


def seed(n_tasks: int):
    import store
    store.init_project_registry()
    store.init_db("maxwell")
    for i in range(n_tasks):
        store.create_task(
            {"workstream_id": "PERF", "title": f"Seeded board task {i+1} "
             f"for render-cost measurement"},
            actor="test", project="maxwell")
    store.create_deliverable(
        {"id": "perf-board", "title": "Perf board load", "status": "approved",
         "end_state": "Board renders a representative task load."},
        actor="test", project="maxwell")


def boot(env, port):
    return subprocess.Popen(
        [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_ready(base, server, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if server.poll() is not None:
            out = server.stdout.read() if server.stdout else ""
            raise RuntimeError(f"app exited early:\n{out[-2000:]}")
        time.sleep(0.2)
    raise TimeoutError("app did not become ready")


OBS = r"""
window.__p={lcp:0,lt:[]};
try{new PerformanceObserver(l=>{for(const e of l.getEntries())window.__p.lcp=Math.max(window.__p.lcp,e.startTime)}).observe({type:'largest-contentful-paint',buffered:true})}catch(e){}
try{new PerformanceObserver(l=>{for(const e of l.getEntries())window.__p.lt.push(e.duration)}).observe({type:'longtask',buffered:true})}catch(e){}
"""
COLLECT = r"""() => {
  const nav=performance.getEntriesByType('navigation')[0]||{};
  const paints={}; for(const p of performance.getEntriesByType('paint'))paints[p.name]=p.startTime;
  const lt=window.__p.lt||[]; let tbt=0; for(const d of lt)tbt+=Math.max(0,d-50);
  const icons=new Set();
  for(const el of document.querySelectorAll('[class*="ti-"]'))
    for(const c of el.classList) if(c.startsWith('ti-')&&c!=='ti-') icons.add(c.slice(3));
  return {ttfb:Math.round(nav.responseStart||0), fcp:Math.round(paints['first-contentful-paint']||0),
    lcp:Math.round(window.__p.lcp||0), dcl:Math.round(nav.domContentLoadedEventEnd||0),
    load:Math.round(nav.loadEventEnd||0), tbt:Math.round(tbt), longtasks:lt.length,
    req:performance.getEntriesByType('resource').length,
    xfer:Math.round(performance.getEntriesByType('resource').reduce((a,r)=>a+(r.transferSize||0),0)/1024),
    icons:[...icons], title:document.title};
}"""


def measure(base, mode, runs):
    from playwright.sync_api import sync_playwright
    rows, icons = [], set()
    with sync_playwright() as pw:
        for _ in range(runs):
            b = pw.chromium.launch(headless=True)
            ctx = b.new_context()
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.clearBrowserCache")
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            if mode == "throttled":
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
                cdp.send("Network.emulateNetworkConditions", {"offline": False,
                    "latency": 150, "downloadThroughput": int(1.6*1024*1024/8),
                    "uploadThroughput": int(750*1024/8)})
            # keep hermetic: stub Mermaid so no CDN fetch
            page.add_init_script("window.mermaid={initialize:()=>{},render:async()=>({svg:'<svg/>'})};")
            page.add_init_script(OBS)
            page.goto(base + "/?project=maxwell", wait_until="load", timeout=45000)
            page.wait_for_timeout(2500)
            d = page.evaluate(COLLECT)
            rows.append(d); icons.update(d["icons"])
            b.close()
    med = lambda k: round(statistics.median(r[k] for r in rows))
    return {k: med(k) for k in ("ttfb", "fcp", "lcp", "dcl", "load", "tbt",
                                "longtasks", "req", "xfer")}, icons, rows[0]["title"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=60)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="perf-board-"))
    env = dict(os.environ)
    env.update({
        "PM_DB_PATH": str(tmp / "maxwell.db"),
        "PM_HELM_DB_PATH": str(tmp / "helm.db"),
        "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
        "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
        "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
        "PM_AUTH_MODE": "dev-open",
        "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
    })
    os.environ.update({k: v for k, v in env.items() if k.startswith("PM_")})
    (tmp / "projects").mkdir(parents=True)

    seed(args.tasks)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = int(s.getsockname()[1])
    base = f"http://127.0.0.1:{port}"
    server = boot(env, port)
    try:
        wait_ready(base, server)
        print(f"Populated board ({args.tasks} tasks), cold-cache, median of {args.runs}\n")
        for mode in ("cold", "throttled"):
            m, icons, title = measure(base, mode, args.runs)
            print(f"  [{mode:9}] FCP={m['fcp']:>5}ms LCP={m['lcp']:>5}ms load={m['load']:>6}ms "
                  f"TBT={m['tbt']:>4}ms longtasks={m['longtasks']:>2} req={m['req']:>3} xfer={m['xfer']:>4}KB")
            if mode == "cold":
                from icon_usage import used_icons
                missing = sorted(icons - used_icons()["all"])
                print(f"             board rendered {len(icons)} distinct icons; subset covers all: "
                      f"{'YES ✅' if not missing else 'NO ❌ ' + str(missing)}\n")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
