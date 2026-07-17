"""Project-owned UI validation policy and exact-head Playwright evidence gates.

ARCH-MS-126 makes UI validation a task contract instead of an agent convention.
The policy is project-global, while runner commands remain project-specific.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


VALIDATION_POLICY_SCHEMA = "switchboard.validation_policy.v1"
UI_PLAYWRIGHT_KIND = "ui_playwright"
UI_CONTEXT = "Switchboard UI / Playwright"

_ROOT = Path(__file__).resolve().parents[3]
_POLICY_PATH = _ROOT / "deploy" / "validation-policy.json"
_ENDPOINTS_PATH = _ROOT / "deploy" / "ui-consumed-endpoints.json"

_UI_PATH_PREFIXES = (
    "static/", "templates/", "src/switchboard/api/routers/auth/",
    "src/switchboard/api/middleware.py", "src/switchboard/api/browser_session.py",
    "deploy/caddyfile", "tests/browser/",
)
_UI_PATH_NAMES = {"app_impl.py", "caddyfile"}
_UI_TEXT_TOKENS = (
    "frontend", "browser", "playwright", "chromium", "cookie", "browser session",
    "session cookie",
    "authentication", " auth", "caddy", "routing", "route", "csp",
    "websocket", "template", "stylesheet", "css", "javascript", " ui ",
    "browser-visible", "api contract", "service cut", "service-cut", "deploy",
)
_CODE_TEXT_TOKENS = (
    "session_profile:code_strict", "code", "repository", "merge", "pull request",
    " pr ", "ci", "test", "script", "api", "service", "deploy", "implementation",
)
_LIVE_EDGE_TOKENS = (
    "authentication", " auth", "cookie", "browser session", "session cookie", "caddy", "routing",
    "service cut", "service-cut", "deploy",
)


def _load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(fallback)
    return value if isinstance(value, dict) else dict(fallback)


def project_validation_policy(project: str) -> dict[str, Any]:
    """Return the effective policy; non-Switchboard projects keep local commands."""
    default = {
        "schema": VALIDATION_POLICY_SCHEMA,
        "project": project,
        "classification_required": project == "switchboard",
        "allowed_ui_impact": ["yes", "no"],
        "required_status_context": UI_CONTEXT,
        "runner": {
            "command": "python3 scripts/run_ui_playwright.py",
            "browser": "chromium", "headless": True, "allow_skip": False,
        },
    }
    policy = _load_json(_POLICY_PATH, default) if project == "switchboard" else default
    policy["project"] = project
    return policy


def ui_consumed_endpoint_manifest() -> dict[str, Any]:
    return _load_json(_ENDPOINTS_PATH, {
        "schema": "switchboard.ui_consumed_endpoints.v1", "endpoints": []})


def _text(payload: Mapping[str, Any]) -> str:
    return " ".join(str(payload.get(key) or "") for key in (
        "title", "description", "phase", "entry_criteria", "exit_criteria",
        "deliverable", "workstream_id", "_wsId",
    )).lower()


def _normalized_files(changed_files: Iterable[Any] | None) -> list[str]:
    return sorted({str(item or "").strip().lower().lstrip("./")
                   for item in (changed_files or []) if str(item or "").strip()})


def infer_ui_impact(payload: Mapping[str, Any],
                    changed_files: Iterable[Any] | None = None) -> dict[str, Any]:
    text = f" {_text(payload)} "
    files = _normalized_files(changed_files)
    reasons: list[str] = []
    for path in files:
        if (path in _UI_PATH_NAMES
                or any(path.startswith(prefix) for prefix in _UI_PATH_PREFIXES)
                or path.endswith((".html", ".css", ".js", ".tsx", ".jsx"))):
            reasons.append(f"ui_path:{path}")
    for token in _UI_TEXT_TOKENS:
        if token in text:
            reasons.append(f"ui_signal:{token.strip()}")
    endpoints = ui_consumed_endpoint_manifest().get("endpoints") or []
    if files and any(path.startswith("src/switchboard/api/") for path in files):
        reasons.append(f"ui_consumed_api_manifest:{len(endpoints)}")
    return {"ui": bool(reasons), "reasons": sorted(set(reasons)), "changed_files": files}


def _looks_like_code(payload: Mapping[str, Any]) -> bool:
    text = f" {_text(payload)} "
    return any(token in text for token in _CODE_TEXT_TOKENS)


def validation_requirement(ui_impact: str, reasons: Iterable[str] = (),
                           payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if ui_impact != "yes":
        return {"required": False}
    text = f" {_text(payload or {})} "
    live_edge = any(token in text for token in _LIVE_EDGE_TOKENS)
    return {
        "required": True,
        "runner": "playwright",
        "browser": "chromium",
        "headless": True,
        "tier": "hermetic",
        "post_deploy_tier": "live_edge" if live_edge else None,
        "allow_skip": False,
        "required_status_context": UI_CONTEXT,
        "reasons": sorted(set(str(item) for item in reasons)),
    }


def classify_task(payload: Mapping[str, Any], *, project: str,
                  existing: Mapping[str, Any] | None = None,
                  changed_files: Iterable[Any] | None = None,
                  material_rescope: bool = False) -> dict[str, Any]:
    """Classify a task, upgrading false ``no`` declarations when reality says UI."""
    current = dict(existing or {})
    explicit = str(payload.get("ui_impact") or "").strip().lower()
    if not explicit:
        explicit = str(current.get("ui_impact") or "").strip().lower()
    inferred = infer_ui_impact({**current, **dict(payload)}, changed_files)
    reasons = list(inferred["reasons"])
    source = "explicit" if explicit in {"yes", "no"} else "inferred"
    if explicit and explicit not in {"yes", "no"}:
        return {"ok": False, "error": "invalid_ui_impact",
                "message": "ui_impact must be yes or no", "ui_impact": explicit}
    intake_only = (
        "triage" in str(payload.get("status") or current.get("status") or "").lower()
        or "intake" in str(payload.get("phase") or current.get("phase") or "").lower()
    )
    if intake_only and not explicit:
        impact = "no"
        source = "deterministic_intake"
        reasons.append("intake_requires_classification_on_implementation_conversion")
    elif inferred["ui"]:
        impact = "yes"
        if explicit == "no":
            source = "upgraded_from_false_no"
            reasons.append("declared_no_overridden_by_inference")
    elif explicit:
        impact = explicit
    elif (project == "switchboard"
          and _looks_like_code({**current, **dict(payload)})
          and material_rescope):
        return {
            "ok": False, "error": "ui_impact_required",
            "message": (
                "New or materially re-scoped code tasks must declare ui_impact=yes|no; "
                "ambiguous code work fails closed."
            ),
            "material_rescope": material_rescope,
        }
    else:
        impact = "no"
        source = ("legacy_repository_default"
                  if project == "switchboard" and _looks_like_code({**current, **dict(payload)})
                  else "deterministic_non_code")
        reasons.append("classification_enforced_at_scoping_boundary"
                       if source == "legacy_repository_default"
                       else "no_code_or_ui_signal")
    requirement = validation_requirement(impact, reasons, {**current, **dict(payload)})
    return {
        "ok": True, "schema": VALIDATION_POLICY_SCHEMA, "project": project,
        "ui_impact": impact, "classification_source": source,
        "ui_validation": requirement, "reasons": sorted(set(reasons)),
        "classified_at": time.time(),
    }


def task_validation(task: Mapping[str, Any] | None, project: str) -> dict[str, Any]:
    task = dict(task or {})
    stored = ((task.get("agent_state") or {}).get("validation_policy") or {})
    if stored.get("schema") == VALIDATION_POLICY_SCHEMA:
        return dict(stored)
    return classify_task(task, project=project, existing=task)


def _run_candidates(evidence: Mapping[str, Any], session: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, container in (("evidence", evidence),
                              ("hygiene", (session or {}).get("hygiene") or {})):
        for key in ("executed_test_run", "executed_test_runs", "test_run", "test_runs"):
            value = container.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    rows.append({**item, "_source": f"{source}.{key}"})
    return rows


def ui_playwright_evidence_gate(task: Mapping[str, Any], evidence: Mapping[str, Any],
                                session: Mapping[str, Any] | None,
                                *, project: str, head_sha: str = "",
                                changed_files: Iterable[Any] | None = None) -> dict[str, Any]:
    classification = classify_task(task, project=project, existing=task,
                                   changed_files=changed_files)
    if not classification.get("ok"):
        return {**classification, "required": True}
    if classification.get("ui_impact") != "yes":
        return {"ok": True, "required": False, "classification": classification}
    waiver = evidence.get("ui_validation_waiver") or {}
    if waiver:
        required = ("approved_by", "approved_at", "reason", "alternative_evidence",
                    "task_id", "expires_at")
        missing = [key for key in required if not waiver.get(key)]
        try:
            expired = float(waiver.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            expired = True
        if not missing and not expired and waiver.get("task_id") == task.get("task_id"):
            return {"ok": True, "required": True, "waived": True,
                    "classification": classification, "waiver": waiver}
        return {"ok": False, "required": True, "reason": "invalid_ui_validation_waiver",
                "message": "UI validation waiver is incomplete, expired, or not task-scoped.",
                "missing": missing, "classification": classification}
    expected_head = str(head_sha or (session or {}).get("head_sha") or "").strip()
    expected_branch = str((session or {}).get("branch") or "").strip()
    expected_session = str((session or {}).get("work_session_id") or "").strip()
    problems: list[dict[str, Any]] = []
    for run in _run_candidates(evidence, session):
        if str(run.get("test_kind") or "") != UI_PLAYWRIGHT_KIND:
            continue
        run_problems: list[str] = []
        executed = int(run.get("executed_count") or 0)
        skipped = int(run.get("skipped_count") or 0)
        console_errors = run.get("console_errors") or []
        failed_requests = run.get("failed_requests") or []
        if run.get("executed") is False or executed <= 0:
            run_problems.append("zero_executed_tests")
        if run.get("skipped") is True or skipped != 0:
            run_problems.append("unexpected_skips")
        if str(run.get("browser") or "chromium").lower() != "chromium" or not run.get("chromium_version"):
            run_problems.append("missing_chromium_proof")
        if run.get("headless") is not True or str(run.get("tier") or "") != "hermetic":
            run_problems.append("wrong_playwright_mode")
        if not str(run.get("base_url") or "").strip():
            run_problems.append("missing_base_url")
        if console_errors or int(run.get("console_error_count") or 0):
            run_problems.append("console_errors")
        if failed_requests or int(run.get("failed_request_count") or 0):
            run_problems.append("failed_requests")
        if not (run.get("trace_hash") or run.get("screenshot_hash") or run.get("artifact_hash")):
            run_problems.append("missing_browser_artifact_hash")
        for key, expected in (("task_id", task.get("task_id")),
                              ("work_session_id", expected_session),
                              ("branch", expected_branch), ("head_sha", expected_head)):
            if expected and str(run.get(key) or "") != str(expected):
                run_problems.append(f"mismatched_{key}")
        if not run_problems:
            clean = {key: value for key, value in run.items() if key != "_source"}
            return {"ok": True, "required": True, "waived": False,
                    "source": run["_source"], "run": clean,
                    "classification": classification}
        problems.append({"source": run["_source"], "problems": run_problems})
    return {
        "ok": False, "required": True, "reason": "missing_ui_playwright_evidence",
        "message": (
            "UI-impacting work requires successful exact-head CLI Playwright evidence; "
            "missing Chromium, zero tests, skips, console errors, and failed requests are red."
        ),
        "problems": problems, "classification": classification,
    }


def policy_artifact_hash(project: str = "switchboard") -> str:
    payload = {"policy": project_validation_policy(project),
               "endpoints": ui_consumed_endpoint_manifest()}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
