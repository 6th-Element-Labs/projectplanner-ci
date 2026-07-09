#!/usr/bin/env python3
"""HARDEN-44 — hermetic tests for scripts/uptime_probe.py.

Script-style (matches the rest of the suite: run directly, exit non-zero on any
failure). No network: a local http.server stands in for plan.taikunai.com so the
real probe code paths (warm-connection /health burst, login round-trip, cookie
handling, latency budget, simulated outage) are exercised offline in CI.
"""
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "uptime_probe",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "uptime_probe.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


# --- pure logic -------------------------------------------------------------

ok(probe.percentile([0.1], 95) == 0.1, "p95 of a single sample is that sample")
ok(probe.percentile([1, 2, 3, 4, 5], 95) == 5, "p95 of 5 samples is the worst (nearest-rank)")
ok(probe.percentile([1, 2, 3, 4, 5], 50) == 3, "p50 of 5 samples is the median")
ok(probe.percentile([5, 1, 3, 2, 4], 95) == 5, "percentile sorts unsorted input")
ok(raises(lambda: probe.percentile([], 95), ValueError), "percentile of empty set raises")

ok(probe.evaluate([{"ok": True, "reasons": []}, {"ok": True, "reasons": []}])["ok"] is True,
   "evaluate is ok when every check passes")
ok(probe.evaluate([{"ok": False, "reasons": ["p95 3.1s over 2.0s budget"]}])["ok"] is False,
   "evaluate fails and surfaces a check's reasons")
_sim = probe.evaluate([{"ok": True, "reasons": []}], simulate_outage=True)
ok(_sim["ok"] is False and any("SIMULATED OUTAGE" in r for r in _sim["reasons"]),
   "simulate_outage forces a failure even when checks pass")


# --- end-to-end against a local fake server ---------------------------------

class Fake(BaseHTTPRequestHandler):
    """Emulates the endpoints the probe hits. Behaviour driven by class flags."""
    protocol_version = "HTTP/1.1"   # keep-alive, so the probe reuses one socket
    health_status = 200
    health_delay = 0.0
    login_status = 200
    set_cookie = True
    authenticated = True
    auth_raw = None   # when set, /session returns this exact JSON value for "authenticated"

    def log_message(self, *a):  # silence test noise
        pass

    def _send(self, status, body, cookie=""):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))  # required for keep-alive
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if self.health_delay:
                time.sleep(self.health_delay)
            self._send(self.health_status, b'{"status":"ok"}')
        elif self.path == "/api/auth/session":
            if Fake.auth_raw is not None:
                authed = Fake.auth_raw
            else:
                authed = self.authenticated and "taikun_session=" in self.headers.get("Cookie", "")
            self._send(200, json.dumps({"authenticated": authed}).encode())
        else:
            self._send(404, b'{}')

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/auth/login":
            cookie = "taikun_session=faketoken; Path=/; HttpOnly" if self.set_cookie else ""
            self._send(self.login_status, b'{"user":{"email":"x"}}', cookie=cookie)
        else:
            self._send(404, b'{}')


def reset():
    for k, v in dict(health_status=200, health_delay=0.0, login_status=200,
                     set_cookie=True, authenticated=True, auth_raw=None).items():
        setattr(Fake, k, v)


def run(base, **overrides):
    env = {"PROBE_BASE_URL": base, "PROBE_HEALTH_SAMPLES": "3",
           "PROBE_TIMEOUT_S": "5", "PROBE_LATENCY_BUDGET_S": "2.0",
           "PROBE_EMAIL": "atlas@example.com", "PROBE_PASSWORD": "pw"}
    env.update(overrides)
    return probe.run(env)


_srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_srv.server_address[1]}"

try:
    reset()
    v = run(BASE)
    checks = {c["check"]: c for c in v["checks"]}
    ok(v["ok"] is True, "all-healthy server -> probe passes")
    ok(checks["health"]["ok"] and checks["login"]["ok"], "both checks pass when healthy")
    ok(checks["login"]["authenticated"] is True, "login round-trip resolves authenticated")

    reset(); Fake.health_status = 503
    v = run(BASE)
    health = [c for c in v["checks"] if c["check"] == "health"][0]
    ok(v["ok"] is False and any("/health request(s) failed" in r for r in v["reasons"]),
       "a 5xx /health is reported as down")
    ok(health["p95_s"] is None and health["ok_samples"] == 0,
       "fast 5xx responses do not enter the latency stats (p95 is None, not a made-up value)")

    reset(); Fake.health_delay = 0.25
    v = run(BASE, PROBE_LATENCY_BUDGET_S="0.1")
    ok(v["ok"] is False and any("p95" in r for r in v["reasons"]),
       "/health latency over budget fails on p95")

    reset(); Fake.login_status = 401
    v = run(BASE)
    ok(v["ok"] is False and any("login HTTP 401" in r for r in v["reasons"]),
       "a 401 login is reported as a failure")
    ok(not any("over" in r and "budget" in r for r in v["reasons"]),
       "a failed login leg does not also emit a bogus latency-budget reason")

    reset(); Fake.set_cookie = False
    v = run(BASE)
    ok(v["ok"] is False and any("cookie" in r for r in v["reasons"]),
       "login without a session cookie fails")

    reset(); Fake.authenticated = False
    v = run(BASE)
    ok(v["ok"] is False and any("authenticated" in r for r in v["reasons"]),
       "a session that doesn't resolve to authenticated fails")

    reset(); Fake.auth_raw = "true"   # truthy STRING, not JSON boolean true
    v = run(BASE)
    login = [c for c in v["checks"] if c["check"] == "login"][0]
    ok(v["ok"] is False and login["authenticated"] is False,
       "a truthy non-boolean 'authenticated' value does NOT pass (strict is-True check)")

    reset()
    v = run(BASE, PROBE_EMAIL="", PROBE_PASSWORD="")
    login = [c for c in v["checks"] if c["check"] == "login"][0]
    ok(v["ok"] is True and login.get("skipped") is True,
       "login check is skipped (not failed) when creds are unset")

    reset()
    v = run(BASE, PROBE_SIMULATE_OUTAGE="1")
    ok(v["ok"] is False and any("SIMULATED OUTAGE" in r for r in v["reasons"]),
       "PROBE_SIMULATE_OUTAGE forces a failure against a healthy server")
finally:
    _srv.shutdown()

print(f"\nuptime probe: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
