#!/usr/bin/env python3
"""Client-side load probe via Playwright CLI (Chromium).

For each condition (cold / warm / throttled) runs N navigations and reports
medians, so prod network jitter doesn't dominate. Captures navigation +
paint timing, LCP, long tasks, Total Blocking Time, and a resource
waterfall. Also collects every `.ti-*` icon class rendered in the DOM so a
subsetted font can be verified to cover real usage before it ships.

Auth: pass --storage-state <file> (a Playwright storageState JSON) to hit
the logged-in board instead of the login shell.
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys

from playwright.sync_api import sync_playwright

OBSERVER = r"""
window.__perf = { lcp: 0, longtasks: [], cls: 0 };
try { new PerformanceObserver((l)=>{for(const e of l.getEntries())
  window.__perf.lcp=Math.max(window.__perf.lcp,e.startTime);})
  .observe({type:'largest-contentful-paint',buffered:true}); } catch(e){}
try { new PerformanceObserver((l)=>{for(const e of l.getEntries())
  window.__perf.longtasks.push({start:e.startTime,dur:e.duration});})
  .observe({type:'longtask',buffered:true}); } catch(e){}
try { new PerformanceObserver((l)=>{for(const e of l.getEntries())
  if(!e.hadRecentInput) window.__perf.cls+=e.value;})
  .observe({type:'layout-shift',buffered:true}); } catch(e){}
"""

COLLECT = r"""() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = p.startTime;
  const res = performance.getEntriesByType('resource').map(r => ({
    name: (r.name.split('/').slice(3).join('/').split('?')[0]) || r.name,
    type: r.initiatorType, transfer: r.transferSize, decoded: r.decodedBodySize,
    dur: Math.round(r.duration), start: Math.round(r.startTime),
  }));
  const lts = window.__perf.longtasks || [];
  let tbt = 0; for (const t of lts) tbt += Math.max(0, t.dur - 50);
  const icons = new Set();
  for (const el of document.querySelectorAll('[class*="ti-"]'))
    for (const c of el.classList) if (c.startsWith('ti-') && c !== 'ti-') icons.add(c.slice(3));
  return {
    ttfb: Math.round(nav.responseStart||0), dcl: Math.round(nav.domContentLoadedEventEnd||0),
    load: Math.round(nav.loadEventEnd||0), fcp: Math.round(paints['first-contentful-paint']||0),
    lcp: Math.round(window.__perf.lcp||0), cls: Math.round((window.__perf.cls||0)*1000)/1000,
    longtask_count: lts.length, tbt: Math.round(tbt),
    transfer: res.reduce((a,r)=>a+(r.transfer||0),0),
    decoded: res.reduce((a,r)=>a+(r.decoded||0),0),
    request_count: res.length, icons: [...icons],
    resources: res.sort((a,b)=>b.dur-a.dur).slice(0,10),
  };
}"""

CONDITIONS = ("cold", "warm", "throttled")


def _nav(ctx, url, mode, disable_cache):
    """Single navigation in an existing context; returns collected metrics."""
    page = ctx.new_page()
    client = ctx.new_cdp_session(page)
    client.send("Network.enable")
    client.send("Network.setCacheDisabled", {"cacheDisabled": disable_cache})
    if mode == "throttled":
        client.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 150,
            "downloadThroughput": int(1.6 * 1024 * 1024 / 8),
            "uploadThroughput": int(750 * 1024 / 8)})
        client.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    page.add_init_script(OBSERVER)
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(2500)
    data = page.evaluate(COLLECT)
    page.close()
    return data


def _run_condition(pw, url, mode, runs, storage_state):
    """One browser per condition. cold/throttled clear cache each run; warm
    primes the cache once, then measures genuine warm loads."""
    browser = pw.chromium.launch(headless=True)
    samples, seen = [], set()
    try:
        if mode == "warm":
            ctx = browser.new_context(storage_state=storage_state)
            _nav(ctx, url, mode, disable_cache=False)  # prime cache, discard
            for _ in range(runs):
                d = _nav(ctx, url, mode, disable_cache=False)
                samples.append(d); seen.update(d.get("icons", []))
            ctx.close()
        else:
            for _ in range(runs):
                ctx = browser.new_context(storage_state=storage_state)
                ctx.clear_cookies()
                cdp = ctx.new_cdp_session(ctx.new_page())
                cdp.send("Network.enable")
                cdp.send("Network.clearBrowserCache")
                d = _nav(ctx, url, mode, disable_cache=True)
                samples.append(d); seen.update(d.get("icons", []))
                ctx.close()
    finally:
        browser.close()
    return samples, seen


def _median_of(runs, key):
    return round(statistics.median(r[key] for r in runs))


def probe(url, runs=3, storage_state=None):
    out = {"url": url, "runs": runs, "conditions": {}, "icons_seen": set()}
    with sync_playwright() as pw:
        for mode in CONDITIONS:
            try:
                samples, seen = _run_condition(pw, url, mode, runs, storage_state)
                out["icons_seen"].update(seen)
            except Exception as e:  # noqa: BLE001
                samples = [{"error": str(e)}]
            ok = [s for s in samples if "error" not in s]
            if not ok:
                out["conditions"][mode] = {"error": samples[0].get("error")}
                continue
            metrics = {k: _median_of(ok, k) for k in
                       ("ttfb", "fcp", "lcp", "dcl", "load", "tbt",
                        "longtask_count", "request_count", "transfer", "decoded")}
            metrics["cls"] = round(statistics.median(s["cls"] for s in ok), 3)
            metrics["slowest"] = ok[0]["resources"]
            out["conditions"][mode] = metrics
    out["icons_seen"] = sorted(out["icons_seen"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="https://plan.taikunai.com/")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--storage-state", default=None)
    a = ap.parse_args()
    result = probe(a.url, a.runs, a.storage_state)
    for mode, m in result["conditions"].items():
        if "error" in m:
            print(f"[{mode}] ERROR: {m['error']}"); continue
        print(f"[{mode:9}] LCP={m['lcp']:>5}ms FCP={m['fcp']:>5}ms "
              f"load={m['load']:>6}ms TBT={m['tbt']:>4}ms "
              f"req={m['request_count']:>2} xfer={m['transfer']//1024:>4}KB")
    print(f"icons rendered in DOM: {len(result['icons_seen'])}")
    print(json.dumps(result))
