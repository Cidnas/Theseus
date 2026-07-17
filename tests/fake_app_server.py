"""Minimal protocol peer used by the app-server client tests."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


thread_params: dict[str, dict[str, Any]] = {}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def receive() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


while request := receive():
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        if not params.get("capabilities", {}).get("experimentalApi"):
            send({"id": request_id, "error": {"code": 1, "message": "experimental API required"}})
        else:
            send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        thread_id = f"thread-{len(thread_params) + 1}"
        thread_params[thread_id] = params
        send({"id": request_id, "result": {"thread": {"id": thread_id}}})
        send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        thread_id = params["threadId"]
        thread_params[thread_id] = params
        send({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        thread_id = params["threadId"]
        turn_id = "turn-1"
        send({"id": request_id, "result": {"turn": {"id": turn_id}}})
        configured = thread_params[thread_id]
        dynamic_tools = configured.get("dynamicTools", [])
        prompt = params.get("input", [{}])[0].get("text", "")
        if prompt == "Request an unavailable tool.":
            requested_tool = "unavailable"
        elif dynamic_tools:
            requested_tool = dynamic_tools[0]["name"]
        else:
            requested_tool = None
        tool_response = None
        if requested_tool:
            send(
                {
                    "id": "server-tool-1",
                    "method": "item/tool/call",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "callId": "call-1",
                        "tool": requested_tool,
                        "arguments": {"value": 7},
                    },
                }
            )
            tool_response = receive()

        payload = {
            "codexHome": os.environ.get("CODEX_HOME"),
            "threadParams": configured,
            "toolResponse": tool_response,
        }
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": json.dumps(payload, sort_keys=True),
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "completed"},
                },
            }
        )
