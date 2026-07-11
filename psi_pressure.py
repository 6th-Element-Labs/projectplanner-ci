"""Linux PSI (/proc/pressure/*) reader for saturation signals (PERF-7).

PSI measures time tasks spend stalled waiting on CPU, memory, or I/O — a leading
indicator of overload that load average misses.  When PSI is unavailable (macOS,
older kernels, containers without the mount) the reader returns an explicit
``available: false`` signal instead of silently substituting zeros.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional

RESOURCES = ("cpu", "memory", "io")
_LINE_RE = re.compile(
    r"^(some|full)\s+avg10=([\d.]+)\s+avg60=([\d.]+)\s+avg300=([\d.]+)\s+total=(\d+)$"
)


def _proc_root() -> str:
    return (os.environ.get("PM_PSI_PROC_ROOT") or "/proc").rstrip("/")


def _read_pressure_file(path: str) -> Optional[str]:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read()
    except OSError:
        return None


def parse_pressure_text(text: str) -> Dict[str, Dict[str, float]]:
    """Parse the two-line PSI stall file format into {some, full} averages."""
    out: Dict[str, Dict[str, float]] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        kind, avg10, avg60, avg300, total = match.groups()
        out[kind] = {
            "avg10": float(avg10),
            "avg60": float(avg60),
            "avg300": float(avg300),
            "total_us": int(total),
        }
    return out


def read_psi(resource: str, proc_root: Optional[str] = None) -> dict:
    """Read one PSI resource file.  ``resource`` is cpu, memory, or io."""
    root = (proc_root or _proc_root()).rstrip("/")
    path = f"{root}/pressure/{resource}"
    text = _read_pressure_file(path)
    if text is None:
        return {
            "resource": resource,
            "available": False,
            "path": path,
            "stall": {},
        }
    stall = parse_pressure_text(text)
    return {
        "resource": resource,
        "available": bool(stall),
        "path": path,
        "stall": stall,
    }


def read_all_psi(proc_root: Optional[str] = None) -> dict:
    """Snapshot all three PSI resources."""
    resources = {name: read_psi(name, proc_root=proc_root) for name in RESOURCES}
    available = any(item.get("available") for item in resources.values())
    return {
        "schema": "switchboard.psi_pressure.v1",
        "available": available,
        "proc_root": (proc_root or _proc_root()).rstrip("/"),
        "resources": resources,
    }
