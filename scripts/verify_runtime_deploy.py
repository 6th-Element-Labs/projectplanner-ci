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
    host: str = "127.0.0.1",
    skip_live_probes: bool = False,
    caddy_text_override: str | None = None,
    unit_fn: Callable[[str], CheckResult] | None = None,
    listener_fn: Callable[[str, int], CheckResult] | None = None,
    health_fn: Callable[[str, str, int], CheckResult] | None = None,
) -> dict[str, Any]:
    repo_caddy = root / "deploy" / "Caddyfile"
    checks: list[CheckResult] = [check_sha(root, canonical_sha)]
    if not skip_live_probes:
        checks.append(check_caddy_checksum(repo_caddy, caddy_live))

    unit_fn = unit_fn or check_unit
    listener_fn = listener_fn or check_listener
    health_fn = health_fn or check_health

    for service, port in services:
        if skip_live_probes:
            continue
        checks.append(unit_fn(service))
        checks.append(listener_fn(host, port))
        checks.append(health_fn(service, host, port))

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
    parser.add_argument("--host", default="127.0.0.1")
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

    evidence = build_evidence(
        root=root,
        canonical_sha=canonical,
        caddy_live=Path(args.caddy_live),
        services=services,
        edge_owns=edge_owns,
        host=args.host,
        skip_live_probes=args.skip_live_probes,
    )
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
