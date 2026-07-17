#!/usr/bin/env python3
"""Regression: an Auth-owned browser session must still open the board."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api import browser_session  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class AuthOwner(BaseHTTPRequestHandler):
    seen_cookie = ""

    def do_GET(self):  # noqa: N802
        type(self).seen_cookie = self.headers.get("Cookie", "")
        body = json.dumps({"authenticated": True, "user": {
            "id": "user-session-fallback", "email": "board@example.com",
        }}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


server = ThreadingHTTPServer(("127.0.0.1", 0), AuthOwner)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
old_primary = os.environ.get("PM_AUTH_HTTP_PRIMARY")
old_url = os.environ.get("PM_AUTH_SESSION_URL")

try:
    os.environ["PM_AUTH_HTTP_PRIMARY"] = "service"
    os.environ["PM_AUTH_SESSION_URL"] = (
        f"http://127.0.0.1:{server.server_port}/api/auth/session")
    user = browser_session._user_from_auth_owner("fresh-browser-cookie")
    ok(user and user.get("id") == "user-session-fallback",
       "loopback Auth owner resolves a freshly issued browser session")
    ok(AuthOwner.seen_cookie == "taikun_session=fresh-browser-cookie",
       "fallback forwards only the named browser session cookie")

    ok(browser_session._user_from_auth_owner("bad;cookie") is None,
       "malformed cookie input never reaches the Auth service")

    os.environ["PM_AUTH_SESSION_URL"] = "https://auth.example.com/api/auth/session"
    ok(browser_session._user_from_auth_owner("fresh-browser-cookie") is None,
       "non-loopback Auth URL is rejected before a browser cookie can leave the host")

    os.environ["PM_AUTH_HTTP_PRIMARY"] = ""
    ok(browser_session._user_from_auth_owner("fresh-browser-cookie") is None,
       "fallback stays disabled when Auth HTTP is not service-owned")
finally:
    if old_primary is None:
        os.environ.pop("PM_AUTH_HTTP_PRIMARY", None)
    else:
        os.environ["PM_AUTH_HTTP_PRIMARY"] = old_primary
    if old_url is None:
        os.environ.pop("PM_AUTH_SESSION_URL", None)
    else:
        os.environ["PM_AUTH_SESSION_URL"] = old_url
    server.shutdown()
    server.server_close()

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
