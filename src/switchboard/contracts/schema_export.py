"""Export registered ``switchboard.*.v1`` JSON Schemas with ``$id`` (ARCH-MS-42).

Unlike the OpenAPI artifact (ARCH-MS-41), which strips ``$id`` for component
naming, this registry keeps the short schema id on every exported document so
event producers/consumers can resolve contracts by ``$id``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Import contract packages so ``register()`` side effects populate the registry.
from . import agents, claims, projects, tasks, wakes  # noqa: F401
from .registry import list_schemas

MANIFEST_SCHEMA = "switchboard.schema_manifest.v1"
MANIFEST_FILENAME = "manifest.json"
V1_SUFFIX = ".v1"
SWITCHBOARD_PREFIX = "switchboard."


def is_v1_schema_id(schema_id: str) -> bool:
    """Return True for registry ids shaped like ``switchboard.*.v1``."""
    return (
        isinstance(schema_id, str)
        and schema_id.startswith(SWITCHBOARD_PREFIX)
        and schema_id.endswith(V1_SUFFIX)
        and schema_id.count(".") >= 2
    )


def registered_v1_schemas() -> dict[str, dict[str, Any]]:
    """Return a deterministic map of registered v1 schemas (``$id`` preserved)."""
    out: dict[str, dict[str, Any]] = {}
    for schema_id, payload in sorted(list_schemas().items()):
        if not is_v1_schema_id(schema_id):
            continue
        schema = dict(payload)
        schema["$id"] = schema_id
        out[schema_id] = schema
    return out


def schema_filename(schema_id: str) -> str:
    """Map ``switchboard.task.create_command.v1`` → filename under ``schemas/``."""
    return f"{schema_id}.json"


def build_manifest(schemas: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the checked-in schema registry manifest."""
    payloads = schemas if schemas is not None else registered_v1_schemas()
    entries = [
        {
            "$id": schema_id,
            "path": schema_filename(schema_id),
        }
        for schema_id in payloads
    ]
    return {
        "schema": MANIFEST_SCHEMA,
        "count": len(entries),
        "schemas": entries,
    }


def render_json(payload: dict[str, Any]) -> str:
    """Serialize with stable key order and a trailing newline."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_schema_registry(out_dir: Path) -> dict[str, Any]:
    """Write every v1 schema plus ``manifest.json`` under ``out_dir``.

    Returns the manifest that was written.
    """
    schemas = registered_v1_schemas()
    out_dir.mkdir(parents=True, exist_ok=True)

    expected_names = {schema_filename(sid) for sid in schemas} | {MANIFEST_FILENAME}
    # Drop any leftover switchboard.*.json (including accidental v2) so the
    # tree matches the export set after a write, not only under --check.
    for existing in out_dir.glob("switchboard.*.json"):
        if existing.name not in expected_names:
            existing.unlink()

    for schema_id, payload in schemas.items():
        path = out_dir / schema_filename(schema_id)
        path.write_text(render_json(payload), encoding="utf-8")

    manifest = build_manifest(schemas)
    (out_dir / MANIFEST_FILENAME).write_text(render_json(manifest), encoding="utf-8")
    return manifest


def check_schema_registry(out_dir: Path) -> list[str]:
    """Compare ``out_dir`` to a fresh export; return human-readable drift notes."""
    schemas = registered_v1_schemas()
    expected_manifest = build_manifest(schemas)
    problems: list[str] = []

    manifest_path = out_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        problems.append(f"missing {MANIFEST_FILENAME}")
    else:
        existing_manifest = manifest_path.read_text(encoding="utf-8")
        if existing_manifest != render_json(expected_manifest):
            problems.append(f"{MANIFEST_FILENAME} is out of date")

    for schema_id, payload in schemas.items():
        path = out_dir / schema_filename(schema_id)
        if not path.is_file():
            problems.append(f"missing {schema_filename(schema_id)}")
            continue
        if path.read_text(encoding="utf-8") != render_json(payload):
            problems.append(f"{schema_filename(schema_id)} is out of date")
        else:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                problems.append(f"{schema_filename(schema_id)} is not valid JSON: {exc}")
            else:
                if loaded.get("$id") != schema_id:
                    problems.append(
                        f"{schema_filename(schema_id)} $id mismatch "
                        f"(want {schema_id!r}, got {loaded.get('$id')!r})"
                    )

    expected_names = {schema_filename(sid) for sid in schemas} | {MANIFEST_FILENAME}
    if out_dir.is_dir():
        for path in sorted(out_dir.iterdir()):
            if path.is_file() and path.name not in expected_names:
                if path.name.startswith("switchboard.") and path.name.endswith(".json"):
                    problems.append(f"stale schema file present: {path.name}")

    return problems
