#!/usr/bin/env python3
"""Minimal MCP server that probes Cursor's native elicitation support."""
from __future__ import annotations

import json
import sys
from typing import Any


PROTOCOL_VERSION = "2025-06-18"


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: Any, value: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def error(request_id: Any, code: int, message: str) -> None:
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    })


def read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def request_elicitation(parent_id: Any) -> None:
    elicitation_id = f"cursor-elicitation-{parent_id}"
    send({
        "jsonrpc": "2.0",
        "id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "message": "Which rollout should I use?",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "rollout": {
                        "type": "string",
                        "title": "Rollout",
                        "description": "Choose one rollout strategy.",
                        "enum": ["canary", "all-at-once"],
                        "enumNames": ["canary", "all-at-once"],
                    }
                },
                "required": ["rollout"],
            },
        },
    })
    while True:
        reply = read_message()
        if reply is None:
            error(parent_id, -32001, "Cursor disconnected before elicitation reply")
            return
        if reply.get("id") != elicitation_id:
            if "id" in reply:
                error(reply["id"], -32601, "request unsupported during elicitation")
            continue
        if "error" in reply:
            result(parent_id, {
                "content": [{
                    "type": "text",
                    "text": "ELICITATION_UNSUPPORTED:" + json.dumps(
                        reply["error"], sort_keys=True, separators=(",", ":")),
                }],
                "isError": True,
            })
            return
        native = reply.get("result")
        result(parent_id, {
            "content": [{
                "type": "text",
                "text": "ELICITATION_REPLY:" + json.dumps(
                    native, sort_keys=True, separators=(",", ":")),
            }]
        })
        return


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            return
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "switchboard-cursor-attention-probe",
                    "version": "1.0.0",
                },
            })
        elif method == "tools/list":
            result(request_id, {
                "tools": [{
                    "name": "ask_rollout",
                    "description": (
                        "Use this tool to ask the human which rollout to use. "
                        "You must call it before answering."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }]
            })
        elif method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") == "ask_rollout":
                request_elicitation(request_id)
            else:
                error(request_id, -32602, "unknown tool")
        elif request_id is not None:
            error(request_id, -32601, "method not found")


if __name__ == "__main__":
    main()
