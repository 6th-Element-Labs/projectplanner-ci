"""Connect-path Codex PTY prompt → PROTO-7/8 Needs-you attention (ADAPTER-31).

Detects known interactive TUI prompts in PTY output, upserts a durable attention
request through the host API, polls for the operator decision, and writes the
answer back to the PTY. Never auto-selects a model downgrade.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Optional


PROVIDER = "openai-codex-pty"
SCHEMA_VERSION = "codex.pty.v1"
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class PtyAttentionFault(RuntimeError):
    """Visible Connect fault: operator did not answer before expiry."""


@dataclass(frozen=True)
class PromptMatch:
    kind: str
    prompt: str
    choices: list[dict[str, Any]]
    recommended_default: str
    signature: str
    provider_request_id: str


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def detect_prompt(text: str) -> Optional[PromptMatch]:
    """Return a PromptMatch for a known interactive Codex TUI dialog, else None."""
    plain = strip_ansi(text)
    if not plain:
        return None
    # ADAPTER-28 / 2026-07-25 21:34 rate-limit model-switch dialog.
    if "Approaching rate limits" in plain and "Switch to" in plain:
        choices = [
            {"id": "1", "label": "Switch to cheaper model",
             "description": "Accept the provider's suggested model downgrade"},
            {"id": "2", "label": "Keep current model",
             "description": "Stay on the current model (operator decision)"},
            {"id": "3", "label": "Keep current model (never show again)",
             "description": "Stay on the current model and suppress this dialog"},
        ]
        # Capture the question line for the Needs-you queue.
        prompt_line = "Approaching rate limits — operator model decision required"
        for line in plain.splitlines():
            stripped = line.strip()
            if "Approaching rate limits" in stripped:
                prompt_line = stripped.lstrip("›•■ ").strip()
                break
        signature = "rate_limit_model_switch"
        request_id = "pty:" + hashlib.sha256(
            (signature + ":" + prompt_line).encode()
        ).hexdigest()[:24]
        return PromptMatch(
            kind=signature,
            prompt=prompt_line,
            choices=choices,
            recommended_default="2",
            signature=signature,
            provider_request_id=request_id,
        )
    return None


def pty_reply_bytes(choice: Any) -> bytes:
    selected = choice if isinstance(choice, dict) else {"id": choice}
    choice_id = str(selected.get("id") or "").strip()
    if choice_id not in {"1", "2", "3"}:
        raise PtyAttentionFault(f"unsupported Codex PTY decision: {choice_id!r}")
    return f"{choice_id}\r".encode("ascii")


def _stable_key(binding: dict[str, str], provider_request_id: str, kind: str) -> str:
    raw = ":".join((
        binding["runner_session_id"], binding["task_id"],
        provider_request_id, kind,
    ))
    return "codex-pty:" + hashlib.sha256(raw.encode()).hexdigest()


class CodexPtyAttentionBridge:
    """One Connect PTY session bound to Switchboard attention delivery."""

    def __init__(
        self,
        *,
        http: Callable[..., dict[str, Any]],
        binding: dict[str, str],
        write_pty: Callable[[bytes], Any],
        poll_interval: float = 1.0,
        journal_path: str = "",
        claim_deadline_s: float = 3600.0,
        expires_in_s: float = 3600.0,
    ):
        self.http = http
        self.binding = binding
        self.write_pty = write_pty
        self.poll_interval = poll_interval
        self.claim_deadline_s = claim_deadline_s
        self.expires_in_s = expires_in_s
        self.receipts: list[dict[str, Any]] = []
        self._pending_deliveries: list[tuple[str, int, dict[str, Any]]] = []
        self._handled: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        configured = journal_path or str(
            os.environ.get("PM_CODEX_PTY_ATTENTION_JOURNAL") or "").strip()
        self.journal_path = Path(configured).resolve() if configured else None
        if self.journal_path and self.journal_path.is_file():
            saved = json.loads(self.journal_path.read_text(encoding="utf-8"))
            if saved.get("binding") != self.binding:
                raise PtyAttentionFault("Codex PTY attention journal binding mismatch")
            self._handled = dict(saved.get("handled") or {})
            self.receipts = list(saved.get("receipts") or [])
            self._pending_deliveries = [
                (entry["request_id"], int(entry["expected_version"]), entry["receipt"])
                for entry in saved.get("pending_deliveries") or []
            ]

    def _save(self) -> None:
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.journal_path.with_suffix(self.journal_path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "schema": "switchboard.codex_pty_attention_journal.v1",
            "binding": self.binding,
            "handled": self._handled,
            "receipts": self.receipts,
            "pending_deliveries": [
                {"request_id": request_id, "expected_version": version,
                 "receipt": receipt}
                for request_id, version, receipt in self._pending_deliveries
            ],
        }, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.journal_path)

    def handle_match(self, match: PromptMatch) -> None:
        key = _stable_key(self.binding, match.provider_request_id, match.kind)
        with self._lock:
            if key in self._handled:
                return
        expires_at = time.time() + float(self.expires_in_s)
        payload = {
            "project": self.binding["project"],
            "provider": PROVIDER,
            "provider_request_id": match.provider_request_id,
            "schema_version": SCHEMA_VERSION,
            "prompt": match.prompt,
            "choices": match.choices,
            "idempotency_key": key,
            "host_id": self.binding["host_id"],
            "runner_session_id": self.binding["runner_session_id"],
            "task_id": self.binding["task_id"],
            "work_session_id": self.binding.get("work_session_id") or "",
            "recommended_default": match.recommended_default,
            "expires_at": expires_at,
            "context": {
                "kind": match.kind,
                "signature": match.signature,
                "task_id": self.binding["task_id"],
                "work_session_id": self.binding.get("work_session_id") or "",
                "runner_session_id": self.binding["runner_session_id"],
                "deep_link": {
                    "task_id": self.binding["task_id"],
                    "runner_session_id": self.binding["runner_session_id"],
                },
                # Explicit operator policy: never silently downgrade.
                "never_auto_switch_model": True,
            },
        }
        created = self.http("POST", "/ixp/v1/attention/requests", payload)
        request = created.get("request") or {}
        request_id = str(request.get("request_id") or "")
        if not request_id:
            raise PtyAttentionFault(
                "Switchboard did not persist the Codex PTY attention request")

        deadline = time.monotonic() + float(self.claim_deadline_s)
        delivery = None
        while delivery is None:
            if time.monotonic() > deadline:
                raise PtyAttentionFault(
                    "Codex PTY attention unanswered past claim deadline; "
                    "runner faulted (no model auto-switch)")
            claimed = self.http("POST", "/ixp/v1/attention/decisions/claim", {
                "project": self.binding["project"],
                "host_id": self.binding["host_id"],
                "provider": PROVIDER,
                "request_id": request_id,
                "runner_session_id": self.binding["runner_session_id"],
                "work_session_id": self.binding.get("work_session_id") or "",
            })
            if claimed.get("error") in {
                "attention_request_expired", "attention_request_not_found",
            } or str((claimed.get("request") or {}).get("status") or "") == "expired":
                raise PtyAttentionFault(
                    "Codex PTY attention request expired unanswered; "
                    "runner faulted (no model auto-switch)")
            delivery = claimed.get("delivery") if claimed.get("claimed") else None
            if delivery is None:
                time.sleep(self.poll_interval)

        decision = delivery.get("decision") or {}
        reply = pty_reply_bytes(decision.get("choice") or {})
        self.write_pty(reply)
        with self._lock:
            self._handled[key] = {
                "request_id": request_id,
                "choice": decision.get("choice") or {},
                "reply": reply.decode("ascii"),
            }
        receipt = {
            "schema": "switchboard.codex_pty_attention_receipt.v1",
            "request_id": request_id,
            "kind": match.kind,
            "decision_id": decision.get("decision_id"),
            "reply": reply.decode("ascii"),
            "events": ["ptyPrompt/resolved"],
        }
        self.receipts.append(receipt)
        self._pending_deliveries.append((
            request_id,
            int((delivery.get("request") or {}).get("version") or 3),
            receipt,
        ))
        self._save()

    def complete_delivery(self) -> None:
        for request_id, expected_version, receipt in list(self._pending_deliveries):
            receipt = dict(receipt)
            receipt["events"] = list(receipt.get("events") or []) + ["ptyPrompt/delivered"]
            delivered = self.http(
                "POST", f"/ixp/v1/attention/requests/{request_id}/delivery", {
                    "project": self.binding["project"],
                    "host_id": self.binding["host_id"],
                    "provider": PROVIDER,
                    "runner_session_id": self.binding["runner_session_id"],
                    "work_session_id": self.binding.get("work_session_id") or "",
                    "expected_version": expected_version,
                    "receipt": receipt,
                })
            if delivered.get("status") != "resolved":
                raise PtyAttentionFault(
                    "Switchboard did not resolve the Codex PTY attention request")
        self._pending_deliveries.clear()
        self._save()


class PtyPromptWatcher:
    """Tail PTY log bytes and drive CodexPtyAttentionBridge on known prompts."""

    def __init__(
        self,
        *,
        bridge: CodexPtyAttentionBridge,
        window_chars: int = 12000,
        on_fault: Optional[Callable[[BaseException], None]] = None,
    ):
        self.bridge = bridge
        self.window_chars = window_chars
        self.on_fault = on_fault
        self._buf = ""
        self._lock = threading.Lock()
        self._active_key: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def feed(self, data: bytes) -> None:
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        with self._lock:
            self._buf = (self._buf + text)[-self.window_chars:]
            snapshot = self._buf
        match = detect_prompt(snapshot)
        if match is None:
            return
        key = _stable_key(
            self.bridge.binding, match.provider_request_id, match.kind)
        with self._lock:
            if self._active_key == key:
                return
            self._active_key = key
        try:
            self.bridge.handle_match(match)
            self.bridge.complete_delivery()
        except BaseException as exc:  # noqa: BLE001 — surface Connect fault
            if self.on_fault is not None:
                self.on_fault(exc)
            else:
                raise

    def start_log_tail(self, log_path: str, *, poll_s: float = 0.5) -> None:
        path = Path(log_path)

        def _loop() -> None:
            offset = 0
            if path.is_file():
                offset = path.stat().st_size
            while not self._stop.is_set():
                try:
                    if path.is_file():
                        size = path.stat().st_size
                        if size < offset:
                            offset = 0
                        if size > offset:
                            with path.open("rb") as fh:
                                fh.seek(offset)
                                chunk = fh.read()
                            offset = size
                            if chunk:
                                self.feed(chunk)
                except Exception:  # noqa: BLE001
                    pass
                self._stop.wait(poll_s)

        self._thread = threading.Thread(
            target=_loop, name="pty-prompt-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
