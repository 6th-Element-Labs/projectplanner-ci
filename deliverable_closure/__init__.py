"""Deliverable closure engine (DELIVERABLES-15).

Runs the two closure gates from ``docs/DELIVERABLE-CLOSURE-GATE.md`` and produces
a graded ``switchboard.deliverable_closure_report.v1``:

* **Gate 1 — scope** (``scope_gate``): pure store logic over ``get_mission_status``
  — no blockers, nothing In Review / In Progress, every linked task terminal
  (Done+terminal provenance, Cancelled, or operator-waived), and a
  done-with-proof ratio floor.
* **Gate 2 — functional** (``functional_gate``): runs the deliverable's
  ``proof_requirements.gates`` resolved through :mod:`deliverable_gates`. Store
  kinds (``store_check``, ``offline_evidence``) run in-process; command kinds
  (``script``, ``pytest``) either take results a verifier agent already produced
  (``submitted_functional``) or are executed here only when ``run_scripts=True``
  (dogfood / CI). A required command gate with neither is recorded ``not_run``
  and holds the grade closed — it is never optimistically passed.

This module is the engine only. Persisting the report and the
``deliverable.closure_verified`` activity stamp, plus the MCP/REST surface, are
DELIVERABLES-16; it imports :mod:`store` and :mod:`deliverable_gates` and nothing
imports it back, so there is no cycle. Grading is deterministic; pass ``now`` for
reproducible reports in tests.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import deliverable_gates
import store

CLOSURE_REPORT_SCHEMA = "switchboard.deliverable_closure_report.v1"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Statuses that keep a linked task non-terminal for closure. "In Review" is
# reported by its own scope check; the terminal states are Done+provenance and
# Cancelled (a deliberate terminal outcome).
_ACTIVE_STATUSES = ("In Progress", "Ready", "Todo", "Backlog", "Blocked")
_CANCELLED_STATUSES = ("Cancelled", "Canceled")

#: Default minimum done-with-proof ratio (over non-waived links).
DEFAULT_MIN_PROOF_RATIO = 1.0

#: Env vars always visible to a command gate; everything else must be named in
#: the gate's ``env_allowlist`` (glob-matched). This makes the manifest's
#: allowlist a real boundary rather than decoration.
_SAFE_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP",
    "SYSTEMROOT", "PYTHONHASHSEED", "PYTHONUNBUFFERED",
)

#: Command-gate execution ceiling when the gate declares no ``timeout_s``.
DEFAULT_GATE_TIMEOUT_S = 900


class ClosureError(ValueError):
    """The deliverable is missing or a closure input is malformed."""


# --- helpers ----------------------------------------------------------------

def _check(check_id: str, passed: bool, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"id": check_id, "pass": bool(passed), "detail": detail or {}}


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _waiver_index(waivers: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for waiver in waivers or []:
        if not isinstance(waiver, dict):
            raise ClosureError("each waiver must be a JSON object")
        task_id = waiver.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ClosureError("waiver requires a task_id")
        if not waiver.get("reason"):
            raise ClosureError(f"waiver for {task_id} requires a reason")
        index[task_id.strip()] = dict(waiver)
    return index


# --- Gate 1: scope ----------------------------------------------------------

def scope_gate(mission_status: Dict[str, Any],
               waivers: Optional[List[Dict[str, Any]]] = None,
               *, min_proof_ratio: float = DEFAULT_MIN_PROOF_RATIO) -> Dict[str, Any]:
    """Gate 1 — scope complete. Pure logic over a ``get_mission_status`` rollup."""
    waived = _waiver_index(waivers)
    blockers = mission_status.get("blockers") or []
    progress = mission_status.get("progress") or {}
    linked = mission_status.get("linked_tasks") or []

    in_review = int(progress.get("in_review_count") or 0)

    active: List[Dict[str, Any]] = []
    non_terminal: List[Dict[str, Any]] = []
    waived_linked = 0
    cancelled_linked = 0
    for link in linked:
        detail = link.get("task_detail") or link.get("task") or {}
        task_id = detail.get("task_id") or link.get("task_id")
        status = detail.get("status")
        if task_id in waived:
            waived_linked += 1
            continue
        if status in _CANCELLED_STATUSES:
            cancelled_linked += 1
        if status in _ACTIVE_STATUSES:
            active.append({"task_id": task_id, "status": status,
                           "project_id": link.get("project_id")})
        provenance = detail.get("provenance") or {}
        terminal = (status == "Done" and provenance.get("terminal")) or (
            status in _CANCELLED_STATUSES)
        if not terminal:
            non_terminal.append({
                "task_id": task_id,
                "status": status,
                "project_id": link.get("project_id"),
                "provenance_type": provenance.get("type"),
            })

    total = int(progress.get("linked_task_count") or len(linked))
    done = int(progress.get("done_with_proof_count") or 0)
    # Denominator = tasks that were expected to ship: exclude operator-waived and
    # Cancelled links (a cancelled task legitimately carries no proof).
    denom = max(0, total - waived_linked - cancelled_linked)
    ratio = (done / denom) if denom else 1.0

    checks = [
        _check("no_blockers", not blockers, {"blocker_count": len(blockers)}),
        _check("no_in_review", in_review == 0, {"in_review_count": in_review}),
        _check("no_in_progress", not active, {"active_tasks": active}),
        _check("terminal_or_waived", not non_terminal,
               {"non_terminal_tasks": non_terminal, "waived_count": waived_linked}),
        _check("done_with_proof_ratio", ratio >= min_proof_ratio,
               {"ratio": round(ratio, 4), "min": min_proof_ratio,
                "done_with_proof": done, "denominator": denom}),
    ]
    return {
        "pass": all(c["pass"] for c in checks),
        "checks": checks,
        "blockers": blockers,
        "non_terminal_tasks": non_terminal,
        "waivers": list(waived.values()),
    }


# --- Gate 2: functional -----------------------------------------------------

def _store_check(name: str, params: Dict[str, Any], *, mission_status: Dict[str, Any],
                 project: str) -> Tuple[bool, Dict[str, Any]]:
    """Named pure-store predicates. Unknown names fail closed."""
    progress = mission_status.get("progress") or {}
    if name == "min_done_with_proof_ratio":
        want = float(params.get("min", DEFAULT_MIN_PROOF_RATIO))
        ratio = float(progress.get("done_with_proof_ratio") or 0.0)
        return ratio >= want, {"ratio": round(ratio, 4), "min": want}
    if name == "no_blockers":
        blockers = mission_status.get("blockers") or []
        return not blockers, {"blocker_count": len(blockers)}
    if name == "task_terminal":
        task_id = params.get("task_id")
        task_project = params.get("task_project") or project
        if not task_id:
            raise ClosureError("store_check 'task_terminal' requires params.task_id")
        task = store.get_task(task_id, project=task_project) or {}
        provenance = task.get("provenance") or {}
        ok = task.get("status") == "Done" and bool(provenance.get("terminal"))
        return ok, {"task_id": task_id, "status": task.get("status"),
                    "provenance_type": provenance.get("type")}
    raise ClosureError(f"unknown store_check predicate {name!r}")


def _offline_evidence_gate(gate: Dict[str, Any], *, project: str) -> Tuple[bool, Dict[str, Any]]:
    task_id = gate.get("task_id")
    task_project = gate.get("task_project") or project
    task = store.get_task(task_id, project=task_project) or {}
    provenance = task.get("provenance") or {}
    ok = provenance.get("type") == "offline_evidence" and bool(provenance.get("terminal"))
    return ok, {"task_id": task_id, "task_project": task_project,
                "provenance_type": provenance.get("type"),
                "status": task.get("status")}


def _build_env(allowlist: Optional[List[str]]) -> Dict[str, str]:
    patterns = allowlist or []
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV or any(fnmatch.fnmatch(key, pat) for pat in patterns):
            env[key] = value
    return env


def _run_command(command: List[str], *, timeout_s: float, allowlist: Optional[List[str]],
                 cwd: str) -> Dict[str, Any]:
    began = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 - command comes from the committed gate registry
            command, cwd=cwd, env=_build_env(allowlist),
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"pass": False, "status": "timeout",
                "duration_s": round(time.perf_counter() - began, 3),
                "error": f"timed out after {timeout_s}s"}
    except (OSError, ValueError) as exc:
        return {"pass": False, "status": "error",
                "duration_s": round(time.perf_counter() - began, 3),
                "error": f"{type(exc).__name__}: {exc}"}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "pass": proc.returncode == 0,
        "status": "ran",
        "exit_code": proc.returncode,
        "duration_s": round(time.perf_counter() - began, 3),
        "artifact_hash": _sha256(output),
        # Only keep a failing tail — a green gate does not need its log inlined.
        "output_tail": "" if proc.returncode == 0 else output[-2000:],
    }


def run_gate(gate: Dict[str, Any], *, project: str, mission_status: Dict[str, Any],
             submitted_functional: Optional[Dict[str, Any]] = None,
             run_scripts: bool = False) -> Dict[str, Any]:
    """Execute one resolved functional gate and return its check result."""
    submitted = submitted_functional or {}
    gid = gate.get("id")
    kind = gate.get("kind")
    required = bool(gate.get("required", True))
    result: Dict[str, Any] = {"id": gid, "kind": kind, "required": required,
                              "source": gate.get("source")}

    # An agent-submitted result always wins — the verifier ran the real harness.
    if gid in submitted:
        sub = submitted[gid]
        if not isinstance(sub, dict) or "pass" not in sub:
            raise ClosureError(f"submitted result for {gid!r} needs a boolean 'pass'")
        result.update({"pass": bool(sub.get("pass")), "status": "submitted",
                       "duration_s": sub.get("duration_s"),
                       "artifact_hash": sub.get("artifact_hash"),
                       "detail": {k: v for k, v in sub.items()
                                  if k not in ("pass", "duration_s", "artifact_hash")}})
        return result

    if kind == "store_check":
        try:
            ok, detail = _store_check(gate.get("check"), gate.get("params") or {},
                                      mission_status=mission_status, project=project)
        except ClosureError as exc:
            result.update({"pass": False, "status": "error", "detail": {"error": str(exc)}})
            return result
        result.update({"pass": ok, "status": "checked", "detail": detail})
        return result

    if kind == "offline_evidence":
        ok, detail = _offline_evidence_gate(gate, project=project)
        result.update({"pass": ok, "status": "checked", "detail": detail})
        return result

    if kind in ("script", "pytest"):
        if not run_scripts:
            # Fail-closed: no in-process execution and no agent result means the
            # proof was not produced. Never optimistically pass.
            result.update({"pass": None, "status": "not_run",
                           "detail": {"reason": "requires a verifier agent result "
                                      "or run_scripts=True"}})
            return result
        if kind == "script":
            command = list(gate.get("command") or [])
        else:
            command = ["python3", "-m", "pytest", gate.get("target"), *(gate.get("args") or [])]
        outcome = _run_command(
            command, timeout_s=float(gate.get("timeout_s") or DEFAULT_GATE_TIMEOUT_S),
            allowlist=gate.get("env_allowlist"), cwd=os.path.join(REPO_ROOT, gate.get("cwd") or ""),
        )
        result.update(outcome)
        result["detail"] = {"command": command}
        return result

    result.update({"pass": False, "status": "error",
                   "detail": {"error": f"unrunnable gate kind {kind!r}"}})
    return result


def functional_gate(functional_gates: List[Dict[str, Any]], *, project: str,
                    mission_status: Dict[str, Any],
                    submitted_functional: Optional[Dict[str, Any]] = None,
                    run_scripts: bool = False) -> Dict[str, Any]:
    """Gate 2 — functional. Run every resolved functional gate; a required gate
    that did not pass (fail or not_run) holds the gate closed. Optional gates are
    recorded but never block."""
    checks = [run_gate(gate, project=project, mission_status=mission_status,
                       submitted_functional=submitted_functional, run_scripts=run_scripts)
              for gate in functional_gates]
    required_ok = all(c.get("pass") is True for c in checks if c.get("required"))
    return {
        "pass": required_ok,
        "checks": checks,
        "required_count": sum(1 for c in checks if c.get("required")),
        "not_run_count": sum(1 for c in checks if c.get("status") == "not_run"),
    }


# --- grading + report -------------------------------------------------------

def _grade(scope: Dict[str, Any], functional: Dict[str, Any],
           waivers: Optional[List[Dict[str, Any]]]) -> str:
    if scope.get("pass") and functional.get("pass"):
        return "waive" if waivers else "pass"
    return "hold"


def _acceptance_results(acceptance_criteria: Optional[List[str]],
                        provided: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if provided is not None:
        return [dict(item) for item in provided]
    # Free-text criteria are not machine-graded here; list them unassessed rather
    # than optimistically passing them.
    return [{"criterion": c, "pass": None, "evidence": []}
            for c in (acceptance_criteria or [])]


def verify_deliverable_closure(
    deliverable_id: str,
    project: str,
    *,
    waivers: Optional[List[Dict[str, Any]]] = None,
    submitted_functional: Optional[Dict[str, Any]] = None,
    acceptance_criteria_results: Optional[List[Dict[str, Any]]] = None,
    run_scripts: bool = False,
    generated_by: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run scope + functional gates for a deliverable and return a graded
    ``switchboard.deliverable_closure_report.v1``. Does not persist (that is
    DELIVERABLES-16). Raises :class:`ClosureError` on a missing deliverable."""
    deliverable = store.get_deliverable(deliverable_id, project=project)
    if not deliverable:
        raise ClosureError(f"deliverable {deliverable_id!r} not found on project {project!r}")

    mission_status = store.get_mission_status(project=project, deliverable_id=deliverable_id)
    if isinstance(mission_status, dict) and mission_status.get("error"):
        raise ClosureError(f"mission status unavailable: {mission_status.get('error')}")

    proof_requirements = deliverable.get("proof_requirements") or {}
    min_ratio = float((proof_requirements.get("done_with_proof_ratio")
                       if isinstance(proof_requirements, dict) else None)
                      or DEFAULT_MIN_PROOF_RATIO)

    try:
        resolved = deliverable_gates.resolve_gates(proof_requirements)
    except deliverable_gates.GateResolutionError as exc:
        raise ClosureError(f"proof_requirements.gates is invalid: {exc}") from exc
    _, functional_specs = deliverable_gates.partition_gates(resolved)

    scope = scope_gate(mission_status, waivers, min_proof_ratio=min_ratio)
    functional = functional_gate(functional_specs, project=project,
                                 mission_status=mission_status,
                                 submitted_functional=submitted_functional,
                                 run_scripts=run_scripts)

    grade = _grade(scope, functional, waivers)
    warnings: List[str] = []
    if not functional_specs:
        warnings.append("no functional gates in proof_requirements — graded on scope only")

    report = {
        "schema": CLOSURE_REPORT_SCHEMA,
        "deliverable_id": deliverable_id,
        "project_id": project,
        "generated_at": float(now if now is not None else time.time()),
        "generated_by": generated_by,
        "grade": grade,
        "gates": {"scope": scope, "functional": functional},
        "acceptance_criteria_results": _acceptance_results(
            deliverable.get("acceptance_criteria"), acceptance_criteria_results),
        "waivers": list(_waiver_index(waivers).values()),
        "warnings": warnings,
        "recommendation": "safe_to_mark_done" if grade in ("pass", "waive") else "hold",
    }
    report["evidence_hash"] = _evidence_hash(report)
    return report


# Fields excluded from the evidence hash so it attests to the verification
# *verdict* (grades, gate ids, pass/fail, output hashes) rather than wall-clock
# noise — two verifications of identical state produce the same hash.
_VOLATILE_HASH_KEYS = frozenset(
    {"generated_at", "generated_by", "evidence_hash", "duration_s", "output_tail"})


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in _VOLATILE_HASH_KEYS}
    if isinstance(obj, list):
        return [_scrub(item) for item in obj]
    return obj


def _evidence_hash(report: Dict[str, Any]) -> str:
    return _sha256(json.dumps(_scrub(report), sort_keys=True, default=str))


# --- verify + persist (DELIVERABLES-16 orchestrator) ------------------------

def verify_and_record_closure(
    deliverable_id: str,
    project: str,
    *,
    actor: str = "verifier",
    report: Optional[Dict[str, Any]] = None,
    submitted_functional: Optional[Dict[str, Any]] = None,
    waivers: Optional[List[Dict[str, Any]]] = None,
    acceptance_criteria_results: Optional[List[Dict[str, Any]]] = None,
    generated_by: str = "",
    run_scripts: bool = False,
) -> Dict[str, Any]:
    """Run (or accept) a closure verification and persist it via :mod:`store`.

    With no ``report``, the engine runs scope + functional gates (passing any
    ``submitted_functional`` verifier results; ``run_scripts`` stays off on the
    server path). A supplied ``report`` is treated as an agent-submitted
    ``switchboard.deliverable_closure_report.v1`` and persisted as-is. Returns
    ``store.record_deliverable_closure``'s result, or ``{"error": ...}`` on a
    closure error (missing deliverable, malformed proof_requirements, or a
    malformed submitted report) — mirroring the store's error convention so the
    MCP/REST callers surface it uniformly.
    """
    try:
        if report is None:
            report = verify_deliverable_closure(
                deliverable_id, project, waivers=waivers,
                submitted_functional=submitted_functional,
                acceptance_criteria_results=acceptance_criteria_results,
                generated_by=generated_by or actor, run_scripts=run_scripts)
        elif not isinstance(report, dict) or report.get("schema") != CLOSURE_REPORT_SCHEMA:
            raise ClosureError(
                f"submitted report must be a {CLOSURE_REPORT_SCHEMA} object")
    except ClosureError as exc:
        return {"error": str(exc), "deliverable_id": deliverable_id}
    return store.record_deliverable_closure(deliverable_id, report, actor=actor, project=project)


# --- verifier dispatch (DELIVERABLES-17) ------------------------------------

#: Signal stamped on the closure dispatch's wake + directed message so a verifier
#: (and the mission UI) can recognise a closure-verification hand-off.
CLOSURE_VERIFICATION_SIGNAL = "deliverable.closure_verification"

#: Runtime the verifier agent runs on. Same fleet runtime as task dispatch.
_VERIFIER_RUNTIME = "claude-code"


def _work_hosts_online(project: str) -> int:
    """Count work-capable hosts for ``project`` so the caller can say "queued,
    waiting for a work host" instead of implying the verifier started. Reuses the
    task-dispatch host detection; any error is a defensive 0 (never a false ready)."""
    try:
        import dispatch  # lazy: dispatch imports store, avoid an import cycle at module load
        return int(dispatch.status(project).get("work_hosts_online") or 0)
    except Exception:
        return 0


def gate_manifest(resolved: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact, prompt-friendly view of a deliverable's resolved gates: identity and
    how each is proven, without the command/env internals the verifier re-resolves.
    ``runs_in_agent`` flags the command gates (script/pytest) the verifier must
    execute itself — store/offline/scope kinds are graded server-side."""
    manifest: List[Dict[str, Any]] = []
    for gate in resolved:
        item = {"id": gate.get("id"), "kind": gate.get("kind"),
                "required": bool(gate.get("required", True)),
                "source": gate.get("source"),
                "runs_in_agent": gate.get("kind") in ("script", "pytest")}
        for key in ("title", "description", "target", "task_id", "check"):
            if gate.get(key):
                item[key] = gate[key]
        manifest.append(item)
    return manifest


def build_closure_prompt(deliverable: Dict[str, Any], gates: List[Dict[str, Any]],
                         *, project: str,
                         waivers: Optional[List[Dict[str, Any]]] = None) -> str:
    """The closure-verifier prompt template (docs/DELIVERABLE-CLOSURE-GATE.md §DELIVERABLES-17).

    Self-contained: it names the deliverable, its end state and acceptance criteria,
    the resolved gate list, and the exact ``verify_deliverable_closure`` call the
    verifier ends on. It explicitly does NOT authorise marking the deliverable
    done — that stays with the operator/webhook path."""
    deliverable_id = deliverable.get("id")
    lines = [
        f"You are the closure verifier for deliverable {deliverable_id!r} on project "
        f"{project!r}. Run its closure gates and record a graded "
        f"{CLOSURE_REPORT_SCHEMA}. Do NOT set the deliverable status=done — that is "
        "the operator/webhook path.",
        "",
        f"End state: {deliverable.get('end_state') or '(none recorded)'}",
        "",
        "Acceptance criteria:",
    ]
    criteria = deliverable.get("acceptance_criteria") or []
    lines += ([f"  {i}. {c}" for i, c in enumerate(criteria, 1)] or ["  (none recorded)"])
    lines += ["", "Gates to verify (resolved from proof_requirements):"]
    for gate in gates:
        how = "run it yourself" if gate.get("runs_in_agent") else "graded server-side"
        req = "required" if gate.get("required") else "optional"
        lines.append(f"  - {gate.get('id')} [{gate.get('kind')}, {req}] — {how}")
    if waivers:
        lines += ["", "Operator waivers to apply:"]
        lines += [f"  - {w.get('task_id')}: {w.get('reason')}"
                  for w in waivers if isinstance(w, dict)]
    run_ids = [g["id"] for g in gates if g.get("runs_in_agent")]
    lines += [
        "",
        "Steps:",
        f"  1. prepare_agent_session(project={project!r}, deliverable_id={deliverable_id!r})",
        "  2. get_deliverable + get_mission_status for full context and linked-task provenance",
        "  3. Run each command gate above (kind=script/pytest); capture pass/fail, duration_s, "
        "and an artifact hash of its output",
        "  4. Collect proof pointers (linked-task merge/offline provenance, artifact hashes)",
        "  5. Call verify_deliverable_closure(deliverable_id, project, "
        "submitted_functional_json={gate_id: {pass, duration_s?, artifact_hash?}}"
        + (", waivers_json=[...]" if waivers else "") + ") to grade and persist the report",
        "  6. Post a summary comment on the deliverable; leave status=done to the operator.",
        "",
        ("Command gates you must execute: " + ", ".join(run_ids)) if run_ids else
        ("No command gates need agent execution — the server grades scope + store/offline "
         "gates; call verify_deliverable_closure to record the report."),
    ]
    return "\n".join(lines)


def request_closure_verification(
    deliverable_id: str,
    project: str,
    *,
    agent_id: str = "",
    actor: str = "operator",
    waivers: Optional[List[Dict[str, Any]]] = None,
    idem_key: str = "",
) -> Dict[str, Any]:
    """Operator "Verify & stamp closure" dispatch (DELIVERABLES-17).

    Assembles the deliverable's context, its resolved scope+functional gate list,
    and a closure prompt template, then dispatches a verifier agent: a directed
    inbox message carries the prompt/context and a lane-less ``message_only`` wake
    rouses the verifier to drain it (never a claim_next task grab — see
    ``adapters/agent_host.wake_mode``). The verifier runs the gates and records a
    graded report through :func:`verify_and_record_closure` (DELIVERABLES-16); it
    never marks the deliverable done.

    Returns a dispatch record, or ``{"error": ...}`` (missing deliverable, malformed
    ``proof_requirements``, or invalid waivers) mirroring the store's error
    convention. Servicing needs a work-capable host online for the runtime;
    otherwise the wake queues (``work_hosts_online``/``queued`` surface that),
    exactly as :func:`dispatch.dispatch` does for tasks."""
    deliverable = store.get_deliverable(deliverable_id, project=project)
    if not deliverable:
        return {"error": f"deliverable {deliverable_id!r} not found on project {project!r}",
                "deliverable_id": deliverable_id}

    try:
        resolved = deliverable_gates.resolve_gates(
            deliverable.get("proof_requirements"), include_scope=True)
    except deliverable_gates.GateResolutionError as exc:
        return {"error": f"proof_requirements.gates is invalid: {exc}",
                "deliverable_id": deliverable_id}

    try:
        waiver_list = list(_waiver_index(waivers).values())
    except ClosureError as exc:
        return {"error": str(exc), "deliverable_id": deliverable_id}

    gates = gate_manifest(resolved)
    prompt = build_closure_prompt(deliverable, gates, project=project, waivers=waiver_list)
    verifier_agent = (agent_id or "").strip() or f"verifier/closure/{deliverable_id}"
    # Idempotency dedupes a double-clicked button. It keys on the target agent so
    # re-dispatching the same deliverable to a *different* verifier is a distinct
    # intent, not an idempotency conflict (_idem_hit rejects a key reused with a
    # different payload). Default target is stable per deliverable, so a repeat is a
    # clean hit that returns the same wake/message.
    base_idem = (idem_key or "").strip() or f"closure-verify:{project}:{deliverable_id}:{verifier_agent}"

    # 1. Deliver the prompt + context to the verifier's mailbox. It is stored even if
    #    no agent is registered under that id yet (mailbox_stored); the verifier drains
    #    it on wake. requires_ack stays off so a queued dispatch raises no ack-timeout.
    message = store.send_agent_message(
        from_agent="switchboard/closure", to_agent=verifier_agent, message=prompt,
        requires_ack=False, signal=CLOSURE_VERIFICATION_SIGNAL, priority=1,
        project=project, idem_key=f"{base_idem}:msg")
    message_id = message.get("id")

    # 2. Rouse the verifier. Lane-less + mode=message_only => the Agent Host launches it
    #    inbox-only, so an unknown "closure_verification" job never becomes a task claim.
    gate_ids = [g["id"] for g in gates]
    wake = store.request_wake(
        selector={"runtime": _VERIFIER_RUNTIME, "agent_id": verifier_agent},
        reason=f"Verify & stamp closure for {deliverable_id}",
        source=f"ui:{actor}",
        policy={"mode": "message_only", "kind": "closure_verification",
                "deliverable_id": deliverable_id, "project": project,
                "gate_ids": gate_ids, "message_id": message_id, "waivers": waiver_list},
        actor=actor, project=project, idem_key=base_idem)
    wake_id = wake.get("wake_id")
    if wake.get("error") or not wake_id:
        return {"dispatched": False, "deliverable_id": deliverable_id, "project": project,
                "agent_id": verifier_agent, "message_id": message_id,
                "error": wake.get("error") or wake.get("reason") or "wake not created"}

    # 3. Stamp the deliverable activity feed (task_id=None => deliverable-scoped) so the
    #    dispatch is legible on the mission header (DELIVERABLES-18 reads this).
    store.append_activity(
        "deliverable.closure_verification_requested", actor,
        {"deliverable_id": deliverable_id, "project": project, "agent_id": verifier_agent,
         "wake_id": wake_id, "message_id": message_id, "gate_ids": gate_ids},
        project=project)

    hosts = _work_hosts_online(project)
    return {
        "dispatched": True, "deliverable_id": deliverable_id, "project": project,
        "agent_id": verifier_agent, "wake_id": wake_id, "wake_status": wake.get("status"),
        "message_id": message_id, "delivery_status": message.get("delivery_status"),
        "signal": CLOSURE_VERIFICATION_SIGNAL, "gates": gates, "prompt": prompt,
        "work_hosts_online": hosts, "queued": hosts == 0,
    }
