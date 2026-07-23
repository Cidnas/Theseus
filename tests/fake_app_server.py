"""Minimal protocol peer used by the app-server client tests."""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any


thread_params: dict[str, dict[str, Any]] = {}
send_lock = threading.Lock()
parallel_lock = threading.Lock()
parallel_ready = threading.Event()
parallel_first_item = threading.Event()
parallel_second_item = threading.Event()
parallel_turns = 0


def send(message: dict[str, Any]) -> None:
    with send_lock:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def receive() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def complete_parallel_turn(thread_id: str, turn_id: str, text: str) -> None:
    parallel_ready.wait()
    if thread_id == "thread-1":
        send_agent_message(thread_id, turn_id, text)
        parallel_first_item.set()
        parallel_second_item.wait()
    else:
        parallel_first_item.wait()
        send_agent_message(thread_id, turn_id, text)
        parallel_second_item.set()
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed"},
            },
        }
    )


def send_agent_message(thread_id: str, turn_id: str, text: str) -> None:
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": f"item-{thread_id}",
                    "type": "agentMessage",
                    "text": text,
                },
            },
        }
    )


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
        if thread_id.startswith("missing-rollout-"):
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": f"no rollout found for thread id {thread_id}",
                    },
                }
            )
        else:
            thread_params[thread_id] = params
            send({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        thread_id = params["threadId"]
        prompt = params.get("input", [{}])[0].get("text", "")
        if prompt.startswith("Parallel "):
            turn_id = f"turn-{thread_id}"
            send({"id": request_id, "result": {"turn": {"id": turn_id}}})
            with parallel_lock:
                parallel_turns += 1
                if parallel_turns == 2:
                    parallel_ready.set()
            threading.Thread(
                target=complete_parallel_turn,
                args=(thread_id, turn_id, prompt),
                daemon=True,
            ).start()
            continue

        turn_id = "turn-1"
        send({"id": request_id, "result": {"turn": {"id": turn_id}}})
        configured = thread_params[thread_id]
        dynamic_tools = configured.get("dynamicTools", [])
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
            "turnParams": params,
            "toolResponse": tool_response,
        }
        application_context = params.get("additionalContext", {})
        active_step = application_context.get("active_learning_step")
        if isinstance(active_step, dict):
            context = json.loads(active_step["value"])
            response_text = json.dumps(
                {
                    "reply": "Fast structured tutor reply.",
                    "evidence": [
                        {
                            "concept_id": context["step"]["concept_ids"][0],
                            "evidence": "The learner supplied a relevant answer.",
                            "elicitation_context": "The active-step practice prompt.",
                        }
                    ],
                    "checkpoint": None,
                },
                sort_keys=True,
            )
        else:
            response_text = json.dumps(payload, sort_keys=True)
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": response_text,
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
