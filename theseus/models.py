"""Small, protocol-independent results and streaming events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunResult:
    thread_id: str
    turn_id: str
    text: str | None
    status: str
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    raw_events: tuple[dict, ...] = ()


@dataclass(frozen=True)
class RunEvent:
    kind: str
    thread_id: str
    turn_id: str | None = None
    text: str | None = None
    tool_name: str | None = None
    result: RunResult | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    reasoning_efforts: tuple[str, ...]
    default_effort: str | None = None
    is_default: bool = False


def event_from_message(message: dict) -> RunEvent | None:
    method = message.get("method")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return None
    thread, turn = params.get("threadId", ""), params.get("turnId")
    if method == "item/agentMessage/delta":
        return RunEvent("text_delta", thread, turn, text=params.get("delta", ""))
    item = params.get("item", {})
    if method == "item/completed" and item.get("type") == "agentMessage":
        return RunEvent("message", thread, turn, text=item.get("text"))
    if method in {"item/started", "item/completed"} and item.get("type") in {
        "dynamicToolCall",
        "mcpToolCall",
        "commandExecution",
    }:
        return RunEvent(
            "tool_started" if method == "item/started" else "tool_completed",
            thread,
            turn,
            tool_name=item.get("tool", item.get("type")),
        )
    return None


@dataclass
class Collector:
    thread_id: str
    keep_raw: bool
    text: str | None = None
    usage: dict | None = None
    events: list[dict] = field(default_factory=list)

    def add(self, message: dict):
        if self.keep_raw:
            self.events.append(message)
        params = message.get("params", {})
        item = params.get("item", {})
        if (
            message.get("method") == "item/completed"
            and item.get("type") == "agentMessage"
        ):
            if item.get("phase") in (None, "final_answer"):
                self.text = item.get("text")
        if message.get("method") == "thread/tokenUsage/updated":
            self.usage = params.get("tokenUsage")

    def finish(self, turn: dict) -> RunResult:
        return RunResult(
            self.thread_id,
            turn["id"],
            self.text,
            turn.get("status", "completed"),
            turn.get("error"),
            self.usage,
            tuple(self.events),
        )
