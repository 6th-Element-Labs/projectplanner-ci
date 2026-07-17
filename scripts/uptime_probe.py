#!/usr/bin/env python3
"""HARDEN-44 — external off-box uptime + latency probe for plan.taikunai.com.

Runs OFF the box (GitHub Actions on the public projectplanner-ci sandbox, on a
schedule) so it keeps watching even when the box itself is dead — the exact gap
plan_uptime_recover.sh (a manual, on-box tool) cannot cover.

Two checks, both timed against a latency budget (default 2.0s):

  1. Liveness  — GET /health. The connection is warmed once (that handshake IS the
                 reachability signal), then a burst is timed over the warm socket so
                 the p95 reflects SERVER responsiveness, not the client's TLS/RTT
                 distance (path noise that would otherwise flap a distant runner).
  2. Login round-trip — POST /api/auth/login then GET /api/auth/session with the
                 returned cookie. Exercises the full web -> auth -> registry-DB path,
                 catching DB/auth outages a static /health can never see.

No third-party dependencies (stdlib only), so the workflow runs it with zero
`pip install`. Prints a JSON summary to stdout and exits non-zero on any failure;
the caller (workflow) turns a non-zero exit into a GitHub-native alert (open/refresh
an issue + a failed run that GitHub emails and pushes to mobile).

Config (all via env; sensible defaults):
  PROBE_BASE_URL          default https://plan.taikunai.com
  PROBE_EMAIL             login round-trip account (skip login check if unset)
  PROBE_PASSWORD          login round-trip password
  PROBE_LATENCY_BUDGET_S  default 2.0   (fail if p95 / a leg exceeds this)
  PROBE_HEALTH_SAMPLES    default 5     (burst size for the p95)
  PROBE_TIMEOUT_S         default 10.0  (per-request timeout)
  PROBE_SIMULATE_OUTAGE   set truthy to force a failure (proves alerting works)
"""
from __future__ import annotations

import http.client
import json
import math
import os
import ssl
import sys
import time
import urllib.parse
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = "https://plan.taikunai.com"
SESSION_COOKIE = "taikun_session"
USER_AGENT = "taikun-uptime-probe/1 (+HARDEN-44)"

# Build the TLS context once. Rebuilding it per request reloads the CA bundle from
# disk, adding hundreds of ms to every measurement.
_SSL_CTX = ssl.create_default_context()


def percentile(samples: List[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list (pct in [0, 100])."""
    if not samples:
        raise ValueError("percentile of empty sample set")
    ordered = sorted(samples)
    rank = math.ceil((pct / 100.0) * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "on", "yes")


class _Timed:
    """Result of one timed HTTP request. ok == HTTP 2xx."""

    def __init__(self, ok: bool, status: Optional[int], seconds: float,
                 error: str = "", set_cookie: str = "", body: bytes = b""):
        self.ok = ok
        self.status = status
        self.seconds = seconds
        self.error = error
        self.set_cookie = set_cookie
        self.body = body


class _Conn:
    """A reusable keep-alive HTTP(S) connection to one origin.

    Reusing one socket pays the TLS handshake once, so subsequent request timings
    reflect the SERVER's latency rather than the client's distance/TLS setup. On a
    dropped connection it reconnects once transparently, so a single blip does not
    page. Never raises for HTTP/transport errors — failures come back as _Timed.
    """

    def __init__(self, base_url: str, timeout: float):
        u = urllib.parse.urlparse(base_url)
        self.host = u.hostname or ""
        self.https = (u.scheme != "http")
        self.port = u.port or (443 if self.https else 80)
        self.timeout = timeout
        self._conn: Optional[http.client.HTTPConnection] = None

    def _connect(self) -> None:
        if self.https:
            self._conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=_SSL_CTX)
        else:
            self._conn = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout)

    def request(self, method: str, path: str,
                headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None) -> _Timed:
        hdrs = {"User-Agent": USER_AGENT, "Connection": "keep-alive"}
        if headers:
            hdrs.update(headers)
        last_err = ""
        elapsed = 0.0
        for attempt in range(2):
            start = time.monotonic()
            try:
                if self._conn is None:
                    self._connect()
                self._conn.request(method, path, body=body, headers=hdrs)
                resp = self._conn.getresponse()
                data = resp.read()  # must fully read to reuse the connection
                elapsed = time.monotonic() - start
                return _Timed(200 <= resp.status < 300, resp.status, elapsed,
                              set_cookie=_all_set_cookies(resp), body=data)
            except (http.client.HTTPException, OSError, ssl.SSLError, TimeoutError) as e:
                elapsed = time.monotonic() - start
                last_err = f"{type(e).__name__}: {e}"
                self.close()          # force a fresh socket on retry
                if attempt == 0:
                    time.sleep(0.4)
        # Report the real elapsed of the last attempt, not a fabricated timeout —
        # a failure must never inject a made-up latency into the p95/max stats.
        return _Timed(False, None, elapsed, error=last_err or "request failed")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def probe_health(base_url: str, *, samples: int, timeout: float,
                 budget_s: float) -> Dict[str, Any]:
    """Warm the connection, then time a GET /health burst over the warm socket.

    Fails on any non-200 (including the warm-up, which is the reachability signal)
    or when the burst p95 exceeds the budget.
    """
    conn = _Conn(base_url, timeout)
    path = "/health"
    failures: List[str] = []
    n = max(1, samples)

    warm = conn.request("GET", path)            # handshake here = liveness signal
    if not (warm.ok and warm.status == 200):
        failures.append(warm.error or f"HTTP {warm.status}")

    # Only successful responses feed the latency stats — a failed request's timing
    # is not a real measurement and would poison p95/max (the failure is already
    # counted as a hard failure below).
    ok_latencies: List[float] = []
    for _ in range(n):
        r = conn.request("GET", path)
        if r.ok and r.status == 200:
            ok_latencies.append(r.seconds)
        else:
            failures.append(r.error or f"HTTP {r.status}")
    conn.close()

    p95 = percentile(ok_latencies, 95) if ok_latencies else None
    reasons: List[str] = []
    if failures:
        reasons.append(f"{len(failures)} /health request(s) failed: {failures[0]}")
    if p95 is not None and p95 > budget_s:
        reasons.append(f"/health p95 {p95:.3f}s over {budget_s:.1f}s budget")
    return {
        "check": "health",
        "ok": not reasons,
        "url": base_url.rstrip("/") + path,
        "samples": n,
        "ok_samples": len(ok_latencies),
        "p95_s": round(p95, 3) if p95 is not None else None,
        "max_s": round(max(ok_latencies), 3) if ok_latencies else None,
        "min_s": round(min(ok_latencies), 3) if ok_latencies else None,
        "failures": failures,
        "reasons": reasons,
    }


def _all_set_cookies(resp: http.client.HTTPResponse) -> str:
    """Every Set-Cookie response header, newline-joined. http.client's getheader()
    comma-folds repeated headers, which corrupts cookies whose values/attributes
    contain commas (e.g. Expires dates); get_all() keeps them separate."""
    return "\n".join(resp.msg.get_all("Set-Cookie") or [])


def _cookie_value(set_cookie_header: str, name: str) -> str:
    """Extract one cookie's value, parsing each Set-Cookie line independently so a
    multi-cookie response can't hide the session morsel behind another cookie."""
    for line in (set_cookie_header or "").split("\n"):
        if not line:
            continue
        jar = SimpleCookie()
        try:
            jar.load(line)
        except Exception:
            continue
        morsel = jar.get(name)
        if morsel:
            return morsel.value
    return ""


def probe_login(base_url: str, email: str, password: str, *, timeout: float,
                budget_s: float) -> Dict[str, Any]:
    """POST /api/auth/login then GET /api/auth/session with the cookie.

    Fails if login is non-200, no session cookie is set, the session doesn't
    resolve to authenticated, or either leg exceeds the latency budget. The
    budget is applied per leg over a warmed connection, so we measure the
    server's auth/DB latency rather than the TLS handshake.
    """
    conn = _Conn(base_url, timeout)
    reasons: List[str] = []
    conn.request("GET", "/health")              # warm-up: pay the handshake first

    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    login = conn.request("POST", "/api/auth/login",
                         headers={"Content-Type": "application/json"}, body=payload)
    login_s = login.seconds
    login_ok = login.ok and login.status == 200
    if not login_ok:
        reasons.append(f"login {login.error or 'HTTP ' + str(login.status)}")

    token = _cookie_value(login.set_cookie, SESSION_COOKIE)
    if login_ok and not token:
        reasons.append(f"login set no {SESSION_COOKIE} cookie")

    session_s = 0.0
    session_ok = False
    authenticated = False
    if token:
        sess = conn.request("GET", "/api/auth/session",
                            headers={"Cookie": f"{SESSION_COOKIE}={token}"})
        session_s = sess.seconds
        session_ok = sess.ok and sess.status == 200
        if session_ok:
            try:
                # Strict identity check: only a real JSON `true` counts as authed —
                # a truthy string/number must not pass the core auth assertion.
                authenticated = json.loads(sess.body or b"{}").get("authenticated") is True
            except Exception:
                authenticated = False
            if not authenticated:
                reasons.append("session did not resolve to authenticated")
        else:
            reasons.append(f"session {sess.error or 'HTTP ' + str(sess.status)}")
    conn.close()

    # Only hold a leg to the latency budget if it actually succeeded — otherwise a
    # failed leg (already reported) would double-count as a bogus latency reason.
    if login_ok and login_s > budget_s:
        reasons.append(f"login {login_s:.3f}s over {budget_s:.1f}s budget")
    if session_ok and session_s > budget_s:
        reasons.append(f"session {session_s:.3f}s over {budget_s:.1f}s budget")

    return {
        "check": "login",
        "ok": not reasons and authenticated,
        "url": base_url.rstrip("/") + "/api/auth/login",
        "authenticated": authenticated,
        "login_s": round(login_s, 3),
        "session_s": round(session_s, 3),
        "roundtrip_s": round(login_s + session_s, 3),
        "reasons": reasons,
    }


def probe_project_read_journey(base_url: str, email: str, password: str,
                               project: str, deliverable_id: str, *, timeout: float,
                               budget_s: float) -> Dict[str, Any]:
    """Exercise least-privilege browser reads across cut-out service ownership."""
    conn = _Conn(base_url, timeout)
    reasons: List[str] = []
    conn.request("GET", "/health")
    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    login = conn.request("POST", "/api/auth/login",
                         headers={"Content-Type": "application/json"}, body=payload)
    token = _cookie_value(login.set_cookie, SESSION_COOKIE)
    if not (login.ok and token):
        reasons.append("least-privilege journey login failed")
    rows: List[Dict[str, Any]] = []
    if token:
        headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
        paths = [
            "/api/projects",
            f"/api/board?project={urllib.parse.quote(project)}",
            f"/api/deliverables?project={urllib.parse.quote(project)}",
        ]
        if deliverable_id:
            paths.append(
                f"/api/deliverables/{urllib.parse.quote(deliverable_id)}/mission_status"
                f"?project={urllib.parse.quote(project)}"
            )
        for path in paths:
            result = conn.request("GET", path, headers=headers)
            rows.append({"path": path, "status": result.status,
                         "seconds": round(result.seconds, 3)})
            if not result.ok:
                reasons.append(f"{path} returned HTTP {result.status}")
            elif result.seconds > budget_s:
                reasons.append(f"{path} {result.seconds:.3f}s over {budget_s:.1f}s budget")
    conn.close()
    return {"check": "least_privilege_project_read", "ok": not reasons,
            "project": project, "journey": rows, "reasons": reasons}


def evaluate(checks: List[Dict[str, Any]], *, simulate_outage: bool = False) -> Dict[str, Any]:
    """Fold per-check results into an overall verdict."""
    reasons: List[str] = []
    for c in checks:
        reasons.extend(c.get("reasons", []))
    if simulate_outage:
        reasons.append("SIMULATED OUTAGE (PROBE_SIMULATE_OUTAGE set) — alerting drill")
    return {"ok": not reasons, "reasons": reasons, "checks": checks}


def run(env: Dict[str, str]) -> Dict[str, Any]:
    base_url = (env.get("PROBE_BASE_URL") or DEFAULT_BASE_URL).strip()
    budget_s = float(env.get("PROBE_LATENCY_BUDGET_S") or 2.0)
    samples = int(env.get("PROBE_HEALTH_SAMPLES") or 5)
    timeout = float(env.get("PROBE_TIMEOUT_S") or 10.0)
    email = (env.get("PROBE_EMAIL") or "").strip()
    password = env.get("PROBE_PASSWORD") or ""
    project = (env.get("PROBE_PROJECT_ID") or "").strip()
    deliverable_id = (env.get("PROBE_DELIVERABLE_ID") or "").strip()
    simulate = _truthy(env.get("PROBE_SIMULATE_OUTAGE"))

    checks: List[Dict[str, Any]] = [
        probe_health(base_url, samples=samples, timeout=timeout, budget_s=budget_s)
    ]
    if email and password:
        checks.append(probe_login(base_url, email, password, timeout=timeout,
                                  budget_s=budget_s))
        if project:
            checks.append(probe_project_read_journey(
                base_url, email, password, project, deliverable_id,
                timeout=timeout, budget_s=budget_s))
        else:
            checks.append({"check": "least_privilege_project_read", "ok": True,
                           "skipped": True, "reasons": [],
                           "note": "PROBE_PROJECT_ID unset; activation is ARCH-MS-113"})
    else:
        checks.append({"check": "login", "ok": True, "skipped": True,
                       "reasons": [], "note": "PROBE_EMAIL/PROBE_PASSWORD unset"})

    verdict = evaluate(checks, simulate_outage=simulate)
    verdict.update({"target": base_url, "budget_s": budget_s})
    return verdict


def _human(verdict: Dict[str, Any]) -> str:
    lines = [f"target: {verdict['target']}  budget: {verdict['budget_s']:.1f}s"]
    for c in verdict["checks"]:
        if c.get("skipped"):
            lines.append(f"  [skip] {c['check']}: {c.get('note', '')}")
            continue
        tag = "PASS" if c["ok"] else "FAIL"
        if c["check"] == "health":
            p95 = c["p95_s"] if c["p95_s"] is not None else "n/a"
            lines.append(f"  [{tag}] health: p95={p95}s max={c['max_s']}s "
                         f"({c['ok_samples']}/{c['samples']} ok)")
        elif c["check"] == "login":
            lines.append(f"  [{tag}] login: roundtrip={c['roundtrip_s']}s "
                         f"authenticated={c.get('authenticated')}")
        elif c["check"] == "least_privilege_project_read":
            lines.append(f"  [{tag}] least-privilege project read: "
                         f"{len(c.get('journey') or [])} endpoint(s)")
    if not verdict["ok"]:
        lines.append("REASONS:")
        lines.extend(f"  - {r}" for r in verdict["reasons"])
    return "\n".join(lines)


def main() -> int:
    verdict = run(dict(os.environ))
    print(json.dumps(verdict, indent=2, sort_keys=True))
    print("\n" + _human(verdict), file=sys.stderr)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
