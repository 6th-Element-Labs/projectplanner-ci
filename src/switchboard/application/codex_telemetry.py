"""Sanitized, append-only Codex execution telemetry.

The Codex TUI persists rich rollout JSONL that can include prompts, reasoning,
tool arguments, account balances, and command output.  Switchboard must not
copy that material into its runner evidence.  This module selects only token,
compaction, turn-lifecycle, and model-routing facts and binds them to the exact
runner execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

TELEMETRY_SCHEMA = "switchboard.codex_telemetry.v1"
SUMMARY_SCHEMA = "switchboard.codex_telemetry_summary.v1"
SAFE_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _camel_or_snake(data: dict[str, Any], name: str) -> Any:
    if name in data:
        return data[name]
    parts = name.split("_")
    camel = parts[0] + "".join(part.title() for part in parts[1:])
    return data.get(camel)


def _usage(data: Any) -> dict[str, int]:
    source = data if isinstance(data, dict) else {}
    return {
        field: _integer(_camel_or_snake(source, field))
        for field in SAFE_USAGE_FIELDS
    }


def parse_launch_config(command: Iterable[str]) -> dict[str, Any]:
    """Read safe model settings from Codex ``-c key=value`` arguments."""
    values = list(command)
    config: dict[str, str] = {}
    for index, value in enumerate(values[:-1]):
        if value != "-c":
            continue
        key, separator, raw = str(values[index + 1]).partition("=")
        if not separator:
            continue
        config[key.strip()] = raw.strip().strip('"').strip("'")
    return {
        "model": config.get("model", ""),
        "reasoning_effort": config.get("model_reasoning_effort", ""),
        "configured_context_window": _integer(
            config.get("model_context_window")),
        "auto_compact_token_limit": _integer(
            config.get("model_auto_compact_token_limit")),
    }


def binding_from_environment(
    *,
    runner_session_id: str,
    task_id: str,
    host_id: str,
    command: Iterable[str] = (),
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = environment if environment is not None else os.environ
    try:
        assignment = json.loads(
            env.get("SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON") or "{}")
    except (TypeError, ValueError):
        assignment = {}
    launch = parse_launch_config(command)
    return {
        "project": str(
            env.get("SWITCHBOARD_TELEMETRY_PROJECT")
            or env.get("PM_PROJECT")
            or "switchboard"),
        "task_id": str(task_id or env.get("PM_TASK_ID") or ""),
        "execution_id": str(assignment.get("execution_id") or ""),
        "generation": _integer(assignment.get("generation")),
        "runner_session_id": str(runner_session_id or ""),
        "host_id": str(host_id or env.get("PM_HOST_ID") or ""),
        "profile": str(assignment.get("context_profile") or ""),
        **launch,
    }


def _source_key(source: str, timestamp: str, kind: str, detail: Any) -> str:
    raw = json.dumps(
        [source, timestamp, kind, detail], sort_keys=True,
        separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _base_event(
    binding: dict[str, Any], *, source: str, timestamp: str,
    event_type: str, detail: Any,
) -> dict[str, Any]:
    return {
        "schema": TELEMETRY_SCHEMA,
        "captured_at": time.time(),
        "source_timestamp": str(timestamp or ""),
        "source": source,
        "source_event_id": _source_key(source, str(timestamp or ""), event_type, detail),
        "project": str(binding.get("project") or ""),
        "task_id": str(binding.get("task_id") or ""),
        "execution_id": str(binding.get("execution_id") or ""),
        "generation": _integer(binding.get("generation")),
        "runner_session_id": str(binding.get("runner_session_id") or ""),
        "host_id": str(binding.get("host_id") or ""),
        "profile": str(binding.get("profile") or ""),
        "model": str(binding.get("model") or ""),
        "reasoning_effort": str(binding.get("reasoning_effort") or ""),
        "configured_context_window": _integer(
            binding.get("configured_context_window")),
        "auto_compact_token_limit": _integer(
            binding.get("auto_compact_token_limit")),
        "event_type": event_type,
    }


def sanitize_rollout_record(
    record: dict[str, Any], binding: dict[str, Any], *, source: str = "codex_rollout",
) -> dict[str, Any] | None:
    """Return a safe measurement or ``None`` for model-visible content."""
    if not isinstance(record, dict):
        return None
    timestamp = str(record.get("timestamp") or "")
    outer_type = str(record.get("type") or "")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    payload_type = str(payload.get("type") or "")

    if outer_type == "event_msg" and payload_type == "token_count":
        info = payload.get("info")
        info = info if isinstance(info, dict) else {}
        total = _usage(_camel_or_snake(info, "total_token_usage"))
        latest = _usage(_camel_or_snake(info, "last_token_usage"))
        context_window = _integer(_camel_or_snake(info, "model_context_window"))
        detail = {"total": total, "latest": latest, "model_context_window": context_window}
        event = _base_event(
            binding, source=source, timestamp=timestamp,
            event_type="token_usage", detail=detail,
        )
        event.update(detail)
        event["context_utilization"] = (
            round(latest["input_tokens"] / context_window, 6)
            if context_window else None
        )
        return event

    if outer_type == "event_msg" and payload_type == "context_compacted":
        return _base_event(
            binding, source=source, timestamp=timestamp,
            event_type="context_compaction", detail={},
        )

    return None


def sanitize_app_server_message(
    message: dict[str, Any], binding: dict[str, Any],
) -> dict[str, Any] | None:
    """Select the safe App Server notifications used by runner measurements."""
    if not isinstance(message, dict):
        return None
    method = str(message.get("method") or "")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    timestamp = str(params.get("timestamp") or "")

    if method == "thread/tokenUsage/updated":
        # The protocol has used both a direct usage object and an ``info``
        # wrapper.  Convert either form into the same rollout-shaped sanitizer.
        info = params.get("info") or params.get("tokenUsage") or params
        synthetic = {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": info},
        }
        event = sanitize_rollout_record(
            synthetic, binding, source="codex_app_server")
        if event:
            event["thread_id"] = str(params.get("threadId") or "")
            event["turn_id"] = str(params.get("turnId") or "")
        return event

    item = params.get("item")
    item = item if isinstance(item, dict) else {}
    if method in {"item/started", "item/completed"} \
            and item.get("type") == "contextCompaction":
        detail = {
            "phase": "started" if method.endswith("started") else "completed",
            "thread_id": str(params.get("threadId") or ""),
            "turn_id": str(params.get("turnId") or ""),
            "item_id": str(item.get("id") or ""),
        }
        event = _base_event(
            binding, source="codex_app_server", timestamp=timestamp,
            event_type="context_compaction", detail=detail,
        )
        event.update({
            **detail,
        })
        return event

    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn")
        turn = turn if isinstance(turn, dict) else {}
        status = str(turn.get("status") or "")
        detail = {
            "status": status,
            "method": method,
            "thread_id": str(params.get("threadId") or ""),
            "turn_id": str(turn.get("id") or params.get("turnId") or ""),
        }
        event = _base_event(
            binding, source="codex_app_server", timestamp=timestamp,
            event_type="turn_lifecycle", detail=detail,
        )
        event.update({
            **detail,
        })
        return event

    if method == "model/rerouted":
        detail = {
            "from_model": str(params.get("fromModel") or ""),
            "to_model": str(params.get("toModel") or ""),
            "reason": str(params.get("reason") or "")[:160],
            "thread_id": str(params.get("threadId") or ""),
            "turn_id": str(params.get("turnId") or ""),
        }
        event = _base_event(
            binding, source="codex_app_server", timestamp=timestamp,
            event_type="model_rerouted", detail=detail,
        )
        event.update({
            **detail,
        })
        return event

    return None


class CodexTelemetryWriter:
    """Idempotent append-only event writer with a replaceable read projection."""

    def __init__(self, event_path: str | Path, summary_path: str | Path | None = None):
        self.event_path = Path(event_path)
        self.summary_path = Path(summary_path) if summary_path else \
            self.event_path.with_name("codex-telemetry-summary.json")
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._sequence = 0
        self._summary: dict[str, Any] = {
            "schema": SUMMARY_SCHEMA,
            "event_count": 0,
            "token_update_count": 0,
            "compaction_count": 0,
            "turn_started_count": 0,
            "turn_completed_count": 0,
            "model_reroute_count": 0,
            "peak_request_input_tokens": 0,
            "peak_context_utilization": 0.0,
            "latest_total": _usage({}),
        }
        self._load_existing()

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self._summary)

    def _load_existing(self) -> None:
        try:
            lines = self.event_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            key = str(event.get("source_event_id") or "")
            if key:
                self._seen.add(key)
            self._sequence = max(self._sequence, _integer(event.get("sequence")))
            self._project(event)

    def _project(self, event: dict[str, Any]) -> None:
        summary = self._summary
        summary["event_count"] += 1
        summary["project"] = event.get("project") or summary.get("project", "")
        summary["task_id"] = event.get("task_id") or summary.get("task_id", "")
        summary["execution_id"] = (
            event.get("execution_id") or summary.get("execution_id", ""))
        summary["runner_session_id"] = (
            event.get("runner_session_id") or summary.get("runner_session_id", ""))
        summary["profile"] = event.get("profile") or summary.get("profile", "")
        summary["model"] = event.get("model") or summary.get("model", "")
        summary["reasoning_effort"] = (
            event.get("reasoning_effort") or summary.get("reasoning_effort", ""))
        summary["configured_context_window"] = max(
            _integer(summary.get("configured_context_window")),
            _integer(event.get("configured_context_window")),
        )
        summary["auto_compact_token_limit"] = max(
            _integer(summary.get("auto_compact_token_limit")),
            _integer(event.get("auto_compact_token_limit")),
        )
        summary.setdefault("first_captured_at", event.get("captured_at"))
        summary["last_captured_at"] = event.get("captured_at")
        event_type = event.get("event_type")
        if event_type == "token_usage":
            summary["token_update_count"] += 1
            latest = _usage(event.get("latest"))
            summary["latest_total"] = _usage(event.get("total"))
            summary["reported_model_context_window"] = max(
                _integer(summary.get("reported_model_context_window")),
                _integer(event.get("model_context_window")),
            )
            summary["peak_request_input_tokens"] = max(
                _integer(summary.get("peak_request_input_tokens")),
                latest["input_tokens"],
            )
            summary["peak_context_utilization"] = max(
                float(summary.get("peak_context_utilization") or 0.0),
                float(event.get("context_utilization") or 0.0),
            )
        elif event_type == "context_compaction" and event.get("phase") != "started":
            summary["compaction_count"] += 1
        elif event_type == "turn_lifecycle":
            key = "turn_started_count" if event.get("method") == "turn/started" \
                else "turn_completed_count"
            summary[key] += 1
        elif event_type == "model_rerouted":
            summary["model_reroute_count"] += 1

    def append(self, event: dict[str, Any] | None) -> bool:
        if not event:
            return False
        key = str(event.get("source_event_id") or "")
        if not key:
            raise ValueError("Codex telemetry event has no source_event_id")
        with self._lock:
            if key in self._seen:
                return False
            self._sequence += 1
            stored = {**event, "sequence": self._sequence}
            self.event_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(stored, sort_keys=True, separators=(",", ":")))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self.event_path, 0o600)
            self._seen.add(key)
            self._project(stored)
            temporary = self.summary_path.with_suffix(self.summary_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._summary, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.summary_path)
            return True


class CodexRolloutTelemetryCollector:
    """Tail the rollout for one exact TUI workspace and retain safe events."""

    def __init__(
        self, *, codex_home: str | Path, cwd: str | Path,
        binding: dict[str, Any], event_path: str | Path,
        poll_interval: float = 0.5, started_at: float | None = None,
    ):
        self.codex_home = Path(codex_home).expanduser().resolve()
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.binding = dict(binding)
        self.writer = CodexTelemetryWriter(event_path)
        self.poll_interval = max(0.02, float(poll_interval))
        self.started_at = float(started_at if started_at is not None else time.time())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rollout_path: Path | None = None
        self._offset = 0

    def _thread_id(self) -> str:
        state_path = self.codex_home / "state_5.sqlite"
        if not state_path.is_file():
            return ""
        try:
            connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT id FROM threads WHERE cwd = ? AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (self.cwd, int(self.started_at) - 60),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return ""
        return str(row[0] if row else "")

    def _find_rollout(self) -> Path | None:
        thread_id = self._thread_id()
        if not thread_id:
            return None
        matches = list((self.codex_home / "sessions").glob(
            f"**/rollout-*{thread_id}.jsonl"))
        return matches[0] if len(matches) == 1 else None

    def collect_once(self) -> int:
        if self._rollout_path is None:
            self._rollout_path = self._find_rollout()
        path = self._rollout_path
        if path is None or not path.is_file():
            return 0
        appended = 0
        try:
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(self._offset)
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        stream.seek(position)
                        break
                    self._offset = stream.tell()
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if self.writer.append(sanitize_rollout_record(
                            record, self.binding)):
                        appended += 1
        except OSError:
            return appended
        return appended

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.collect_once()
        self.collect_once()

    def start(self) -> CodexRolloutTelemetryCollector:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"codex-telemetry-{self.binding.get('runner_session_id', '')}",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.poll_interval * 4))
        self.collect_once()


__all__ = [
    "CodexRolloutTelemetryCollector",
    "CodexTelemetryWriter",
    "binding_from_environment",
    "parse_launch_config",
    "sanitize_app_server_message",
    "sanitize_rollout_record",
]
