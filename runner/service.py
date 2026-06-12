#!/usr/bin/env python3
"""Maxwell dispatch runner. POST /dispatch {task_id, brief} (Bearer token) -> runs Claude Code
headless in a claude/<task> branch, pushes it, emits the PR compare URL. GET /job/<id> for status.
Stdlib only; bind restricted by the security group + token."""
import json, os, subprocess, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = "/home/claude-runner/runner"
JOBS = os.path.join(HERE, "jobs"); os.makedirs(JOBS, exist_ok=True)
TOKEN = open("/home/claude-runner/.maxwell/dispatch_token").read().strip()


def run_job(job_id, task_id, brief):
    d = os.path.join(JOBS, job_id); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "brief"), "w").write(brief)
    open(os.path.join(d, "status"), "w").write("running")
    open(os.path.join(d, "task_id"), "w").write(task_id)
    try:
        subprocess.run([os.path.join(HERE, "run_task.sh"), task_id, os.path.join(d, "brief"), d],
                       timeout=3600)
    except Exception as e:
        open(os.path.join(d, "status"), "w").write("error: " + str(e)[:120])


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _auth(self):
        return self.headers.get("Authorization", "").replace("Bearer ", "").strip() == TOKEN

    def do_POST(self):
        if self.path != "/dispatch":
            return self._send(404, {"error": "not found"})
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        task_id = (body.get("task_id") or "task").strip()
        brief = body.get("brief") or ""
        if not brief:
            return self._send(400, {"error": "brief required"})
        job_id = uuid.uuid4().hex[:12]
        threading.Thread(target=run_job, args=(job_id, task_id, brief), daemon=True).start()
        self._send(200, {"job_id": job_id, "task_id": task_id, "status": "running",
                         "status_url": "/job/" + job_id})

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self.path.startswith("/job/"):
            return self._send(404, {"error": "not found"})
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        d = os.path.join(JOBS, os.path.basename(self.path))
        if not os.path.isdir(d):
            return self._send(404, {"error": "no such job"})
        rd = lambda f: (open(os.path.join(d, f)).read().strip()
                        if os.path.exists(os.path.join(d, f)) else None)
        self._send(200, {"status": rd("status"), "branch": rd("branch"), "pr_url": rd("pr_url"),
                         "task_id": rd("task_id"), "log_tail": (rd("claude.log") or "")[-2500:]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("RUNNER_PORT", "8130"))), H).serve_forever()
