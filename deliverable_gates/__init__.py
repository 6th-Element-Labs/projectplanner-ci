"""Deliverable closure-gate registry (DELIVERABLES-14).

The registry maps stable gate ids to concrete check definitions so a
deliverable's ``proof_requirements.gates`` can reference a shared harness check
by id instead of re-declaring how to run it. The closure engine
(DELIVERABLES-15 ``verify_deliverable_closure``) consumes what
:func:`resolve_gates` returns; this module never *runs* a gate, it only loads,
validates, and resolves the mapping.

Manifest: ``deliverable_gates/manifest.json`` — see
``docs/DELIVERABLE-CLOSURE-GATE.md``.

Three ways a ``proof_requirements.gates`` entry binds to a check
------------------------------------------------------------------
1. **Reference** — ``{"id": "harness:concurrent_load_gate", "required": true}``
   resolves to the registry entry with that id.
2. **Override** — the same reference may override a small allowlist of fields
   (``required``, ``timeout_s``, ``env_allowlist``, ``args``, ``params``,
   ``cwd``, ``title``, ``description``) without editing the shared manifest,
   e.g. ``{"id": "harness:test_mcp_observability", "required": true}`` to
   promote an optional gate for one deliverable.
3. **Inline** — an entry that is not in the registry but carries its own
   ``kind`` defines a per-deliverable gate on the spot, e.g.
   ``{"id": "store:foo", "kind": "store_check", "check": "foo"}``.

A referenced id that is neither in the registry, the reserved ``scope``
built-in, nor a valid inline definition fails closed with
:class:`GateResolutionError` — a dangling gate reference is a real wiring bug,
not something to silently drop (see the fail-fix-early working agreement).

Kinds: ``script``, ``pytest``, ``store_check``, ``offline_evidence`` (declared
in the manifest) plus the reserved built-in ``scope`` (Gate 1, referenced as
``{"id": "scope"}`` and never declared in the manifest).
"""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

REGISTRY_SCHEMA = "switchboard.deliverable_gate_registry.v1"
RESOLVED_SCHEMA = "switchboard.deliverable_resolved_gates.v1"

#: Reserved gate id for the built-in scope gate (Gate 1). It is never declared
#: in the manifest; ``resolve_gates`` synthesises it from a ``{"id": "scope"}``
#: reference.
SCOPE_GATE_ID = "scope"
SCOPE_KIND = "scope"

#: Kinds a manifest gate may declare.
GATE_KINDS = ("script", "pytest", "store_check", "offline_evidence")

#: Fields a ``proof_requirements.gates`` entry may override on a referenced
#: registry gate. ``id``/``kind``/``command`` define a gate's identity and are
#: intentionally not overridable (change the manifest, or define it inline).
OVERRIDE_KEYS = (
    "required",
    "timeout_s",
    "env_allowlist",
    "args",
    "params",
    "cwd",
    "title",
    "description",
)

# Gate ids are lowercase tokens joined by ``:`` ``_`` ``-`` ``.`` ``/`` — enough
# for ``harness:concurrent_load_gate`` / ``store:links_terminal`` while catching
# whitespace and empty segments early.
_ID_RE = re.compile(r"^[a-z0-9]+(?:[:_./-][a-z0-9]+)*$")


class GateRegistryError(ValueError):
    """The manifest is missing, unreadable, or malformed."""


class GateResolutionError(ValueError):
    """A deliverable's ``proof_requirements.gates`` is invalid or dangling."""


def manifest_path() -> Path:
    """Absolute path to the committed registry manifest."""
    return Path(__file__).resolve().with_name("manifest.json")


# --- validation -------------------------------------------------------------

def _require_id(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateRegistryError(f"{where}: gate 'id' must be a non-empty string")
    gid = value.strip()
    if not _ID_RE.match(gid):
        raise GateRegistryError(
            f"{where}: gate id {gid!r} is not a valid token "
            "(lowercase, ':' '_' '-' '.' '/' separated)"
        )
    return gid


def _check_str(entry: Dict[str, Any], field: str, *, where: str, required: bool) -> None:
    if field not in entry:
        if required:
            raise GateRegistryError(f"{where}: {field!r} is required")
        return
    if not isinstance(entry[field], str) or not entry[field].strip():
        raise GateRegistryError(f"{where}: {field!r} must be a non-empty string")


def _check_str_list(entry: Dict[str, Any], field: str, *, where: str, required: bool) -> None:
    if field not in entry:
        if required:
            raise GateRegistryError(f"{where}: {field!r} is required")
        return
    value = entry[field]
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GateRegistryError(f"{where}: {field!r} must be a non-empty list of strings")


def _check_timeout(entry: Dict[str, Any], *, where: str) -> None:
    if "timeout_s" not in entry:
        return
    value = entry["timeout_s"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise GateRegistryError(f"{where}: 'timeout_s' must be a positive number")


def _check_env_allowlist(entry: Dict[str, Any], *, where: str) -> None:
    if "env_allowlist" not in entry:
        return
    value = entry["env_allowlist"]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GateRegistryError(f"{where}: 'env_allowlist' must be a list of strings")


def validate_gate(entry: Any, *, where: str = "gate") -> Dict[str, Any]:
    """Validate one gate definition and return a normalised copy.

    ``required`` defaults to ``True`` (a declared gate is required unless a
    deliverable explicitly waives it). Raises :class:`GateRegistryError` on any
    structural problem. The reserved built-in ``scope`` id/kind is not a
    declarable gate — ``resolve_gates`` synthesises it instead.
    """
    if not isinstance(entry, dict):
        raise GateRegistryError(f"{where}: gate must be a JSON object")
    gid = _require_id(entry.get("id"), where=where)
    where = f"{where} {gid!r}"

    kind = entry.get("kind")
    if kind not in GATE_KINDS:
        raise GateRegistryError(
            f"{where}: 'kind' must be one of {sorted(GATE_KINDS)} (got {kind!r})"
        )
    if gid == SCOPE_GATE_ID:
        raise GateRegistryError(
            f"{where}: gate id 'scope' is reserved for the built-in Gate 1"
        )

    if "required" in entry and not isinstance(entry["required"], bool):
        raise GateRegistryError(f"{where}: 'required' must be a boolean")
    _check_timeout(entry, where=where)
    _check_env_allowlist(entry, where=where)

    if kind == "script":
        _check_str_list(entry, "command", where=where, required=True)
        _check_str(entry, "cwd", where=where, required=False)
    elif kind == "pytest":
        _check_str(entry, "target", where=where, required=True)
        if "args" in entry:
            _check_str_list(entry, "args", where=where, required=True)
    elif kind == "store_check":
        _check_str(entry, "check", where=where, required=True)
        if "params" in entry and not isinstance(entry["params"], dict):
            raise GateRegistryError(f"{where}: 'params' must be a JSON object")
    elif kind == "offline_evidence":
        _check_str(entry, "task_id", where=where, required=True)
        _check_str(entry, "task_project", where=where, required=False)

    normalised = copy.deepcopy(entry)
    normalised["id"] = gid
    normalised.setdefault("required", True)
    return normalised


# --- manifest loading (mtime-cached) ---------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {}


def load_manifest(path: Optional[Path] = None, *, use_cache: bool = True) -> Dict[str, Any]:
    """Load, validate, and return the registry manifest as a dict.

    Cached by ``(path, mtime, size)`` so repeated resolution during a closure
    run does not re-read/re-parse disk. Raises :class:`GateRegistryError`.
    """
    target = Path(path) if path is not None else manifest_path()
    try:
        stat = target.stat()
    except OSError as exc:
        raise GateRegistryError(f"gate registry manifest missing: {target} ({exc})") from exc
    cache_key = str(target)
    signature = (stat.st_mtime_ns, stat.st_size)
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] == signature:
                return cached[1]

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateRegistryError(f"gate registry manifest unreadable ({target}): {exc}") from exc

    manifest = _validate_manifest(raw, where=str(target))
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (signature, manifest)
    return manifest


def _validate_manifest(raw: Any, *, where: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise GateRegistryError(f"{where}: manifest must be a JSON object")
    if raw.get("schema") != REGISTRY_SCHEMA:
        raise GateRegistryError(
            f"{where}: 'schema' must be {REGISTRY_SCHEMA!r} (got {raw.get('schema')!r})"
        )
    gates = raw.get("gates")
    if not isinstance(gates, list):
        raise GateRegistryError(f"{where}: 'gates' must be a list")
    seen: Dict[str, int] = {}
    validated: List[Dict[str, Any]] = []
    for index, gate in enumerate(gates):
        entry = validate_gate(gate, where=f"{where} gates[{index}]")
        gid = entry["id"]
        if gid in seen:
            raise GateRegistryError(
                f"{where}: duplicate gate id {gid!r} (gates[{seen[gid]}] and gates[{index}])"
            )
        seen[gid] = index
        validated.append(entry)
    manifest = copy.deepcopy(raw)
    manifest["gates"] = validated
    return manifest


def registry_gates(path: Optional[Path] = None, *, use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """Return ``{gate_id: validated_entry}`` for the manifest."""
    manifest = load_manifest(path, use_cache=use_cache)
    return {gate["id"]: gate for gate in manifest["gates"]}


def gate_ids(path: Optional[Path] = None) -> List[str]:
    """Sorted list of gate ids declared in the manifest."""
    return sorted(registry_gates(path).keys())


# --- resolution -------------------------------------------------------------

def builtin_scope_gate(required: bool = True) -> Dict[str, Any]:
    """The reserved Gate 1 (scope complete) spec, synthesised not stored."""
    return {
        "id": SCOPE_GATE_ID,
        "kind": SCOPE_KIND,
        "title": "Scope complete (Gate 1)",
        "required": bool(required),
        "builtin": True,
        "source": "builtin",
    }


def _apply_overrides(base: Dict[str, Any], entry: Dict[str, Any], *, where: str) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key in OVERRIDE_KEYS:
        if key in entry:
            merged[key] = copy.deepcopy(entry[key])
    # Re-validate so an override cannot smuggle in an invalid timeout/list/etc.
    return validate_gate(merged, where=where)


def resolve_gates(
    proof_requirements: Optional[Dict[str, Any]],
    *,
    path: Optional[Path] = None,
    include_scope: bool = False,
) -> List[Dict[str, Any]]:
    """Resolve ``proof_requirements.gates`` to fully-specified gate dicts.

    Each returned gate carries a ``source`` of ``builtin`` (scope), ``registry``
    (referenced/overridden manifest gate), or ``inline`` (defined on the entry).
    Order follows the declared ``gates`` list. With ``include_scope=True`` the
    built-in scope gate is prepended when the deliverable did not declare it, so
    the executor always runs Gate 1. Raises :class:`GateResolutionError`.
    """
    pr = proof_requirements or {}
    if not isinstance(pr, dict):
        raise GateResolutionError("proof_requirements must be a JSON object")
    entries = pr.get("gates")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise GateResolutionError("proof_requirements.gates must be a list")

    registry = registry_gates(path)
    resolved: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"proof_requirements.gates[{index}]"
        if not isinstance(entry, dict):
            raise GateResolutionError(f"{where}: gate reference must be a JSON object")
        gid = entry.get("id")
        if not isinstance(gid, str) or not gid.strip():
            raise GateResolutionError(f"{where}: 'id' must be a non-empty string")
        gid = gid.strip()
        if gid in seen:
            raise GateResolutionError(f"{where}: duplicate gate id {gid!r}")
        seen.add(gid)

        if gid == SCOPE_GATE_ID:
            required = entry.get("required", True)
            if not isinstance(required, bool):
                raise GateResolutionError(f"{where}: 'required' must be a boolean")
            resolved.append(builtin_scope_gate(required))
            continue
        if gid in registry:
            try:
                merged = _apply_overrides(registry[gid], entry, where=where)
            except GateRegistryError as exc:
                raise GateResolutionError(str(exc)) from exc
            merged["source"] = "registry"
            resolved.append(merged)
            continue
        if "kind" in entry:
            try:
                merged = validate_gate(entry, where=where)
            except GateRegistryError as exc:
                raise GateResolutionError(str(exc)) from exc
            merged["source"] = "inline"
            resolved.append(merged)
            continue
        raise GateResolutionError(
            f"{where}: unknown gate id {gid!r}; not in the registry "
            f"({sorted(registry)}), not the built-in 'scope', and no inline "
            "'kind' provided"
        )

    if include_scope and SCOPE_GATE_ID not in seen:
        resolved.insert(0, builtin_scope_gate(True))
    return resolved


def resolve_deliverable_gates(
    deliverable: Dict[str, Any],
    *,
    path: Optional[Path] = None,
    include_scope: bool = False,
) -> List[Dict[str, Any]]:
    """Convenience: resolve a deliverable dict's ``proof_requirements`` gates."""
    return resolve_gates(
        (deliverable or {}).get("proof_requirements"),
        path=path,
        include_scope=include_scope,
    )


def partition_gates(resolved: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split resolved gates into ``(scope_gates, functional_gates)``.

    Maps directly onto the closure report shape ``gates.scope`` / ``gates.functional``.
    """
    scope = [gate for gate in resolved if gate.get("kind") == SCOPE_KIND]
    functional = [gate for gate in resolved if gate.get("kind") != SCOPE_KIND]
    return scope, functional
