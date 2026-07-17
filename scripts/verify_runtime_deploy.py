#!/usr/bin/env python3
"""Exact-SHA / runtime evidence check for process-cut deploys (ARCH-MS-101).

Reusable for subsequent service cuts. Emits ``switchboard.runtime_deploy.v1`` JSON
comparing canonical SHA, VM SHA, repo/live Caddy checksums, unit state, listener,
local health identity, and edge ownership from the live (or repo) Caddyfile.

Examples:
  python scripts/verify_runtime_deploy.py \\
    --canonical-sha "$(git rev-parse origin/master)" \\
    --service switchboard-tasks --port 8122 \\
    --edge-owns '/api/tasks*:8122'

  # Fixture / CI mode (no live systemd):
  python scripts/verify_runtime_deploy.py --fixture-json path/to/fixture.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA = "switchboard.runtime_deploy.v1"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def _run(cmd: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha(root: Path, ref: str = "HEAD") -> str | None:
    proc = _run(["git", "-C", str(root), "rev-parse", ref])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def unit_state(unit: str) -> dict[str, str]:
    active = _run(["systemctl", "is-active", unit]).stdout.strip() or "unknown"
    enabled = _run(["systemctl", "is-enabled", unit]).stdout.strip() or "unknown"
    return {"unit": unit, "active": active, "enabled": enabled}


def port_listening(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_health(
    url: str,
    *,
    expect_service: str | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url, "http_status": None, "body": None, "service": None}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            out["http_status"] = int(resp.status)
            raw = resp.read().decode("utf-8", errors="replace")
            out["body"] = raw
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                out["service"] = payload.get("service")
                out["status"] = payload.get("status")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        out["error"] = str(exc)
        return out
    if expect_service is not None:
        out["service_match"] = out.get("service") == expect_service
    return out


def semantic_body_sha256(body: bytes) -> str:
    """Hash response meaning while retaining volatile observation-field shape.

    Task detail includes ``session_health.checked_at``, which is generated for
    every request.  Edge and direct owner probes are necessarily separate HTTP
    requests, so byte hashes can differ even when they reached the same backend.
    Canonicalize JSON and replace only values named ``checked_at`` with a stable
    marker.  The key must still exist, and every other value remains part of the
    ownership fingerprint.  Non-JSON responses keep exact byte semantics.
    """
    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        payload = json.loads(body, parse_constant=reject_nonstandard_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return hashlib.sha256(body).hexdigest()

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("<volatile:checked_at>" if key == "checked_at" else stable(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    canonical = json.dumps(
        stable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def http_fingerprint(
    url: str,
    *,
    token: str = "",
    method: str = "GET",
    json_body: bytes | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    # An empty token means a genuinely anonymous request (no Authorization header) --
    # BUG-69's anon-read check needs that; every other caller here always passes a
    # real token, so this default only changes behavior for the new check below.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=json_body, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"url": url, "method": method, "error": str(exc)}
    return {
        "url": url,
        "method": method,
        "http_status": status,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_semantic_sha256": semantic_body_sha256(body),
    }


def check_live_route_owner(
    *,
    base_url: str,
    path: str,
    method: str,
    token: str,
    owner_port: int,
    other_port: int,
    json_body: bytes | None = None,
) -> CheckResult:
    edge = http_fingerprint(
        f"{base_url.rstrip('/')}{path}", token=token, method=method,
        json_body=json_body,
    )
    owner = http_fingerprint(
        f"http://127.0.0.1:{owner_port}{path}", token=token, method=method,
        json_body=json_body,
    )
    other = http_fingerprint(
        f"http://127.0.0.1:{other_port}{path}", token=token, method=method,
        json_body=json_body,
    )
    edge_status = edge.get("http_status")
    owner_status = owner.get("http_status")
    same_owner_body = (
        edge.get("body_semantic_sha256") == owner.get("body_semantic_sha256")
    )
    owner_distinguished = edge_status == owner_status and same_owner_body
    differs_from_other = (
        edge_status != other.get("http_status")
        or edge.get("body_semantic_sha256") != other.get("body_semantic_sha256")
    )
    ok = bool(edge_status is not None and owner_distinguished and differs_from_other)
    return CheckResult(
        name=f"live_edge:{method}:{path}->{owner_port}",
        ok=ok,
        detail={
            "path": path,
            "expected_owner_port": owner_port,
            "other_port": other_port,
            "edge": edge,
            "owner": owner,
            "other": other,
        },
        message=(
            f"authenticated edge probe reaches :{owner_port}"
            if ok else f"authenticated edge probe does not reach :{owner_port}"
        ),
    )


def check_anon_read_rejected(*, base_url: str, path: str) -> CheckResult:
    """BUG-69: an unauthenticated caller must never reach live project task data.

    The route-ownership checks above always send a bearer token -- they prove "the
    right service answers", not "an anonymous caller is rejected". Those are
    different guarantees, and BUG-69 shipped exactly that gap twice (2026-07-15,
    2026-07-17): the edge correctly routed /api/tasks* to :8122 (the ownership
    checks would have passed), while :8122 itself skipped the auth middleware and
    answered anonymous reads with 200 + live task data. This is the check that
    would have caught it before either incident reached prod.
    """
    probe = http_fingerprint(f"{base_url.rstrip('/')}{path}", method="GET")
    status = probe.get("http_status")
    ok = status == 401
    return CheckResult(
        name=f"live_edge:anon_rejected:{path}",
        ok=ok,
        detail={"path": path, "probe": probe},
        message=(
            "anonymous read is rejected (401)" if ok
            else f"anonymous read was NOT rejected (got {status}) -- BUG-69 regression"
        ),
    )


def resolve_probe_task_id(
    *, base_url: str, token: str, configured_task_id: str = "",
) -> tuple[str, CheckResult]:
    """Choose an existing task for read-only ownership probes.

    An explicit task remains supported for drills.  Normal redeploys discover a
    current task from the authenticated Tasks list so deploy liveness is not
    coupled forever to one board record.
    """
    path = "/api/tasks?project=switchboard"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    status: int | None = None
    payload: Any = None
    error = ""
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            status = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        error = f"HTTP {status}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        error = str(exc)

    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    candidates = sorted(
        str(task.get("task_id") or "").strip()
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict) and str(task.get("task_id") or "").strip()
    )
    configured = configured_task_id.strip()
    selected = configured if configured and configured in candidates else ""
    if not configured and candidates:
        selected = candidates[0]
    ok = bool(status == 200 and selected)
    if configured and not selected and not error:
        error = "configured probe task is not present in the authenticated task list"
    return selected, CheckResult(
        name="live_edge:probe_task",
        ok=ok,
        detail={
            "path": path,
            "http_status": status,
            "configured": bool(configured),
            "selected_task_id": selected or None,
            "candidate_count": len(candidates),
            "error": error or None,
        },
        message=(
            f"selected existing task {selected} for read-only ownership probes"
            if ok else "could not select an existing task for ownership probes"
        ),
    )


def resolve_probe_deliverable_id(
    *, base_url: str, token: str,
) -> tuple[str, CheckResult]:
    """Choose an existing deliverable for non-mutating ownership probes."""
    path = "/api/deliverables?project=switchboard"
    probe = http_fingerprint(
        f"{base_url.rstrip('/')}{path}", token=token, method="GET",
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    payload: Any = None
    error = ""
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            ValueError) as exc:
        error = str(exc)
    deliverables = payload.get("deliverables") if isinstance(payload, dict) else None
    candidates = sorted(
        str(item.get("deliverable_id") or item.get("id") or "").strip()
        for item in (deliverables if isinstance(deliverables, list) else [])
        if isinstance(item, dict)
        and str(item.get("deliverable_id") or item.get("id") or "").strip()
    )
    selected = candidates[0] if candidates else ""
    ok = probe.get("http_status") == 200 and bool(selected)
    return selected, CheckResult(
        name="live_edge:probe_deliverable",
        ok=ok,
        detail={"path": path, "selected_deliverable_id": selected or None,
                "candidate_count": len(candidates), "error": error or None},
        message=(f"selected existing deliverable {selected} for ownership probes"
                 if ok else "could not select an existing deliverable for ownership probes"),
    )


_HANDLE_RE = re.compile(
    r"handle\s+(?P<path>[^\s{]+)\s*\{(?P<body>.*?)\n\s*\}",
    re.DOTALL,
)
_PROXY_RE = re.compile(r"reverse_proxy\s+([^\s{]+)")


def parse_edge_ownership(caddy_text: str) -> dict[str, str]:
    """Map Caddy handle path → upstream host:port (first reverse_proxy)."""
    owns: dict[str, str] = {}
    for match in _HANDLE_RE.finditer(caddy_text):
        path = match.group("path").strip()
        body = match.group("body")
        proxy = _PROXY_RE.search(body)
        if not proxy:
            continue
        upstream = proxy.group(1).strip()
        # normalize 127.0.0.1:8122 → keep as-is; strip scheme if present
        owns[path] = upstream
    return owns


def edge_owns_port(owns: dict[str, str], path_pattern: str, port: int) -> bool:
    """True when a handle matching path_pattern proxies to the given port."""
    # Exact key first, then prefix/wildcard style.
    candidates = []
    if path_pattern in owns:
        candidates.append(owns[path_pattern])
    else:
        needle = path_pattern.rstrip("*")
        for key, upstream in owns.items():
            if key == path_pattern or key.startswith(needle) or path_pattern.startswith(key.rstrip("*")):
                candidates.append(upstream)
    return any(upstream.endswith(f":{port}") for upstream in candidates)


def check_sha(root: Path, canonical_sha: str) -> CheckResult:
    vm = git_sha(root, "HEAD")
    ok = bool(vm) and vm == canonical_sha
    return CheckResult(
        name="exact_sha",
        ok=ok,
        detail={"canonical_sha": canonical_sha, "vm_sha": vm},
        message="VM HEAD matches canonical SHA" if ok else "VM HEAD does not match canonical SHA",
    )


def check_caddy_checksum(repo_caddy: Path, live_caddy: Path) -> CheckResult:
    repo_hash = sha256_file(repo_caddy)
    live_hash = sha256_file(live_caddy)
    ok = bool(repo_hash) and repo_hash == live_hash
    return CheckResult(
        name="caddy_checksum",
        ok=ok,
        detail={
            "repo_caddy": str(repo_caddy),
            "live_caddy": str(live_caddy),
            "repo_sha256": repo_hash,
            "live_sha256": live_hash,
        },
        message="repo and live Caddyfile checksums match" if ok else "Caddyfile checksum mismatch or missing",
    )


def check_unit(service: str) -> CheckResult:
    state = unit_state(service)
    ok = state["active"] == "active" and state["enabled"] in {"enabled", "static"}
    return CheckResult(
        name=f"unit:{service}",
        ok=ok,
        detail=state,
        message=f"{service} enabled+active" if ok else f"{service} not enabled+active",
    )


def check_listener(host: str, port: int) -> CheckResult:
    ok = port_listening(host, port)
    return CheckResult(
        name=f"listener:{host}:{port}",
        ok=ok,
        detail={"host": host, "port": port},
        message=f"{host}:{port} accepting connections" if ok else f"{host}:{port} not listening",
    )


def check_health(service: str, host: str, port: int) -> CheckResult:
    url = f"http://{host}:{port}/health"
    body = local_health(url, expect_service=service)
    ok = body.get("http_status") == 200 and body.get("service") == service
    return CheckResult(
        name=f"health:{service}",
        ok=ok,
        detail=body,
        message=f"{service} /health identifies service" if ok else f"{service} /health failed identity check",
    )


def check_readiness(service: str, host: str, port: int, path: str) -> CheckResult:
    url = f"http://{host}:{port}{path}"
    body = local_health(url, expect_service=service)
    ok = (body.get("http_status") == 200
          and body.get("service") == service
          and body.get("status") == "ready")
    return CheckResult(
        name=f"ready:{service}", ok=ok, detail=body,
        message=(f"{service} dependency readiness passed"
                 if ok else f"{service} dependency readiness failed"),
    )


def check_edge(
    caddy_text: str,
    path_pattern: str,
    port: int,
    *,
    source: str,
) -> CheckResult:
    owns = parse_edge_ownership(caddy_text)
    ok = edge_owns_port(owns, path_pattern, port)
    return CheckResult(
        name=f"edge:{path_pattern}->{port}",
        ok=ok,
        detail={"source": source, "path": path_pattern, "port": port, "handles": owns},
        message=(
            f"edge {path_pattern} owns :{port}"
            if ok
            else f"edge {path_pattern} does not own :{port}"
        ),
    )


def build_evidence(
    *,
    root: Path,
    canonical_sha: str,
    caddy_live: Path,
    services: list[tuple[str, int]],
    edge_owns: list[tuple[str, int]],
    readiness: list[tuple[str, int, str]] | None = None,
    host: str = "127.0.0.1",
    skip_live_probes: bool = False,
    caddy_text_override: str | None = None,
    unit_fn: Callable[[str], CheckResult] | None = None,
    listener_fn: Callable[[str, int], CheckResult] | None = None,
    health_fn: Callable[[str, str, int], CheckResult] | None = None,
    edge_base_url: str = "",
    bearer_token: str = "",
    probe_task_id: str = "",
) -> dict[str, Any]:
    repo_caddy = root / "deploy" / "Caddyfile"
    checks: list[CheckResult] = [check_sha(root, canonical_sha)]
    if not skip_live_probes:
        checks.append(check_caddy_checksum(repo_caddy, caddy_live))

    unit_fn = unit_fn or check_unit
    listener_fn = listener_fn or check_listener
    health_fn = health_fn or check_health
    readiness = readiness or []

    for service, port in services:
        if skip_live_probes:
            continue
        checks.append(unit_fn(service))
        checks.append(listener_fn(host, port))
        checks.append(health_fn(service, host, port))
    if not skip_live_probes:
        for service, port, path in readiness:
            checks.append(check_readiness(service, host, port, path))

    if caddy_text_override is not None:
        caddy_text = caddy_text_override
        source = "override"
    elif caddy_live.is_file():
        caddy_text = caddy_live.read_text(encoding="utf-8")
        source = str(caddy_live)
    else:
        caddy_text = repo_caddy.read_text(encoding="utf-8") if repo_caddy.is_file() else ""
        source = str(repo_caddy)

    for path_pattern, port in edge_owns:
        checks.append(check_edge(caddy_text, path_pattern, port, source=source))

    # BUG-69: anonymous-rejection is checked independent of the authenticated route-
    # ownership probes below (no bearer token needed, no task id needed -- the list
    # endpoint is enough). Only runs when a live edge is actually being probed.
    service_ports = {service: port for service, port in services}
    if edge_base_url and not skip_live_probes:
        checks.append(check_anon_read_rejected(
            base_url=edge_base_url, path="/api/tasks?project=switchboard",
        ))
        if service_ports.get("switchboard-deliverables") == 8124:
            checks.append(check_anon_read_rejected(
                base_url=edge_base_url, path="/api/deliverables?project=switchboard",
            ))

    if edge_base_url or bearer_token or probe_task_id:
        if not edge_base_url or not bearer_token:
            checks.append(CheckResult(
                name="live_edge:credentials",
                ok=False,
                detail={"edge_base_url_present": bool(edge_base_url),
                        "bearer_token_present": bool(bearer_token)},
                message="live edge probes require base URL and bearer token",
            ))
        else:
            selected_task_id, selection_check = resolve_probe_task_id(
                base_url=edge_base_url,
                token=bearer_token,
                configured_task_id=probe_task_id,
            )
            checks.append(selection_check)
            task = urllib.parse.quote(selected_task_id, safe="")
            project_qs = "?project=switchboard"
            if selected_task_id:
                checks.extend([
                check_live_route_owner(
                    base_url=edge_base_url,
                    path=f"/api/tasks/{task}{project_qs}", method="GET",
                    token=bearer_token, owner_port=8122, other_port=8110,
                ),
                check_live_route_owner(
                    base_url=edge_base_url,
                    path=f"/api/tasks/{task}/dispatch/latest{project_qs}", method="GET",
                    token=bearer_token, owner_port=8110, other_port=8122,
                ),
                check_live_route_owner(
                    base_url=edge_base_url,
                    # Chat is POST-only. An empty JSON body reaches the monolith
                    # handler and fails with "message required" before any write;
                    # the thin Tasks app has no chat route and returns its generic 404.
                    path=f"/api/tasks/{task}/chat{project_qs}", method="POST",
                    token=bearer_token, owner_port=8110, other_port=8122,
                    json_body=b"{}",
                ),
                check_live_route_owner(
                    base_url=edge_base_url,
                    path=f"/api/tasks/{task}/review_verdict{project_qs}", method="GET",
                    token=bearer_token, owner_port=8110, other_port=8122,
                ),
                ])
            if service_ports.get("switchboard-deliverables") == 8124:
                selected_deliverable_id, selection_check = resolve_probe_deliverable_id(
                    base_url=edge_base_url, token=bearer_token,
                )
                checks.append(selection_check)
                deliverable = urllib.parse.quote(selected_deliverable_id, safe="")
                checks.append(check_live_route_owner(
                    base_url=edge_base_url,
                    path="/api/deliverables?project=switchboard", method="GET",
                    token=bearer_token, owner_port=8124, other_port=8110,
                ))
                if selected_deliverable_id:
                    checks.append(check_live_route_owner(
                        base_url=edge_base_url,
                        path=f"/api/deliverables/{deliverable}?project=switchboard",
                        method="GET", token=bearer_token,
                        owner_port=8124, other_port=8110,
                    ))
                # Invalid-id write is side-effect free and proves the GET-only matcher
                # leaves Deliverables mutations on the monolith.
                checks.append(check_live_route_owner(
                    base_url=edge_base_url,
                    path=("/api/deliverables/__runtime_probe_missing__/closure_request"
                          "?project=switchboard"),
                    method="POST", token=bearer_token,
                    owner_port=8110, other_port=8124, json_body=b"{}",
                ))

    ok = all(c.ok for c in checks)
    return {
        "schema": SCHEMA,
        "ok": ok,
        "observed_at": time.time(),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "root": str(root),
        "canonical_sha": canonical_sha,
        "vm_sha": git_sha(root, "HEAD"),
        "services": [{"service": s, "port": p} for s, p in services],
        "readiness": [
            {"service": service, "port": port, "path": path}
            for service, port, path in readiness
        ],
        "checks": [
            {
                "name": c.name,
                "ok": c.ok,
                "message": c.message,
                "detail": c.detail,
            }
            for c in checks
        ],
    }


def _parse_service(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError(
            f"service must be name:port (got {value!r})"
        )
    name, port_s = value.rsplit(":", 1)
    return name, int(port_s)


def _parse_edge(value: str) -> tuple[str, int]:
    # '/api/tasks*:8122'
    if ":" not in value:
        raise argparse.ArgumentTypeError(
            f"edge-owns must be path:port (got {value!r})"
        )
    path, port_s = value.rsplit(":", 1)
    return path, int(port_s)


def _parse_ready(value: str) -> tuple[str, int, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or not parts[2].startswith("/"):
        raise argparse.ArgumentTypeError(
            f"ready must be service:port:/path (got {value!r})"
        )
    return parts[0], int(parts[1]), parts[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("PLAN_ROOT", "/opt/projectplanner"))
    parser.add_argument("--canonical-sha", default="")
    parser.add_argument("--caddy-live", default="/etc/caddy/Caddyfile")
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        dest="services",
        help="service:port (repeatable). Also accepts --service NAME --port N pairs via NAME:PORT.",
    )
    parser.add_argument(
        "--edge-owns",
        action="append",
        default=[],
        dest="edge_owns",
        help="path:port the edge must own (repeatable), e.g. '/api/tasks*:8122'",
    )
    parser.add_argument(
        "--ready", action="append", default=[], dest="readiness",
        help="service:port:/path dependency-readiness probe (repeatable)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--edge-base-url", default=os.environ.get("PM_BASE", ""))
    parser.add_argument("--probe-task-id", default="")
    parser.add_argument(
        "--skip-live-probes",
        action="store_true",
        help="Only check SHA + edge ownership from repo Caddyfile (CI/static mode).",
    )
    parser.add_argument("--json-out", default="", help="Write evidence JSON to this path.")
    parser.add_argument(
        "--fixture-json",
        default="",
        help="Load a pre-built evidence-like fixture and exit with its ok flag (test aid).",
    )
    args = parser.parse_args(argv)

    if args.fixture_json:
        payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8"))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 1

    root = Path(args.root)
    canonical = args.canonical_sha or git_sha(root, "origin/master") or git_sha(root, "HEAD") or ""
    if not canonical:
        print("!! could not resolve --canonical-sha", file=sys.stderr)
        return 2

    services = [_parse_service(raw) for raw in args.services]
    edge_owns = [_parse_edge(raw) for raw in args.edge_owns]
    readiness = [_parse_ready(raw) for raw in args.readiness]

    evidence = build_evidence(
        root=root,
        canonical_sha=canonical,
        caddy_live=Path(args.caddy_live),
        services=services,
        readiness=readiness,
        edge_owns=edge_owns,
        host=args.host,
        skip_live_probes=args.skip_live_probes,
        edge_base_url=args.edge_base_url,
        bearer_token=os.environ.get("PM_RUNTIME_PROOF_TOKEN", ""),
        probe_task_id=args.probe_task_id,
    )
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
