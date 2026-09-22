"""Schema-checked callbacks with bounded sync and async execution."""

from __future__ import annotations

import asyncio
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator

from ._runtime import Submission


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable
    validator: Draft202012Validator

    def protocol_spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def tool_result(value: Any, *, success: bool) -> dict:
    if isinstance(value, list) and all(
        isinstance(item, dict) and item.get("type") in {"inputText", "inputImage"}
        for item in value
    ):
        content = value
    else:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
        content = [{"type": "inputText", "text": text}]
    return {"contentItems": content, "success": success}


class ToolExecutor:
    def __init__(self, limit: int, timeout: float):
        self.limit, self.timeout = limit, timeout
        self.active = 0
        self.pool = ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="theseus-tool"
        )

    async def call(self, tool: Tool, arguments: Any, caller_loop=None) -> dict:
        if not isinstance(arguments, dict):
            return tool_result("tool arguments must be a JSON object", success=False)
        if not tool.validator.is_valid(arguments):
            return tool_result(
                "tool arguments do not match the input schema", success=False
            )
        if self.active >= self.limit:
            return tool_result(
                "tool concurrency limit reached; retry after another call finishes",
                success=False,
            )
        self.active += 1
        loop = asyncio.get_running_loop()
        sync = not (
            inspect.iscoroutinefunction(tool.handler)
            or inspect.iscoroutinefunction(getattr(tool.handler, "__call__", None))
        )
        future = None
        if sync:
            future = self.pool.submit(tool.handler, arguments)

            # Hold the slot until the actual function exits, even after a timeout.
            def finished(_):
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(self._release)
                    except RuntimeError:
                        pass

            future.add_done_callback(finished)
        try:
            async with asyncio.timeout(self.timeout):
                if future is not None:
                    wrapped = asyncio.wrap_future(future)
                    # Observe late failures after the caller times out/cancels.
                    wrapped.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                    value = await asyncio.shield(wrapped)
                else:
                    if caller_loop is not None and caller_loop is not loop:
                        value = await Submission(
                            caller_loop, tool.handler(arguments)
                        ).wait()
                    else:
                        value = await tool.handler(arguments)
                if inspect.isawaitable(value):
                    if inspect.iscoroutine(value):
                        value.close()
                    return tool_result(
                        "tool returned an awaitable; declare its handler async",
                        success=False,
                    )
                return tool_result(value, success=True)
        except TimeoutError:
            return tool_result("tool execution timed out", success=False)
        except Exception as error:
            # Tool exceptions may contain secrets; expose the class, not arbitrary text.
            return tool_result(f"tool failed ({type(error).__name__})", success=False)
        finally:
            if not sync:
                self._release()

    def _release(self):
        self.active -= 1

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
