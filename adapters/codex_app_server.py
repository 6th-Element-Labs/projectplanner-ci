"""Pinned Codex App Server runner with durable Switchboard attention delivery."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from switchboard.application.codex_telemetry import (
    CodexTelemetryWriter,
    binding_from_environment,
    sanitize_app_server_message,
)
from switchboard.connect.execution_assignment import (
    CODEX_CONTEXT_PROFILE_SCHEMA,
    ExecutionAssignmentError,
    resolve_codex_context_profile,
)

PINNED_CODEX_VERSION = "0.144.5"
ATTENTION_METHODS = {
    "item/commandExecution/requestApproval": "command_approval",
    "item/fileChange/requestApproval": "file_change_approval",
    "item/tool/requestUserInput": "user_input",
    "item/permissions/requestApproval": "permission_approval",
}


class AppServerError(RuntimeError):
    pass


_PROFILE_CONFIG_KEYS = {
    "model": "model",
    "model_reasoning_effort": "reasoning_effort",
    "model_context_window": "context_window",
    "model_auto_compact_token_limit": "auto_compact_token_limit",
}


def _config_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value.strip('"\'')
    return str(decoded)


def _profile_config(profile: str) -> tuple[str, dict[str, Any]]:
    try:
        selected, values = resolve_codex_context_profile(profile)
    except ExecutionAssignmentError as exc:
        raise AppServerError(str(exc)) from exc
    if selected:
        validate_profile_templates(selected)
    return selected, values


def validate_profile_templates(profile: str = "") -> None:
    """Ensure checked-in TOML templates match the server-owned profile catalog."""
    selected = str(profile or "").strip().lower()
    names = [selected] if selected else [
        "luna-max-large-codebase", "luna-max-long-running"]
    profile_dir = Path(__file__).resolve().parent / "codex" / "profiles"
    for name in names:
        try:
            _, values = resolve_codex_context_profile(name)
        except ExecutionAssignmentError as exc:
            raise AppServerError(str(exc)) from exc
        path = profile_dir / f"{name}.config.toml"
        if not path.is_file() or path.is_symlink():
            raise AppServerError(
                f"codex context profile template missing: {name}")
        try:
            with path.open("rb") as stream:
                configured = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise AppServerError(
                f"codex context profile template invalid: {name}") from exc
        expected = {
            "model": values["model"],
            "model_reasoning_effort": values["reasoning_effort"],
            "model_context_window": int(values["context_window"]),
            "model_auto_compact_token_limit": int(
                values["auto_compact_token_limit"]),
        }
        if configured != expected:
            raise AppServerError(
                f"codex context profile template drifted: {name}")


def profile_overrides(
        profile: str, existing: Iterable[str] = ()) -> list[str]:
    """Return explicit Codex ``-c`` expressions for one named profile.

    Existing host overrides are checked for controlled keys before the profile
    is appended.  This prevents an operator config or a duplicate override from
    silently changing the requested model or context limits.
    """
    selected, values = _profile_config(profile)
    if not selected:
        return []
    for override in existing:
        key, separator, raw = str(override or "").partition("=")
        field = _PROFILE_CONFIG_KEYS.get(key.strip())
        if not separator or not field:
            continue
        expected = str(values[field])
        if _config_value(raw) != expected:
            raise AppServerError(
                f"codex context profile {selected!r} conflicts with {key.strip()}")
    return [
        f'model={json.dumps(values["model"])}',
        f'model_reasoning_effort={json.dumps(values["reasoning_effort"])}',
        f'model_context_window={int(values["context_window"])}',
        f'model_auto_compact_token_limit={int(values["auto_compact_token_limit"])}',
    ]


def launch_receipt(profile: str, binding: Mapping[str, Any], *,
                   codex_version: str = PINNED_CODEX_VERSION) -> dict[str, Any]:
    """Build a redacted, provider-effective launch receipt."""
    selected, values = _profile_config(profile)
    if not selected:
        return {}
    effort = values["reasoning_effort"]
    context_window = int(values["context_window"])
    auto_compact = int(values["auto_compact_token_limit"])
    return {
        "schema": "switchboard.codex_launch_receipt.v1",
        "profile": selected,
        "profile_schema": CODEX_CONTEXT_PROFILE_SCHEMA,
        "model": values["model"],
        "model_reasoning_effort": effort,
        "model_context_window": context_window,
        "model_auto_compact_token_limit": auto_compact,
        # Short aliases make the receipt easy to consume without weakening the
        # canonical Codex config field names above.
        "reasoning_effort": effort,
        "context_window": context_window,
        "auto_compact_token_limit": auto_compact,
        "codex_version": str(codex_version or "").strip(),
        "host_id": str(binding.get("host_id") or ""),
        "task_id": str(binding.get("task_id") or ""),
        "claim_id": str(binding.get("claim_id") or ""),
        "work_session_id": str(binding.get("work_session_id") or ""),
        "host": str(binding.get("host_id") or ""),
        "task": str(binding.get("task_id") or ""),
        "claim": str(binding.get("claim_id") or ""),
        "work_session": str(binding.get("work_session_id") or ""),
    }


def persist_launch_receipt(path: str | Path, profile: str,
                           binding: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one redacted profile receipt atomically under the runner root."""
    receipt_path = Path(path).expanduser()
    if receipt_path.exists() and receipt_path.is_symlink():
        raise AppServerError("Codex launch receipt path must not be a symlink")
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = launch_receipt(profile, binding)
    if not receipt:
        return receipt
    temporary = receipt_path.with_suffix(receipt_path.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(receipt_path)
    return receipt


def _stable_key(binding: dict[str, str], request_id: Any, method: str) -> str:
    raw = ":".join((
        binding["runner_session_id"], binding["task_id"], str(request_id), method,
    ))
    return "codex-app-server:" + hashlib.sha256(raw.encode()).hexdigest()


def _choices(method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if method == "item/tool/requestUserInput":
        result: list[dict[str, Any]] = []
        for question in params.get("questions") or []:
            for option in question.get("options") or []:
                result.append({
                    "id": f"{question.get('id')}:{option.get('label')}",
                    "label": option.get("label"),
                    "description": option.get("description"),
                    "question_id": question.get("id"),
                })
        return result
    if method == "item/permissions/requestApproval":
        return [{"id": "accept"}, {"id": "decline"}]
    return [
        {"id": "accept", "label": "Allow once"},
        {"id": "acceptForSession", "label": "Allow for session"},
        {"id": "decline", "label": "Deny"},
        {"id": "cancel", "label": "Deny and stop"},
    ]


def _prompt(method: str, params: dict[str, Any]) -> str:
    if method == "item/tool/requestUserInput":
        return "\n".join(
            str(question.get("question") or question.get("header") or "Codex question")
            for question in params.get("questions") or []
        )
    return str(
        params.get("reason") or params.get("command") or params.get("grantRoot")
        or f"Codex requests {ATTENTION_METHODS[method].replace('_', ' ')}"
    )


def _reply(method: str, choice: Any, params: dict[str, Any]) -> dict[str, Any]:
    selected = choice if isinstance(choice, dict) else {"id": choice}
    choice_id = selected.get("id")
    if method == "item/tool/requestUserInput":
        supplied = selected.get("answers")
        if isinstance(supplied, dict):
            answers = {}
            for key, value in supplied.items():
                if isinstance(value, dict):
                    answers[key] = value
                elif isinstance(value, list):
                    answers[key] = {"answers": [str(item) for item in value]}
                else:
                    answers[key] = {"answers": [str(value)]}
            return {"answers": answers}
        answers: dict[str, dict[str, list[str]]] = {}
        for question in params.get("questions") or []:
            qid = str(question.get("id") or "")
            answer = selected.get(qid)
            if answer is None and str(choice_id or "").startswith(qid + ":"):
                answer = str(choice_id).split(":", 1)[1]
            answers[qid] = {"answers": [str(answer or "")]}
        return {"answers": answers}
    if method == "item/permissions/requestApproval":
        if choice_id not in {"accept", "acceptForSession"}:
            # An empty grant is the protocol-native denial for permission profiles.
            return {"permissions": {}, "scope": "turn"}
        return {
            "permissions": selected.get("permissions") or params.get("permissions") or {},
            "scope": "session" if choice_id == "acceptForSession" else "turn",
        }
    if choice_id not in {"accept", "acceptForSession", "decline", "cancel"}:
        raise AppServerError(f"unsupported Codex approval decision: {choice_id!r}")
    return {"decision": choice_id}


class CodexAppServer:
    """One stdio App Server connection bound to one Switchboard execution."""

    def __init__(
        self, command: list[str], *, cwd: str, env: dict[str, str],
        http: Callable[..., dict[str, Any]], binding: dict[str, str],
        popen: Callable[..., Any] = subprocess.Popen, poll_interval: float = 1.0,
        journal_path: str = "", context_profile: str = "", telemetry_path: str = "",
    ):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.http = http
        self.binding = binding
        self.popen = popen
        self.poll_interval = poll_interval
        self._next_id = 1
        self.receipts: list[dict[str, Any]] = []
        self.context_profile = ""
        self.launch_receipt: dict[str, Any] = {}
        self._pending_deliveries: list[tuple[str, int, dict[str, Any]]] = []
        self._replies: dict[str, dict[str, Any]] = {}
        configured_journal = journal_path or str(
            os.environ.get("PM_CODEX_APP_SERVER_JOURNAL") or "").strip()
        self.journal_path = Path(configured_journal).resolve() if configured_journal else None
        configured_telemetry = str(telemetry_path or "").strip()
        if not configured_telemetry and self.journal_path:
            configured_telemetry = str(
                self.journal_path.with_name("codex-telemetry.jsonl"))
        self.telemetry = (
            CodexTelemetryWriter(configured_telemetry)
            if configured_telemetry else None
        )
        self.telemetry_binding = binding_from_environment(
            runner_session_id=str(binding.get("runner_session_id") or ""),
            task_id=str(binding.get("task_id") or ""),
            host_id=str(binding.get("host_id") or ""),
            command=command,
            environment=env,
        )
        if self.journal_path and self.journal_path.is_file():
            saved = json.loads(self.journal_path.read_text(encoding="utf-8"))
            if saved.get("binding") != self.binding:
                raise AppServerError("Codex App Server journal binding mismatch")
            self._replies = dict(saved.get("replies") or {})
            self.receipts = list(saved.get("receipts") or [])
            self._pending_deliveries = [
                (entry["request_id"], int(entry["expected_version"]), entry["receipt"])
                for entry in saved.get("pending_deliveries") or []
            ]
            self.launch_receipt = dict(saved.get("launch_receipt") or {})
            self.context_profile = str(
                self.launch_receipt.get("profile") or "").strip().lower()
        selected_profile = str(context_profile or "").strip().lower()
        if selected_profile:
            selected_profile, _ = _profile_config(selected_profile)
            if (self.context_profile
                    and self.context_profile != selected_profile):
                raise AppServerError("Codex App Server journal profile mismatch")
            self.context_profile = selected_profile
            if not self.launch_receipt:
                self.launch_receipt = launch_receipt(
                    selected_profile, self.binding)
        if self.launch_receipt:
            self._save()

    def _save(self) -> None:
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.journal_path.with_suffix(self.journal_path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "schema": "switchboard.codex_app_server_journal.v1",
            "binding": self.binding,
            "launch_receipt": self.launch_receipt,
            "replies": self._replies,
            "receipts": self.receipts,
            "pending_deliveries": [
                {"request_id": request_id, "expected_version": version,
                 "receipt": receipt}
                for request_id, version, receipt in self._pending_deliveries
            ],
        }, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.journal_path)

    def _send(self, process: Any, message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _request(self, process: Any, method: str, params: dict[str, Any]) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._send(process, {"jsonrpc": "2.0", "id": request_id,
                             "method": method, "params": params})
        return request_id

    def _attention(self, process: Any, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params") or {}
        server_request_id = message["id"]
        key = _stable_key(self.binding, server_request_id, method)
        if key in self._replies:
            self._send(process, {"jsonrpc": "2.0", "id": server_request_id,
                                 "result": self._replies[key]})
            return
        payload = {
            "project": self.binding["project"],
            "provider": "openai-codex-app-server",
            "provider_request_id": str(server_request_id),
            "schema_version": "codex.app-server.v2",
            "prompt": _prompt(method, params),
            "choices": _choices(method, params),
            "idempotency_key": key,
            "host_id": self.binding["host_id"],
            "runner_session_id": self.binding["runner_session_id"],
            "task_id": self.binding["task_id"],
            "context": {
                "method": method,
                "params": params,
                "task_id": self.binding["task_id"],
                "work_session_id": self.binding["work_session_id"],
                "runner_session_id": self.binding["runner_session_id"],
                "server_request_id": server_request_id,
            },
        }
        created = self.http("POST", "/ixp/v1/attention/requests", payload)
        request = created.get("request") or {}
        request_id = str(request.get("request_id") or "")
        if not request_id:
            raise AppServerError("Switchboard did not persist the Codex server request")
        delivery = None
        while delivery is None:
            claimed = self.http("POST", "/ixp/v1/attention/decisions/claim", {
                "project": self.binding["project"],
                "host_id": self.binding["host_id"],
                "provider": "openai-codex-app-server",
                "request_id": request_id,
                "runner_session_id": self.binding["runner_session_id"],
                "work_session_id": self.binding["work_session_id"],
            })
            delivery = claimed.get("delivery") if claimed.get("claimed") else None
            if delivery is None:
                time.sleep(self.poll_interval)
        decision = delivery.get("decision") or {}
        response = _reply(method, decision.get("choice") or {}, params)
        self._send(process, {"jsonrpc": "2.0", "id": server_request_id,
                             "result": response})
        self._replies[key] = response
        receipt = {
            "schema": "switchboard.codex_app_server_receipt.v1",
            "request_id": request_id,
            "server_request_id": server_request_id,
            "method": method,
            "decision_id": decision.get("decision_id"),
            "reply": response,
            "events": ["serverRequest/resolved"],
        }
        self.receipts.append(receipt)
        self._pending_deliveries.append((
            request_id,
            int((delivery.get("request") or {}).get("version") or 3),
            receipt,
        ))
        self._save()

    def _complete_deliveries(self, turn_id: str) -> None:
        for request_id, expected_version, receipt in self._pending_deliveries:
            receipt["events"].append("item/completed")
            receipt["turn_id"] = turn_id
            delivered = self.http(
                "POST", f"/ixp/v1/attention/requests/{request_id}/delivery", {
                    "project": self.binding["project"],
                    "host_id": self.binding["host_id"],
                    "provider": "openai-codex-app-server",
                    "runner_session_id": self.binding["runner_session_id"],
                    "work_session_id": self.binding["work_session_id"],
                    "expected_version": expected_version,
                    "receipt": receipt,
                })
            if delivered.get("status") != "resolved":
                raise AppServerError(
                    "Switchboard did not resolve the Codex attention request")
        self._pending_deliveries.clear()
        self._save()

    def run(self, prompt: str) -> subprocess.CompletedProcess[str]:
        process = self.popen(
            self.command, cwd=self.cwd, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        initialize_id = self._request(process, "initialize", {
            "clientInfo": {"name": "switchboard-agent-host", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        initialized = False
        turn_started = False
        turn_id = None
        output: list[str] = []
        try:
            while True:
                if process.poll() is not None:
                    break
                events = selector.select(timeout=1)
                for key, _mask in events:
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    output.append(line)
                    message = json.loads(line)
                    if self.telemetry is not None:
                        self.telemetry.append(sanitize_app_server_message(
                            message, self.telemetry_binding))
                    if message.get("id") == initialize_id and "result" in message:
                        self._send(process, {"jsonrpc": "2.0", "method": "initialized",
                                             "params": {}})
                        initialized = True
                        self._request(process, "thread/start", {
                            "cwd": self.cwd, "ephemeral": False,
                            "approvalPolicy": "on-request", "sandbox": "workspace-write",
                        })
                    elif (initialized and not turn_started and message.get("id")
                          and isinstance(message.get("result"), dict)
                          and (message["result"].get("thread") or {}).get("id")):
                        thread_id = message["result"]["thread"]["id"]
                        self._request(process, "turn/start", {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": prompt}],
                        })
                        turn_started = True
                    elif message.get("method") in ATTENTION_METHODS and "id" in message:
                        self._attention(process, message)
                    elif message.get("method") == "turn/completed":
                        turn_id = (message.get("params") or {}).get("turn", {}).get("id")
                        self._complete_deliveries(str(turn_id or ""))
                        process.terminate()
                if turn_started and process.poll() is not None:
                    break
        finally:
            selector.close()
        try:
            stdout_tail, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_tail, stderr = process.communicate()
        stdout = "".join(output) + (stdout_tail or "")
        if not turn_id and process.returncode not in {0, -15}:
            return subprocess.CompletedProcess(
                self.command, process.returncode or 1, stdout, stderr)
        return subprocess.CompletedProcess(self.command, 0, stdout, stderr)


def pinned_command(
        executable: str, overrides: list[str], *, profile: str = "",
        context_profile: str = "") -> list[str]:
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=False,
    )
    actual = (version.stdout or version.stderr or "").strip().split()[-1]
    expected = str(os.environ.get("PM_CODEX_APP_SERVER_VERSION")
                   or PINNED_CODEX_VERSION).strip()
    if version.returncode or actual != expected:
        raise AppServerError(
            f"Codex App Server version mismatch: expected {expected}, found {actual or 'unknown'}")
    selected_profile = str(profile or context_profile or "").strip().lower()
    explicit_profile_overrides = profile_overrides(
        selected_profile, overrides) if selected_profile else []
    return [
        executable, "app-server", "--stdio",
        *[value for override in (*explicit_profile_overrides, *overrides)
          for value in ("-c", override)],
    ]
