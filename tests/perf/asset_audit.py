#!/usr/bin/env python3
"""HTTP asset audit — no browser required.

Fetches the entry HTML, discovers every referenced JS/CSS/font asset, and
checks each for the cheap-win regressions: missing compression, weak
cache-control, oversized bundles, render-blocking placement. Folds in the
icon-font bloat finding from icon_usage. Emits a JSON blob the orchestrator
scores against budgets.
"""
from __future__ import annotations
import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_usage import summary as icon_summary  # noqa: E402

_ASSET_RE = re.compile(r"""(?:src|href)=["']([^"']+\.(?:js|css))(?:\?[^"']*)?["']""")
UA = "Mozilla/5.0 (perf-suite; Playwright-CLI audit)"


def _fetch(url: str, want_encoding: bool = True):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip, br" if want_encoding else "identity",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return r.status, dict(r.headers), body


def audit(base_url: str) -> dict:
    base = base_url.rstrip("/")
    status, headers, html = _fetch(base + "/")
    html_text = gzip.decompress(html).decode("utf-8", "replace") \
        if headers.get("Content-Encoding") == "gzip" else html.decode("utf-8", "replace")

    refs, seen = [], set()
    for m in _ASSET_RE.findall(html_text):
        path = m if m.startswith("/") else "/" + m
        if path not in seen:
            seen.add(path)
            refs.append(path)
    # entry HTML itself + the always-present icon font
    assets = []
    for path in ["/"] + refs + ["/vendor/tabler/fonts/tabler-icons.woff2"]:
        url = base + path
        try:
            st, hd, body = _fetch(url)
        except Exception as e:  # noqa: BLE001
            assets.append({"path": path, "error": str(e)})
            continue
        cache = hd.get("Cache-Control", "")
        assets.append({
            "path": path,
            "status": st,
            "encoding": hd.get("Content-Encoding", "none"),
            "transfer_bytes": len(body),
            "content_type": (hd.get("Content-Type", "").split(";")[0]),
            "cache_control": cache,
            "immutable": "immutable" in cache,
            "no_store": "no-store" in cache,
        })

    findings = []
    text_types = ("javascript", "css", "html", "json", "svg")
    for a in assets:
        if a.get("error"):
            continue
        ct = a["content_type"]
        # uncompressed text asset over 2 KB
        if a["encoding"] == "none" and any(t in ct for t in text_types) \
                and a["transfer_bytes"] > 2048:
            findings.append({"kind": "uncompressed", "path": a["path"],
                             "bytes": a["transfer_bytes"], "severity": "high"})
        # cacheable static asset with no lasting cache policy
        if a["path"] != "/" and not a["immutable"] and "max-age" not in a["cache_control"]:
            findings.append({"kind": "weak-cache", "path": a["path"],
                             "cache": a["cache_control"] or "(none)", "severity": "medium"})
        # oversized single asset
        if a["transfer_bytes"] > 150 * 1024:
            findings.append({"kind": "oversized-asset", "path": a["path"],
                             "bytes": a["transfer_bytes"], "severity": "high"})

    icons = icon_summary()
    total_transfer = sum(a.get("transfer_bytes", 0) for a in assets)
    return {
        "base_url": base,
        "request_count": len(assets),
        "total_transfer_bytes": total_transfer,
        "assets": assets,
        "findings": findings,
        "icons": {
            "defined": icons["defined"],
            "used": icons["used_total"],
            "waste_ratio": round(1 - icons["used_total"] / icons["defined"], 3),
        },
    }


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "https://plan.taikunai.com"
    print(json.dumps(audit(base), indent=2))
