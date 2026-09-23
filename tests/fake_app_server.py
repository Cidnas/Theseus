"""Deterministic protocol peer: no credentials, model calls, or business tools."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

state_path = Path(os.environ["CODEX_HOME"]) / "fake-threads.json"
threads: dict[str, dict] = (
    json.loads(state_path.read_text()) if state_path.exists() else {}
)
active: dict[str, dict] = {}
pending_tools: dict[str, dict] = {}
interrupts: list[dict] = []
sequence = 0
send_lock = threading.Lock()
parallel: list[dict] = []


def send(message):
    with send_lock:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def item(run, text, *, turn=None):
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": run["thread"],
                "turnId": turn or run["turn"],
                "item": {
                    "id": "item-" + run["turn"],
                    "type": "agentMessage",
                    "text": text,
                },
            },
        }
    )


def complete(run, text=None, status="completed"):
    if text is not None:
        item(run, text)
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": run["thread"],
                "turn": {"id": run["turn"], "status": status},
            },
        }
    )
    if active.get(run["thread"]) is run:
        active.pop(run["thread"], None)


def payload(run, tool_response=None):
    return json.dumps(
        {
            "codexHome": os.environ.get("CODEX_HOME"),
            "threadParams": threads[run["thread"]],
            "turnParams": run["params"],
            "toolResponse": tool_response,
            "interrupts": interrupts,
        },
        sort_keys=True,
    )


def reply_start(request_id, run, delay=0):
    if delay:
        time.sleep(delay)
    send({"id": request_id, "result": {"turn": {"id": run["turn"]}}})


for line in sys.stdin:
    request = json.loads(line)
    request_id, method, params = (
        request.get("id"),
        request.get("method"),
        request.get("params", {}),
    )
    if method is None:
        run = pending_tools.pop(request_id, None)
        if run and active.get(run["thread"]) is run:
            if "toolCalls" in run:
                call_id, name = run["toolCalls"][request_id]
                run["toolResponses"][call_id] = request["result"]
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": run["thread"],
                            "turnId": run["turn"],
                            "item": {
                                "id": call_id,
                                "type": "dynamicToolCall",
                                "tool": name,
                            },
                        },
                    }
                )
                if len(run["toolResponses"]) == len(run["toolCalls"]):
                    complete(run, json.dumps(run["toolResponses"]))
            else:
                complete(run, payload(run, request))
    elif method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        thread = f"thread-{len(threads) + 1}"
        threads[thread] = params
        state_path.write_text(json.dumps(threads))
        send({"id": request_id, "result": {"thread": {"id": thread}}})
    elif method == "thread/resume":
        thread = params["threadId"]
        if "dynamicTools" in params:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32602, "message": "unsupported resume field"},
                }
            )
            continue
        if thread not in threads:
            send({"id": request_id, "error": {"code": -32600, "message": "no rollout"}})
        else:
            threads[thread] = {**threads[thread], **params}
            send({"id": request_id, "result": {"thread": {"id": thread}}})
    elif method == "model/list":
        number = 2 if params.get("cursor") else 1
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": f"model-{number}",
                            "model": f"model-{number}",
                            "displayName": f"Model {number}",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "low"},
                                {"reasoningEffort": "high"},
                            ],
                            "defaultReasoningEffort": "low",
                            "isDefault": number == 1,
                        }
                    ],
                    "nextCursor": "page-2" if number == 1 else None,
                },
            }
        )
    elif method == "turn/interrupt":
        interrupts.append(params)
        send({"id": request_id, "result": {}})
        run = active.get(params["threadId"])
        if (
            run
            and run["turn"] == params["turnId"]
            and run["prompt"] != "Ignore interrupt."
        ):
            complete(run, status="interrupted")
    elif method == "turn/start":
        thread = params["threadId"]
        if params["input"][0]["text"] == "Reject turn.":
            send({"id": request_id, "error": {"code": -32602, "message": "rejected"}})
            continue
        if thread in active:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32600, "message": "already running"},
                }
            )
            continue
        sequence += 1
        prompt = params["input"][0]["text"]
        run = {
            "thread": thread,
            "turn": f"turn-{sequence}",
            "params": params,
            "prompt": prompt,
        }
        active[thread] = run
        if prompt == "Exit server.":
            sys.exit(2)
        if prompt == "Invalid JSON.":
            sys.stdout.write("not json\n")
            sys.stdout.flush()
            continue
        if prompt == "Complete before start response.":
            complete(run, "early completion")
            reply_start(request_id, run)
            continue
        if prompt == "Delay start response.":
            threading.Thread(
                target=reply_start, args=(request_id, run, 0.15), daemon=True
            ).start()
            continue
        reply_start(request_id, run)
        send(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread,
                    "turn": {"id": run["turn"], "status": "inProgress"},
                },
            }
        )
        if prompt in {"Hold turn.", "Ignore interrupt."}:
            continue
        if prompt == "Stream then hold.":
            send(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread,
                        "turnId": run["turn"],
                        "delta": "started",
                    },
                }
            )
            continue
        if prompt == "Stale turn events.":
            item(run, "stale message", turn="turn-stale")
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread,
                        "turn": {"id": "turn-stale", "status": "completed"},
                    },
                }
            )
        if prompt.startswith("Parallel "):
            parallel.append(run)
            if len(parallel) == 2:
                first, second = parallel
                item(first, first["prompt"])
                item(second, second["prompt"])
                complete(second)
                complete(first)
                parallel.clear()
            continue
        if prompt == "Stream deltas.":
            for delta in ("hello", " world"):
                send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread,
                            "turnId": run["turn"],
                            "delta": delta,
                        },
                    }
                )
            complete(run, "hello world")
            continue
        tools = threads[thread].get("dynamicTools", [])
        if prompt == "Concurrent tools.":
            run["toolCalls"], run["toolResponses"] = {}, {}
            for value, tool in enumerate(tools, start=1):
                call_id = f"call-{value}"
                request_id = f"request-{value}-{run['turn']}"
                run["toolCalls"][request_id] = (call_id, tool["name"])
                pending_tools[request_id] = run
                send(
                    {
                        "id": request_id,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": thread,
                            "turnId": run["turn"],
                            "callId": call_id,
                            "tool": tool["name"],
                            "arguments": {"value": value},
                        },
                    }
                )
            continue
        name = (
            "unavailable"
            if prompt == "Request an unavailable tool."
            else (tools[0]["name"] if tools else None)
        )
        if name:
            tool_id = "tool-" + run["turn"]
            pending_tools[tool_id] = run
            arguments = {
                "value": "wrong type" if prompt == "Invalid tool arguments." else 7
            }
            if prompt == "Structured tool arguments.":
                arguments = {
                    "values": [1, 2],
                    "mode": "sum",
                    "metadata": {"tag": "test"},
                }
            send(
                {
                    "id": tool_id,
                    "method": "item/tool/call",
                    "params": {
                        "threadId": thread,
                        "turnId": run["turn"],
                        "callId": tool_id,
                        "tool": name,
                        "arguments": arguments,
                    },
                }
            )
        else:
            complete(run, payload(run))
