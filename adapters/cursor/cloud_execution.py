#!/usr/bin/env python3
"""Cursor Cloud Agents v1 transport for the shared ADAPTER-17 contract.

The adapter keeps provider credentials out of the dispatch envelope, verifies Cursor
account/repository access before launch, starts from an already-pushed task branch,
and adopts a run only after reading back Cursor's stable agent id and app URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from adapters.cloud_execution import (
    CANONICAL_REPO,
    evaluate_trigger,
    load_contract,
    validate_dispatch_envelope,
    validate_usage_receipt,
)


VENDOR_ID = "cursor-background-agent"
API_BASE = "https://api.cursor.com"
REPOSITORY_URL = f"https://github.com/{CANONICAL_REPO}"
MCP_ENDPOINT = "https://plan.taikunai.com/mcp"
MAX_MCP_TOKEN_TTL_S = 6 * 60 * 60
CONTINUITY_CAPABILITIES = {
    "fresh_dispatch": True,
    "same_run_resume": False,
    "same_agent_follow_up": True,
    "follow_up_operation": "POST /v1/agents/{id}/runs",
    "reason": (
        "Cursor v1 can add a new run to the same durable agent conversation, but it cannot "
        "resume a cancelled/terminal run in place. Switchboard records follow-up, not exact resume."
    ),
}


class CursorAPIError(RuntimeError):
    """Provider error with a deliberately small, secret-safe detail surface."""

    def __init__(self, status: int, payload: Any):
        self.status = int(status)
        self.payload = payload if isinstance(payload, dict) else {}
        detail = self.payload.get("message") or self.payload.get("error") or f"HTTP {status}"
        super().__init__(str(detail))

    def safe_detail(self) -> dict[str, Any]:
        # Provider messages can echo rejected request fields. Keep only identifier-like
        # codes so an MCP Authorization header can never leak into Switchboard evidence.
        detail: dict[str, Any] = {}
        code = self.payload.get("code")
        if isinstance(code, (int, float)):
            detail["code"] = code
        elif isinstance(code, str) and code.replace("_", "").replace("-", "").isalnum():
            detail["code"] = code[:80]
        return detail


class CursorHTTPClient:
    """Small stdlib JSON client using Cursor v1's documented basic authentication."""

    def __init__(self, api_key: str, *, api_base: str = API_BASE, timeout: float = 30.0):
        self.api_key = str(api_key or "")
        self.api_base = api_base.rstrip("/")
        self.timeout = float(timeout)

    def request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        if not self.api_key:
            raise CursorAPIError(401, {"error": "cursor_api_key_missing"})
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        headers = {"Accept": "application/json", "Authorization": f"Basic {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.api_base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                error_payload = json.loads(raw.decode()) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            raise CursorAPIError(exc.code, error_payload) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CursorAPIError(503, {"error": "cursor_api_unreachable"}) from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorAPIError(502, {"error": "cursor_response_malformed"}) from exc
        if not isinstance(decoded, dict):
            raise CursorAPIError(502, {"error": "cursor_response_not_object"})
        return decoded


def git_remote_branch_exists(repo_url: str, branch: str, *, timeout: float = 20.0) -> bool:
    """Prove the exact provider starting branch is already visible on the remote."""
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", repo_url, branch],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _deny(reason: str, failure_class: str, **detail: Any) -> dict[str, Any]:
    return {
        "allowed": False,
        "adopted": False,
        "dev_status": "failed",
        "reason": reason,
        "failure_class": failure_class,
        **detail,
    }


def _repo_key(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parsed = urlparse(text if "://" in text else "https://" + text)
    path = parsed.path.strip("/")
    return path.lower()


def _repository_urls(payload: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    items = payload.get("items") or payload.get("repositories") or payload.get("repos") or []
    if isinstance(items, dict):
        items = list(items.values())
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            values.add(_repo_key(item))
        elif isinstance(item, dict):
            for key in ("url", "repository", "repoUrl", "cloneUrl", "htmlUrl"):
                if item.get(key):
                    values.add(_repo_key(str(item[key])))
    return {value for value in values if value}


def _provider_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "creating": "provisioning",
        "pending": "queued",
        "queued": "queued",
        "active": "provisioning",
        "running": "running",
        "finished": "completed",
        "completed": "completed",
        "error": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "expired": "expired",
        "archived": "completed",
    }.get(normalized, normalized)


def _pr_url(agent: dict[str, Any], run: dict[str, Any]) -> str:
    for container in (run, agent):
        for key in ("prUrl", "pr_url"):
            if container.get(key):
                return str(container[key])
        git = container.get("git") or {}
        for branch in git.get("branches") or [] if isinstance(git, dict) else []:
            if isinstance(branch, dict) and (branch.get("prUrl") or branch.get("pr_url")):
                return str(branch.get("prUrl") or branch.get("pr_url"))
    return ""


class CursorCloudExecutionAdapter:
    """Fail-closed Cursor Cloud Agents v1 implementation."""

    vendor_id = VENDOR_ID
    continuity = CONTINUITY_CAPABILITIES

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        resolve_mcp_token: Callable[[str], str] | None = None,
        branch_probe: Callable[[str, str], bool] | None = None,
        contract: dict[str, Any] | None = None,
    ):
        self.api_key = str(api_key if api_key is not None else os.getenv("CURSOR_API_KEY", ""))
        self.client = client or CursorHTTPClient(self.api_key)
        self.resolve_mcp_token = resolve_mcp_token or (lambda _ref: "")
        self.branch_probe = branch_probe or git_remote_branch_exists
        self.contract = contract or load_contract()
        self.vendor = next(v for v in self.contract["vendors"] if v["id"] == self.vendor_id)

    def capabilities(self) -> dict[str, Any]:
        return {"vendor_id": self.vendor_id, **self.continuity}

    def _token(self, dispatch: dict[str, Any]) -> str:
        token_ref = str((dispatch.get("mcp_access") or {}).get("token_ref") or "")
        try:
            return str(self.resolve_mcp_token(token_ref) or "")
        except Exception:
            return ""

    def _active_sessions(self, payload: dict[str, Any]) -> tuple[int, bool]:
        items = payload.get("items") or payload.get("agents") or []
        if not isinstance(items, list):
            return 0, False
        active = sum(
            1 for item in items
            if isinstance(item, dict) and str(item.get("status") or "").upper() == "ACTIVE"
        )
        pagination_incomplete = bool(payload.get("nextPageToken") or payload.get("nextCursor"))
        return active, not pagination_incomplete

    def preflight(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        errors = validate_dispatch_envelope(dispatch)
        if errors:
            return _deny("invalid_dispatch_envelope", "invalid_input", errors=errors)
        continuity = str(dispatch.get("continuity") or "fresh_only")
        if continuity not in {"fresh_only", "fresh"}:
            return _deny(
                "same_run_resume_unsupported",
                "invalid_input",
                requested_continuity=continuity,
                capabilities=self.capabilities(),
            )
        task_id = str(dispatch.get("task_id") or "").lower()
        branch = str(dispatch.get("branch") or "")
        if not branch.startswith(("cursor/", "codex/", "claude/")) or task_id not in branch.lower():
            return _deny("cursor_task_branch_required", "stale_branch", branch=branch)
        mcp_access = dispatch.get("mcp_access") or {}
        if mcp_access.get("endpoint") != MCP_ENDPOINT:
            return _deny("untrusted_mcp_endpoint", "invalid_input")
        try:
            token_ttl = float(mcp_access.get("expires_at")) - time.time()
        except (TypeError, ValueError):
            token_ttl = -1
        if token_ttl <= 0 or token_ttl > MAX_MCP_TOKEN_TTL_S:
            return _deny("scoped_mcp_token_expiry_invalid", "absent_permission")
        if not self.api_key:
            return _deny("missing_provider_setup", "absent_permission", missing=["cursor_api_key"])
        if not self._token(dispatch):
            return _deny(
                "missing_provider_setup", "absent_permission", missing=["scoped_mcp_token_ref"]
            )
        if not self.branch_probe(REPOSITORY_URL, branch):
            return _deny(
                "pushed_task_branch_missing",
                "stale_branch",
                repository=CANONICAL_REPO,
                branch=branch,
            )
        try:
            identity = self.client.request("GET", "/v1/me")
            repositories = self.client.request("GET", "/v1/repositories?limit=100")
            agents = self.client.request("GET", "/v1/agents?limit=100")
        except CursorAPIError as exc:
            return _deny(
                "provider_preflight_failed",
                "absent_permission" if exc.status in {401, 403} else "broken_connection",
                provider_status=exc.status,
                provider_error=exc.safe_detail(),
            )
        if not identity:
            return _deny("provider_identity_missing", "missing_data")
        if _repo_key(REPOSITORY_URL) not in _repository_urls(repositories):
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                missing=["github_repo_grant"],
                repository=CANONICAL_REPO,
            )
        active_sessions, complete_count = self._active_sessions(agents)
        if not complete_count:
            return _deny("provider_session_count_incomplete", "missing_data")
        ready = evaluate_trigger(
            self.vendor_id,
            dispatch,
            self.vendor["requirements"],
            active_sessions,
            contract=self.contract,
        )
        if ready.get("allowed"):
            ready.update({"provider_identity_verified": True, "repository_grant_verified": True})
        return ready

    def _agent_id(self, dispatch: dict[str, Any]) -> str:
        seed = ":".join(
            (str(dispatch.get(key) or "") for key in ("project", "task_id", "wake_id"))
        )
        return "bc-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"switchboard:{self.vendor_id}:{seed}"))

    def _request_body(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        token = self._token(dispatch)
        brief = str(dispatch["dev_brief"]).strip()
        prompt = (
            f"{brief}\n\n"
            f"Switchboard assignment: project={dispatch['project']} task={dispatch['task_id']} "
            f"wake={dispatch['wake_id']}. Work only in {CANONICAL_REPO} on the already-pushed "
            f"branch {dispatch['branch']}. Never write main/master. Use taikun-plan for the "
            "session handshake, claim/evidence, and open a PR when tests pass."
        )
        return {
            "agentId": self._agent_id(dispatch),
            "prompt": {"text": prompt},
            "repos": [{"url": REPOSITORY_URL, "startingRef": dispatch["branch"]}],
            "workOnCurrentBranch": True,
            "autoCreatePR": True,
            "skipReviewerRequest": True,
            "conversationMode": "agent",
            "mcpServers": [
                {
                    "name": "taikun-plan",
                    "type": "http",
                    "url": dispatch["mcp_access"]["endpoint"],
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            ],
        }

    def trigger(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        ready = self.preflight(dispatch)
        if not ready.get("allowed"):
            return ready
        agent_id = self._agent_id(dispatch)
        try:
            created = self.client.request("POST", "/v1/agents", self._request_body(dispatch))
        except CursorAPIError as exc:
            if exc.status != 409:
                return _deny(
                    "vendor_api_error",
                    "broken_connection",
                    provider_status=exc.status,
                    provider_error=exc.safe_detail(),
                )
            created = {"agent": {"id": agent_id}, "idempotent_replay": True}
        returned_agent = created.get("agent") if isinstance(created.get("agent"), dict) else created
        returned_id = str((returned_agent or {}).get("id") or "")
        if returned_id and returned_id != agent_id:
            return _deny("provider_agent_id_mismatch", "invalid_input")
        readback = self.get_session(agent_id)
        if not readback.get("ok"):
            return readback
        receipt = evaluate_trigger(
            self.vendor_id,
            dispatch,
            self.vendor["requirements"],
            int(ready.get("active_sessions") or 0),
            provider_result=readback,
            contract=self.contract,
        )
        if receipt.get("allowed"):
            receipt.update(
                {
                    "provider_run_id": readback.get("run_id"),
                    "pr_url": readback.get("pr_url") or None,
                    "continuity": self.capabilities(),
                    "idempotent_replay": bool(created.get("idempotent_replay")),
                }
            )
        return receipt

    def get_session(self, provider_session_id: str) -> dict[str, Any]:
        session_id = str(provider_session_id or "").strip()
        if not session_id:
            return _deny("provider_session_id_missing", "missing_data")
        safe_id = quote(session_id, safe="")
        try:
            agent = self.client.request("GET", f"/v1/agents/{safe_id}")
            run_id = str(agent.get("latestRunId") or "")
            run = (
                self.client.request("GET", f"/v1/agents/{safe_id}/runs/{quote(run_id, safe='')}")
                if run_id else {}
            )
        except CursorAPIError as exc:
            return _deny(
                "vendor_session_unreadable",
                "unreachable_agent",
                provider_status=exc.status,
                provider_error=exc.safe_detail(),
            )
        agent_id = str(agent.get("id") or "")
        agent_url = str(agent.get("url") or "")
        expected_url = f"https://cursor.com/agents/{session_id}"
        if agent_id != session_id or not agent_url or not agent_url.startswith(expected_url):
            return _deny(
                "adoption_receipt_incomplete",
                "missing_data",
                missing=[name for name, value in (
                    ("agent_id", agent_id == session_id),
                    ("agent_url", agent_url.startswith(expected_url)),
                ) if not value],
            )
        status = _provider_status(run.get("status") or agent.get("status"))
        return {
            "ok": True,
            "agent_id": agent_id,
            "agent_url": agent_url,
            "run_id": run_id or None,
            "status": status,
            "pr_url": _pr_url(agent, run),
        }

    def resume_session(self, _provider_session_id: str) -> dict[str, Any]:
        return _deny(
            "same_run_resume_unsupported",
            "invalid_input",
            capabilities=self.capabilities(),
        )

    def follow_up(self, provider_session_id: str, prompt: str) -> dict[str, Any]:
        if not str(provider_session_id or "").strip() or not str(prompt or "").strip():
            return _deny("follow_up_input_missing", "invalid_input")
        safe_id = quote(str(provider_session_id), safe="")
        try:
            response = self.client.request(
                "POST",
                f"/v1/agents/{safe_id}/runs",
                {"prompt": {"text": str(prompt).strip()}, "conversationMode": "agent"},
            )
        except CursorAPIError as exc:
            return _deny(
                "vendor_follow_up_failed",
                "broken_connection",
                provider_status=exc.status,
                provider_error=exc.safe_detail(),
            )
        run = response.get("run") if isinstance(response.get("run"), dict) else response
        run_id = str((run or {}).get("id") or "")
        if not run_id:
            return _deny("follow_up_receipt_incomplete", "missing_data")
        return {
            "allowed": True,
            "vendor_id": self.vendor_id,
            "provider_session_id": str(provider_session_id),
            "provider_run_id": run_id,
            "provider_status": _provider_status((run or {}).get("status")),
            "continuity": "same_agent_follow_up",
            "exact_resume": False,
        }

    def get_usage(
        self, provider_session_id: str, *, task_id: str, run_id: str = ""
    ) -> dict[str, Any]:
        safe_id = quote(str(provider_session_id or ""), safe="")
        if not safe_id or not task_id:
            return _deny("usage_input_missing", "invalid_input")
        query = "?" + urlencode({"runId": run_id}) if run_id else ""
        try:
            payload = self.client.request("GET", f"/v1/agents/{safe_id}/usage{query}")
        except CursorAPIError as exc:
            return _deny(
                "provider_usage_unreadable",
                "broken_connection",
                provider_status=exc.status,
                provider_error=exc.safe_detail(),
            )
        usage = payload.get("totalUsage") or {}
        receipt = {
            "schema": "switchboard.cloud_usage_receipt.v1",
            "source": "agent_report",
            "confidence": "reported",
            "billing_mode": "api_usage",
            "cost_usd": 0,
            "cost_status": "unknown_until_provider_reconcile",
            "task_id": task_id,
            "vendor_id": self.vendor_id,
            "provider_session_id_hash": hashlib.sha256(
                str(provider_session_id).encode()
            ).hexdigest(),
            "runner_session_id": f"cloud/{self.vendor_id}/{provider_session_id}",
            "provider_run_id": run_id or None,
            "input_tokens": int(usage.get("inputTokens") or 0),
            "output_tokens": int(usage.get("outputTokens") or 0),
            "cache_write_tokens": int(usage.get("cacheWriteTokens") or 0),
            "cache_read_tokens": int(usage.get("cacheReadTokens") or 0),
            "total_tokens": int(usage.get("totalTokens") or 0),
        }
        errors = validate_usage_receipt(receipt)
        return receipt if not errors else _deny("usage_receipt_invalid", "malformed_payload",
                                                errors=errors)
