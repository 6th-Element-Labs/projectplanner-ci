#!/usr/bin/env python3
"""BUG-70: effective sibling routing and authenticated edge proof."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
pattern_match = re.search(
    r"@tasks_sibling path_regexp tasks_sibling (\^/api/tasks/[^\n]+\$)",
    caddyfile,
)
ok(pattern_match is not None, "Caddy uses an anchored named regexp for sibling BCs")
pattern = pattern_match.group(1) if pattern_match else r"a^"
for path in (
    "/api/tasks/ARCH-MS-102/dispatch",
    "/api/tasks/ARCH-MS-102/dispatch/latest",
    "/api/tasks/ARCH-MS-102/chat",
    "/api/tasks/ARCH-MS-102/review_verdict",
    "/api/tasks/ARCH-MS-102/review_findings/f-1/resolution",
):
    ok(re.fullmatch(pattern, path) is not None, f"sibling matcher owns {path}")
ok(re.fullmatch(pattern, "/api/tasks/ARCH-MS-102") is None,
   "plain task CRUD stays outside the sibling matcher")
ok("handle /api/tasks/*/dispatch*" not in caddyfile,
   "unreliable multi-wildcard dispatch matcher is removed")

caddy_binary = shutil.which("caddy")
docker_binary = shutil.which("docker") if os.environ.get("CI") else None
ok(bool(caddy_binary or docker_binary),
   "merge-gating CI can execute a real Caddy process")
if caddy_binary or docker_binary:
    caddy_image = "caddy:2.10.2-alpine"
    if not caddy_binary:
        pulled = subprocess.run(
            [docker_binary, "pull", caddy_image],
            text=True, capture_output=True, check=False, timeout=180,
        )
        ok(pulled.returncode == 0, "CI pulled the pinned Caddy image")
    adapt_command = (
        [caddy_binary, "adapt", "--adapter", "caddyfile", "--config", "deploy/Caddyfile"]
        if caddy_binary else
        [docker_binary, "run", "--rm", "-v", f"{ROOT}:/workspace:ro", "-w", "/workspace",
         caddy_image, "caddy", "adapt", "--adapter", "caddyfile", "--config", "deploy/Caddyfile"]
    )
    adapted = subprocess.run(
        adapt_command,
        cwd=ROOT, text=True, capture_output=True, check=False, timeout=20,
    )
    ok(adapted.returncode == 0, "Caddy accepts the named sibling matcher")
    try:
        json.loads(adapted.stdout)
        valid_json = True
    except json.JSONDecodeError:
        valid_json = False
    ok(valid_json, "Caddy adaptation emits valid JSON")

    class IdentityHandler(BaseHTTPRequestHandler):
        identity = ""

        def _reply(self) -> None:
            body = self.identity.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_: object) -> None:
            return

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    mono_port, tasks_port, edge_port = free_port(), free_port(), free_port()
    mono_handler = type("MonolithHandler", (IdentityHandler,), {"identity": "monolith"})
    tasks_handler = type("TasksHandler", (IdentityHandler,), {"identity": "tasks"})
    mono_server = ThreadingHTTPServer(("127.0.0.1", mono_port), mono_handler)
    tasks_server = ThreadingHTTPServer(("127.0.0.1", tasks_port), tasks_handler)
    threads = [
        threading.Thread(target=mono_server.serve_forever, daemon=True),
        threading.Thread(target=tasks_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    with tempfile.TemporaryDirectory(prefix="bug70-caddy-") as tmp:
        config = Path(tmp) / "Caddyfile"
        cidfile = Path(tmp) / "caddy.cid"
        config.write_text(
            f"""http://127.0.0.1:{edge_port} {{
    @tasks_sibling path_regexp tasks_sibling ^/api/tasks/[^/]+/(dispatch|chat|review_[^/]+)(/.*)?$
    handle @tasks_sibling {{
        reverse_proxy 127.0.0.1:{mono_port}
    }}
    handle /api/tasks* {{
        reverse_proxy 127.0.0.1:{tasks_port}
    }}
}}
""",
            encoding="utf-8",
        )
        caddy_command = (
            [caddy_binary, "run", "--adapter", "caddyfile", "--config", str(config)]
            if caddy_binary else
            [docker_binary, "run", "--rm", "--cidfile", str(cidfile),
             "--network", "host", "-v",
             f"{config}:/etc/caddy/Caddyfile:ro", caddy_image,
             "caddy", "run", "--adapter", "caddyfile", "--config", "/etc/caddy/Caddyfile"]
        )
        caddy = subprocess.Popen(
            caddy_command,
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{edge_port}"
            for _ in range(40):
                try:
                    urllib.request.urlopen(base + "/api/tasks/T-1", timeout=0.2).read()
                    break
                except OSError:
                    time.sleep(0.05)

            def routed(path: str, method: str = "GET") -> str:
                request = urllib.request.Request(base + path, data=b"{}" if method == "POST" else None,
                                                 method=method)
                return urllib.request.urlopen(request, timeout=2).read().decode("utf-8")

            ok(routed("/api/tasks/T-1") == "tasks",
               "real Caddy routes plain Tasks CRUD to :8122 backend")
            ok(routed("/api/tasks/T-1/dispatch/latest") == "monolith",
               "real Caddy routes nested dispatch to monolith")
            ok(routed("/api/tasks/T-1/chat", "POST") == "monolith",
               "real Caddy routes chat to monolith")
            ok(routed("/api/tasks/T-1/review_findings/f-1/resolution") == "monolith",
               "real Caddy routes nested review paths to monolith")
        finally:
            if not caddy_binary and cidfile.is_file():
                subprocess.run(
                    [docker_binary, "stop", cidfile.read_text(encoding="utf-8").strip()],
                    check=False, capture_output=True, timeout=10,
                )
            else:
                caddy.terminate()
            caddy.wait(timeout=10)
            mono_server.shutdown()
            tasks_server.shutdown()

spec = importlib.util.spec_from_file_location(
    "verify_runtime_deploy", ROOT / "scripts" / "verify_runtime_deploy.py"
)
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify
spec.loader.exec_module(verify)


def fingerprint_for(port: int, status: int, digest: str) -> dict[str, object]:
    return {"url": f"http://127.0.0.1:{port}", "method": "GET",
            "http_status": status, "body_sha256": digest,
            "body_semantic_sha256": digest}


volatile_edge = json.dumps({
    "task_id": "T-1", "session_health": {"status": "healthy", "checked_at": 1.0},
}).encode()
volatile_owner = json.dumps({
    "session_health": {"checked_at": 2.0, "status": "healthy"}, "task_id": "T-1",
}).encode()
changed_owner = json.dumps({
    "session_health": {"checked_at": 2.0, "status": "unsafe"}, "task_id": "T-1",
}).encode()
ok(verify.semantic_body_sha256(volatile_edge) == verify.semantic_body_sha256(volatile_owner),
   "semantic fingerprint ignores only volatile checked_at values and JSON ordering")
ok(verify.semantic_body_sha256(volatile_edge) != verify.semantic_body_sha256(changed_owner),
   "semantic fingerprint still rejects non-volatile body changes")
ok(verify.semantic_body_sha256(b"not-json-a") != verify.semantic_body_sha256(b"not-json-b"),
   "semantic fingerprint retains exact byte comparison for non-JSON bodies")
ok(verify.semantic_body_sha256(b"NaN") != verify.semantic_body_sha256(b" NaN "),
   "semantic fingerprint treats non-standard JSON constants as byte-exact bodies")


def passing_probe(url: str, **_: object) -> dict[str, object]:
    if ":8122" in url:
        return fingerprint_for(8122, 404, "tasks-404")
    return fingerprint_for(8110, 200, "mono-200")


original = verify.http_fingerprint
verify.http_fingerprint = passing_probe
try:
    result = verify.check_live_route_owner(
        base_url="https://plan.example",
        path="/api/tasks/T-1/dispatch/latest?project=switchboard",
        method="GET", token="secret", owner_port=8110, other_port=8122,
    )
    ok(result.ok, "authenticated edge proof accepts the monolith owner")

    def falling_through(url: str, **_: object) -> dict[str, object]:
        if ":8110" in url:
            return fingerprint_for(8110, 200, "mono-200")
        return fingerprint_for(8122, 404, "tasks-404")

    verify.http_fingerprint = falling_through
    result = verify.check_live_route_owner(
        base_url="https://plan.example",
        path="/api/tasks/T-1/dispatch/latest?project=switchboard",
        method="GET", token="secret", owner_port=8110, other_port=8122,
    )
    ok(not result.ok, "authenticated edge proof rejects the BUG-70 fallthrough")

    def same_status_wrong_body(url: str, **_: object) -> dict[str, object]:
        if ":8110" in url:
            return fingerprint_for(8110, 200, "mono-200")
        if ":8122" in url:
            return fingerprint_for(8122, 404, "tasks-404")
        return fingerprint_for(443, 200, "unrelated-edge-body")

    verify.http_fingerprint = same_status_wrong_body
    result = verify.check_live_route_owner(
        base_url="https://plan.example",
        path="/api/tasks/T-1/dispatch/latest?project=switchboard",
        method="GET", token="secret", owner_port=8110, other_port=8122,
    )
    ok(not result.ok,
       "authenticated edge proof rejects same-status wrong-body false positives")
finally:
    verify.http_fingerprint = original

redeploy = (ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8")
ok("PM_RUNTIME_PROOF_TOKEN" in redeploy and "--probe-task-id" in redeploy,
   "redeploy makes authenticated owner probes mandatory")
ok("PM_MCP_TOKEN" in redeploy and "unavailable for authenticated edge proof" in redeploy,
   "redeploy fails closed when the proof credential is unavailable")
ok('RUNTIME_PROOF_TASK_ID:-}' in redeploy,
   "redeploy discovers a current probe task unless an operator configures one")
snapshot_pos = redeploy.find('ROLLBACK_DIR="$(mktemp -d')
sync_pos = redeploy.find('bash "$ROOT/deploy/sync_caddy_fail_closed.sh"')
proof_pos = redeploy.find('"$PYTHON" "$ROOT/scripts/verify_runtime_deploy.py"')
rollback_pos = redeploy.find('fail_runtime_proof "authenticated runtime proof failed"')
ok(-1 < snapshot_pos < sync_pos < proof_pos < rollback_pos,
   "redeploy snapshots topology before cut and restores it on proof failure")
ok("restart projectplanner" in redeploy
   and 'cp "$ROLLBACK_DIR/Caddyfile" "$CADDY_LIVE"' in redeploy
   and "TASKS_WAS_ACTIVE" in redeploy and "TASKS_WAS_ENABLED" in redeploy,
   "rollback restores monolith mode, Caddy, and Tasks unit state")
arm_pos = redeploy.find("rollback_guard_arm restore_tasks_cut_topology")
unit_mutation_pos = redeploy.find("sudo cp deploy/*.service")
disarm_pos = redeploy.find("rollback_guard_disarm")
ok(-1 < arm_pos < unit_mutation_pos < sync_pos < proof_pos < disarm_pos,
   "rollback transaction spans every topology mutation through runtime proof")

guard = ROOT / "deploy" / "redeploy_rollback_guard.sh"
restore_match = re.search(
    r"(?ms)^restore_tasks_cut_topology\(\) \{\n.*?^\}\n", redeploy,
)
ok(restore_match is not None,
   "rollback integration harness executes the production restore function")
with tempfile.TemporaryDirectory() as guard_tmp:
    guard_tmp_path = Path(guard_tmp)
    bin_dir = guard_tmp_path / "bin"
    fake_root = guard_tmp_path / "root"
    bin_dir.mkdir()
    (fake_root / "deploy").mkdir(parents=True)
    event_log = guard_tmp_path / "events"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'systemctl %s\\n' \"$*\" >> \"$EVENT_LOG\"\n"
        "if [ \"${FAIL_CADDY_ACTIVATION:-0}\" = 1 ] "
        "&& { [ \"$*\" = 'reload caddy' ] || [ \"$*\" = 'restart caddy' ]; }; "
        "then exit 1; fi\n"
        "exit 0\n", encoding="utf-8",
    )
    systemctl.chmod(0o755)
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'sudo %s\\n' \"$*\" >> \"$EVENT_LOG\"\n"
        "exec \"$@\"\n", encoding="utf-8",
    )
    sudo.chmod(0o755)
    cp = bin_dir / "cp"
    cp.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${FAIL_CADDY_COPY:-0}\" = 1 ] "
        "&& [ \"${1##*/}\" = Caddyfile ]; then exit 1; fi\n"
        "exec /bin/cp \"$@\"\n", encoding="utf-8",
    )
    cp.chmod(0o755)
    wait_for_health = fake_root / "deploy" / "wait-for-health.sh"
    wait_for_health.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'health %s\\n' \"$HEALTH_URL\" >> \"$EVENT_LOG\"\n"
        "if [ \"${FAIL_MONOLITH_HEALTH:-0}\" = 1 ] "
        "&& [ \"$HEALTH_URL\" = http://127.0.0.1:8110/health ]; then exit 1; fi\n",
        encoding="utf-8",
    )
    wait_for_health.chmod(0o755)

    for signal_line, expected_rc, label in (
        ("false", 1, "ordinary failure"),
        ("kill -INT $$", 130, "SIGINT"),
        ("kill -TERM $$", 143, "SIGTERM"),
    ):
        rollback_dir = guard_tmp_path / f"rollback-{expected_rc}"
        rollback_dir.mkdir()
        caddy_live = guard_tmp_path / f"Caddyfile-{expected_rc}"
        app_unit = guard_tmp_path / f"projectplanner-{expected_rc}.service"
        tasks_unit = guard_tmp_path / f"switchboard-tasks-{expected_rc}.service"
        for path, old in (
            (caddy_live, "old-caddy\n"),
            (app_unit, "old-app-unit\n"),
            (tasks_unit, "old-tasks-unit\n"),
        ):
            path.write_text(old, encoding="utf-8")
        for name, content in (
            ("Caddyfile", "old-caddy\n"),
            ("projectplanner.service", "old-app-unit\n"),
            ("switchboard-tasks.service", "old-tasks-unit\n"),
        ):
            (rollback_dir / name).write_text(content, encoding="utf-8")
            (rollback_dir / f"{name}.present").touch()
        event_log.write_text("", encoding="utf-8")
        function_source = restore_match.group(0) if restore_match else ""
        script = f'''set -euo pipefail
export PATH="{bin_dir}:$PATH"
export EVENT_LOG="{event_log}"
ROOT="{fake_root}"
ROLLBACK_DIR="{rollback_dir}"
CADDY_LIVE="{caddy_live}"
CADDY_UNIT="caddy"
PROJECTPLANNER_UNIT_LIVE="{app_unit}"
TASKS_UNIT_LIVE="{tasks_unit}"
TASKS_WAS_ACTIVE="active"
TASKS_WAS_ENABLED="enabled"
section() {{ printf 'section %s\\n' "$1" >> "$EVENT_LOG"; }}
{function_source}
cleanup() {{ printf 'cleanup\\n' >> "$EVENT_LOG"; rm -rf "$ROLLBACK_DIR"; }}
restore_instrumented() {{
    printf 'restore-start\\n' >> "$EVENT_LOG"
    local restore_rc=0
    restore_tasks_cut_topology || restore_rc=$?
    printf 'restore-complete\\n' >> "$EVENT_LOG"
    return "$restore_rc"
}}
source "{guard}"
rollback_guard_arm restore_instrumented cleanup
printf 'new-caddy\\n' > "$CADDY_LIVE"
printf 'new-app-unit\\n' > "$PROJECTPLANNER_UNIT_LIVE"
printf 'new-tasks-unit\\n' > "$TASKS_UNIT_LIVE"
{signal_line}
rollback_guard_disarm
'''
        completed = subprocess.run(
            ["bash", "-c", script], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        events = event_log.read_text(encoding="utf-8").splitlines()
        restored = (
            caddy_live.read_text(encoding="utf-8") == "old-caddy\n"
            and app_unit.read_text(encoding="utf-8") == "old-app-unit\n"
            and tasks_unit.read_text(encoding="utf-8") == "old-tasks-unit\n"
        )
        required_events = (
            "systemctl daemon-reload" in events
            and "systemctl restart projectplanner" in events
            and "systemctl reload caddy" in events
            and "systemctl restart switchboard-tasks" in events
            and "systemctl enable switchboard-tasks" in events
            and "health http://127.0.0.1:8110/health" in events
            and "health http://127.0.0.1:8122/health" in events
        )
        ordered = (
            "restore-start" in events and "restore-complete" in events
            and "cleanup" in events
            and events.index("restore-start")
            < events.index("health http://127.0.0.1:8110/health")
            < events.index("systemctl reload caddy")
            < events.index("systemctl restart switchboard-tasks")
            < events.index("health http://127.0.0.1:8122/health")
            < events.index("restore-complete")
            < events.index("cleanup")
        )
        ok(completed.returncode == expected_rc and restored
           and required_events and ordered and not rollback_dir.exists(),
           f"full topology rollback is exact and ordered on {label}")

    # If the restored monolith is unhealthy, the rollback must leave the current
    # edge and Tasks lifecycle alone instead of routing traffic into an outage.
    rollback_dir = guard_tmp_path / "rollback-unhealthy"
    rollback_dir.mkdir()
    caddy_live = guard_tmp_path / "Caddyfile-unhealthy"
    app_unit = guard_tmp_path / "projectplanner-unhealthy.service"
    tasks_unit = guard_tmp_path / "switchboard-tasks-unhealthy.service"
    for path, old in (
        (caddy_live, "old-caddy\n"),
        (app_unit, "old-app-unit\n"),
        (tasks_unit, "old-tasks-unit\n"),
    ):
        path.write_text(old, encoding="utf-8")
    for name, content in (
        ("Caddyfile", "old-caddy\n"),
        ("projectplanner.service", "old-app-unit\n"),
        ("switchboard-tasks.service", "old-tasks-unit\n"),
    ):
        (rollback_dir / name).write_text(content, encoding="utf-8")
        (rollback_dir / f"{name}.present").touch()
    event_log.write_text("", encoding="utf-8")
    function_source = restore_match.group(0) if restore_match else ""
    negative_script = f'''set -euo pipefail
export PATH="{bin_dir}:$PATH"
export EVENT_LOG="{event_log}"
export FAIL_MONOLITH_HEALTH=1
ROOT="{fake_root}"
ROLLBACK_DIR="{rollback_dir}"
CADDY_LIVE="{caddy_live}"
CADDY_UNIT="caddy"
PROJECTPLANNER_UNIT_LIVE="{app_unit}"
TASKS_UNIT_LIVE="{tasks_unit}"
TASKS_WAS_ACTIVE="inactive"
TASKS_WAS_ENABLED="disabled"
section() {{ printf 'section %s\\n' "$1" >> "$EVENT_LOG"; }}
{function_source}
cleanup() {{ printf 'cleanup\\n' >> "$EVENT_LOG"; rm -rf "$ROLLBACK_DIR"; }}
source "{guard}"
rollback_guard_arm restore_tasks_cut_topology cleanup
printf 'new-caddy\\n' > "$CADDY_LIVE"
printf 'new-app-unit\\n' > "$PROJECTPLANNER_UNIT_LIVE"
printf 'new-tasks-unit\\n' > "$TASKS_UNIT_LIVE"
false
rollback_guard_disarm
'''
    completed = subprocess.run(
        ["bash", "-c", negative_script], check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    events = event_log.read_text(encoding="utf-8").splitlines()
    ok(completed.returncode == 1
       and caddy_live.read_text(encoding="utf-8") == "new-caddy\n"
       and "systemctl reload caddy" not in events
       and "systemctl restart caddy" not in events
       and "systemctl stop switchboard-tasks" not in events
       and "systemctl disable switchboard-tasks" not in events
       and events[-1] == "cleanup" and not rollback_dir.exists(),
       "unhealthy restored monolith preserves current edge and Tasks lifecycle")

    # Edge restoration itself is a second safety gate. A missing snapshot, a
    # failed copy, or a failed reload plus restart must all leave the current
    # Tasks lifecycle untouched because the active edge may still need :8122.
    for label, extra_env, include_caddy_snapshot, expected_caddy in (
        ("missing Caddy snapshot", "", False, "new-caddy\n"),
        ("failed Caddy copy", "export FAIL_CADDY_COPY=1", True, "new-caddy\n"),
        ("failed Caddy reload and restart", "export FAIL_CADDY_ACTIVATION=1",
         True, "old-caddy\n"),
    ):
        slug = label.replace(" ", "-")
        rollback_dir = guard_tmp_path / f"rollback-{slug}"
        rollback_dir.mkdir()
        caddy_live = guard_tmp_path / f"Caddyfile-{slug}"
        app_unit = guard_tmp_path / f"projectplanner-{slug}.service"
        tasks_unit = guard_tmp_path / f"switchboard-tasks-{slug}.service"
        for path, content in (
            (caddy_live, "new-caddy\n"),
            (app_unit, "new-app-unit\n"),
            (tasks_unit, "new-tasks-unit\n"),
        ):
            path.write_text(content, encoding="utf-8")
        for name, content in (
            ("projectplanner.service", "old-app-unit\n"),
            ("switchboard-tasks.service", "old-tasks-unit\n"),
        ):
            (rollback_dir / name).write_text(content, encoding="utf-8")
            (rollback_dir / f"{name}.present").touch()
        if include_caddy_snapshot:
            (rollback_dir / "Caddyfile").write_text("old-caddy\n", encoding="utf-8")
            (rollback_dir / "Caddyfile.present").touch()
        event_log.write_text("", encoding="utf-8")
        edge_failure_script = f'''set -euo pipefail
export PATH="{bin_dir}:$PATH"
export EVENT_LOG="{event_log}"
{extra_env}
ROOT="{fake_root}"
ROLLBACK_DIR="{rollback_dir}"
CADDY_LIVE="{caddy_live}"
CADDY_UNIT="caddy"
PROJECTPLANNER_UNIT_LIVE="{app_unit}"
TASKS_UNIT_LIVE="{tasks_unit}"
TASKS_WAS_ACTIVE="inactive"
TASKS_WAS_ENABLED="disabled"
section() {{ printf 'section %s\\n' "$1" >> "$EVENT_LOG"; }}
{function_source}
cleanup() {{ printf 'cleanup\\n' >> "$EVENT_LOG"; rm -rf "$ROLLBACK_DIR"; }}
source "{guard}"
rollback_guard_arm restore_tasks_cut_topology cleanup
false
rollback_guard_disarm
'''
        completed = subprocess.run(
            ["bash", "-c", edge_failure_script], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        events = event_log.read_text(encoding="utf-8").splitlines()
        lifecycle_untouched = (
            "systemctl stop switchboard-tasks" not in events
            and "systemctl restart switchboard-tasks" not in events
            and "systemctl enable switchboard-tasks" not in events
            and "systemctl disable switchboard-tasks" not in events
        )
        ok(completed.returncode == 1 and lifecycle_untouched
           and caddy_live.read_text(encoding="utf-8") == expected_caddy
           and events[-1] == "cleanup" and not rollback_dir.exists(),
           f"{label} preserves current Tasks lifecycle")


class ProbeListResponse:
    status = 200

    def __enter__(self) -> "ProbeListResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    @staticmethod
    def read() -> bytes:
        return b'{"tasks":[{"task_id":"T-2"},{"task_id":"T-1"}]}'


original_urlopen = verify.urllib.request.urlopen
verify.urllib.request.urlopen = lambda *_args, **_kwargs: ProbeListResponse()
try:
    selected, selection = verify.resolve_probe_task_id(
        base_url="https://plan.example", token="secret",
    )
    ok(selection.ok and selected == "T-1",
       "runtime proof deterministically discovers an existing task")
    selected, selection = verify.resolve_probe_task_id(
        base_url="https://plan.example", token="secret", configured_task_id="missing",
    )
    ok(not selection.ok and not selected,
       "runtime proof fails closed for a configured task that does not exist")
finally:
    verify.urllib.request.urlopen = original_urlopen

source = (ROOT / "scripts" / "verify_runtime_deploy.py").read_text(encoding="utf-8")
ok('path=f"/api/tasks/{task}/chat{project_qs}", method="POST"' in source
   and 'json_body=b"{}"' in source,
   "chat ownership proof uses safely invalid non-mutating POST")

with tempfile.TemporaryDirectory(prefix="bug70-chat-apps-") as chat_tmp:
    chat_probe = r'''
import json, os
from pathlib import Path
tmp = Path(os.environ["BUG70_CHAT_TMP"])
os.environ.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "dev-open", "PM_JWT_SECRET": "bug70-test",
})
(tmp / "projects").mkdir(exist_ok=True)
import store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from switchboard.api import deps
from switchboard.api.routers import tasks as tasks_router
from switchboard.api.tasks_port_adapters import configure_tasks_ports, ensure_tasks_runtime
from switchboard.services.tasks import create_app
from switchboard.services.tasks.settings import TasksServiceSettings
store.init_db("switchboard")
created = store.create_task(
    {"workstream_id": "BUG70", "title": "chat probe"}, project="switchboard",
)
task_id = created.get("task_id") if isinstance(created, dict) else created
configure_tasks_ports(); ensure_tasks_runtime()
mono = FastAPI()
mono.include_router(tasks_router.create_router(
    resolve_project=deps.resolve_project, resolve_principal=deps.resolve_principal,
    sibling_bc_only=True,
))
cut = create_app(TasksServiceSettings(
    service_name="bug70-cut", host="127.0.0.1", port=8122,
))
before = len((store.get_task(task_id, project="switchboard") or {}).get("activity", []))
mono_response = TestClient(mono).post(
    f"/api/tasks/{task_id}/chat", params={"project": "switchboard"}, json={},
)
after = len((store.get_task(task_id, project="switchboard") or {}).get("activity", []))
cut_response = TestClient(cut).post(
    f"/api/tasks/{task_id}/chat", params={"project": "switchboard"}, json={},
)
print(json.dumps({
    "mono_status": mono_response.status_code, "mono_body": mono_response.text,
    "cut_status": cut_response.status_code, "cut_body": cut_response.text,
    "activity_unchanged": before == after,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", chat_probe], cwd=ROOT,
        env=dict(os.environ, BUG70_CHAT_TMP=chat_tmp),
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        chat_result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        chat_result = {}
    ok(completed.returncode == 0
       and chat_result.get("mono_status") == 400
       and chat_result.get("cut_status") == 404
       and chat_result.get("mono_body") != chat_result.get("cut_body")
       and chat_result.get("activity_unchanged") is True,
       "real monolith and Tasks apps distinguish empty chat POST without mutation "
       f"(result={chat_result}, stderr={completed.stderr[-300:]!r})")

print(f"\nBUG-70 Tasks edge routing: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
