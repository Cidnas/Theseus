"""Thread lifecycle, turn routing, and tool dispatch on the client's I/O loop."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ._runtime import Submission
from ._tools import Tool, ToolExecutor, tool_result
from ._transport import RequestRejected, Transport
from .errors import (
    CodexBusyError,
    CodexProtocolError,
    CodexTimeoutError,
)
from .models import Collector, ModelInfo, RunResult


@dataclass
class AgentSpec:
    params: dict
    tools: dict[str, Tool]
    defaults: dict = field(default_factory=dict)


@dataclass
class ActiveRun:
    task: asyncio.Task
    queue: asyncio.Queue
    turn_id: str | None = None
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    tools: set[asyncio.Task] = field(default_factory=set)
    seen_calls: set = field(default_factory=set)
    interrupt_sent: bool = False
    start_request: asyncio.Task | None = None
    terminal_turns: set[str] = field(default_factory=set)
    failure: BaseException | None = None


def turn_id(message: dict) -> str | None:
    params = message.get("params", {})
    return params.get("turnId") or params.get("turn", {}).get("id")


class Backend:
    def __init__(
        self,
        command,
        project,
        home,
        agents: dict[str, AgentSpec],
        *,
        timeout: float,
        max_runs: int,
        max_tools: int,
        tool_timeout: float,
        cancel_timeout: float,
        event_buffer: int,
    ):
        self.agents = agents
        self.timeout, self.max_runs = timeout, max_runs
        self.cancel_timeout, self.event_buffer = cancel_timeout, event_buffer
        self.loaded: set[str] = set()
        self.active: dict[str, ActiveRun] = {}
        self.quarantined: set[str] = set()
        self.start_lock = asyncio.Lock()
        self.transport = Transport(command, project, home, self._dispatch, self._fail)
        self.tools = ToolExecutor(max_tools, tool_timeout)
        self.callbacks = ThreadPoolExecutor(
            max_workers=max_runs, thread_name_prefix="theseus-event"
        )
        self.callback_slots = threading.BoundedSemaphore(max_runs)
        self.closing = False

    async def start(self):
        async with self.start_lock:
            if self.transport.alive:
                return
            if self.transport.process is not None:
                await self.transport.close()
            self.loaded.clear()
            self.quarantined.clear()
            try:
                await self.transport.start()
            except BaseException:
                await self.transport.close()
                raise

    async def operation(self, function: Callable, *args, **kwargs):
        try:
            async with asyncio.timeout(self.timeout):
                return await function(*args, **kwargs)
        except TimeoutError:
            raise CodexTimeoutError(
                "app-server operation exceeded its deadline"
            ) from None

    async def create(self, spec: AgentSpec) -> str:
        await self.start()
        result = await self.transport.request("thread/start", spec.params)
        thread = result.get("thread", {}).get("id")
        if not isinstance(thread, str):
            raise CodexProtocolError("thread/start did not return thread.id")
        self.agents[thread] = spec
        self.loaded.add(thread)
        return thread

    async def resume(self, thread: str, spec: AgentSpec):
        self._require_idle(thread)
        self.agents[thread] = spec
        self.loaded.discard(thread)
        return thread

    def _require_idle(self, thread: str):
        if thread in self.active or thread in self.quarantined:
            raise CodexBusyError(
                "thread has an active or unconfirmed turn; cancel it or close the client"
            )

    async def configure(self, thread: str, options: dict):
        self._require_idle(thread)
        self.agents[thread].defaults.update(options)

    async def models(self, include_hidden: bool) -> tuple[ModelInfo, ...]:
        await self.start()
        result, cursor, seen = [], None, set()
        while True:
            page = await self.transport.request(
                "model/list", {"includeHidden": include_hidden, "cursor": cursor}
            )
            for model in page.get("data", []):
                result.append(
                    ModelInfo(
                        model.get("model", model["id"]),
                        model.get("displayName", model["id"]),
                        tuple(
                            option["reasoningEffort"]
                            for option in model.get("supportedReasoningEfforts", [])
                        ),
                        model.get("defaultReasoningEffort"),
                        model.get("isDefault", False),
                    )
                )
            cursor = page.get("nextCursor")
            if cursor is None:
                return tuple(result)
            if cursor in seen:
                raise CodexProtocolError("model/list repeated a pagination cursor")
            seen.add(cursor)

    async def run(
        self,
        thread: str,
        params: dict,
        fallback: AgentSpec,
        *,
        timeout: float,
        on_event: Callable | None,
        keep_raw: bool,
        caller_loop=None,
    ) -> RunResult:
        self._require_idle(thread)
        if len(self.active) >= self.max_runs:
            raise CodexBusyError("run concurrency limit reached")
        state = ActiveRun(asyncio.current_task(), asyncio.Queue(self.event_buffer))
        self.active[thread] = state
        collector = Collector(thread, keep_raw)
        try:
            async with asyncio.timeout(timeout):
                await self.start()
                spec = self.agents.setdefault(thread, fallback)
                if thread not in self.loaded:
                    # Dynamic definitions are persisted by Codex at thread/start.
                    # Current ThreadResumeParams accepts config, not dynamicTools.
                    resume = {"threadId": thread, "config": spec.params["config"]}
                    await self.transport.request("thread/resume", resume)
                    self.loaded.add(thread)
                effective = dict(spec.defaults)
                effective.update(params)
                state.start_request = asyncio.create_task(
                    self.transport.request("turn/start", effective, raw=True)
                )
                # Shield the response so cancellation can recover the actual turn ID.
                response = await asyncio.shield(state.start_request)
                state.turn_id = response.get("result", {}).get("turn", {}).get("id")
                if not isinstance(state.turn_id, str):
                    raise CodexProtocolError("turn/start did not return turn.id")
                if state.turn_id in state.terminal_turns:
                    state.completed.set()
                spec.defaults.update(
                    {
                        key: effective[key]
                        for key in ("model", "effort", "summary", "serviceTier")
                        if key in effective
                    }
                )
                await self._record(response, collector, on_event, caller_loop)
                while True:
                    message = await state.queue.get()
                    if isinstance(message, BaseException):
                        raise message
                    incoming_turn = turn_id(message)
                    if incoming_turn is not None and incoming_turn != state.turn_id:
                        if "id" in message:
                            await self._reject(
                                message, "tool request belongs to another turn"
                            )
                        continue
                    await self._record(message, collector, on_event, caller_loop)
                    if "id" in message and "method" in message:
                        if incoming_turn is None:
                            await self._reject(
                                message, "tool request is missing its turn ID"
                            )
                            continue
                        call = message.get("params", {}).get("callId", message["id"])
                        if call in state.seen_calls:
                            await self._reject(message, "duplicate tool call")
                            continue
                        state.seen_calls.add(call)
                        if len(state.tools) >= self.tools.limit:
                            await self._reject(
                                message, "tool concurrency limit reached"
                            )
                            continue
                        task = asyncio.create_task(
                            self._tool_call(message, spec, caller_loop)
                        )
                        state.tools.add(task)

                        def finished(done, owner=state):
                            owner.tools.discard(done)
                            if not done.cancelled():
                                done.exception()

                        task.add_done_callback(finished)
                    if message.get("method") == "turn/completed":
                        state.completed.set()
                        return collector.finish(message["params"]["turn"])
        except TimeoutError:
            await self._interrupt(thread, state)
            raise CodexTimeoutError("run exceeded its deadline") from None
        except asyncio.CancelledError:
            await self._interrupt(thread, state)
            raise
        except BaseException:
            await self._interrupt(thread, state)
            raise
        finally:
            for task in tuple(state.tools):
                task.cancel()
            await asyncio.gather(*state.tools, return_exceptions=True)
            if self.active.get(thread) is state:
                self.active.pop(thread, None)

    async def _record(self, message, collector, callback, caller_loop):
        collector.add(message)
        if callback is not None:
            if inspect.iscoroutinefunction(callback):
                if (
                    caller_loop is not None
                    and caller_loop is not asyncio.get_running_loop()
                ):
                    await Submission(caller_loop, callback(message)).wait()
                else:
                    await callback(message)
            else:
                if not self.callback_slots.acquire(blocking=False):
                    raise CodexBusyError("event callback concurrency limit reached")
                future = self.callbacks.submit(callback, message)
                future.add_done_callback(lambda _: self.callback_slots.release())
                await asyncio.wrap_future(future)

    async def cancel(self, thread: str) -> bool:
        state = self.active.get(thread)
        if state is None:
            return False
        if not state.task.cancelling():
            state.task.cancel()
        try:
            await asyncio.shield(state.task)
        except (Exception, asyncio.CancelledError):
            pass
        return True

    async def _interrupt(self, thread: str, state: ActiveRun):
        if state.completed.is_set() or self.closing or not self.transport.alive:
            return
        try:
            async with asyncio.timeout(self.cancel_timeout):
                if state.turn_id is None and state.start_request is not None:
                    try:
                        response = await asyncio.shield(state.start_request)
                    except RequestRejected:
                        return
                    state.turn_id = response.get("result", {}).get("turn", {}).get("id")
                if state.turn_id is None:
                    return  # The run was cancelled before sending turn/start.
                # The terminal notification may precede the start response.
                if state.turn_id in state.terminal_turns:
                    state.completed.set()
                    return
                if not state.interrupt_sent:
                    state.interrupt_sent = True
                    await self.transport.request(
                        "turn/interrupt", {"threadId": thread, "turnId": state.turn_id}
                    )
                await state.completed.wait()
        except (Exception, asyncio.CancelledError):
            # Never start a new turn while the previous one's termination is unknown.
            self.quarantined.add(thread)
        finally:
            if state.start_request is not None and not state.start_request.done():
                state.start_request.cancel()
                await asyncio.gather(state.start_request, return_exceptions=True)

    async def _reject(self, message, reason):
        await self.transport.send(
            {"id": message["id"], "result": tool_result(reason, success=False)}
        )

    async def _tool_call(self, message: dict, spec: AgentSpec, caller_loop):
        try:
            if message.get("method") != "item/tool/call":
                await self.transport.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "unsupported server request",
                        },
                    }
                )
                return
            params = message.get("params", {})
            tool = spec.tools.get(params.get("tool"))
            result = (
                await self.tools.call(tool, params.get("arguments", {}), caller_loop)
                if tool
                else tool_result("unknown or unavailable tool", success=False)
            )
            await self.transport.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            if self.transport.alive:
                await self._reject(message, "tool call cancelled")
            raise
        except Exception:
            if self.transport.alive:
                await self._reject(message, "tool dispatch failed")

    def _dispatch(self, message: dict):
        params = message.get("params", {})
        thread = params.get("threadId") or params.get("thread", {}).get("id")
        state = self.active.get(thread)
        if state is None:
            # No registered run means no application callback is authorized.
            if "id" in message:
                task = asyncio.create_task(
                    self._reject(message, "no active run for tool request")
                )
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
            return
        incoming = turn_id(message)
        if message.get("method") == "turn/completed" and incoming is not None:
            if len(state.terminal_turns) < self.event_buffer:
                state.terminal_turns.add(incoming)
            if incoming == state.turn_id:
                state.completed.set()
        if state.failure is not None:
            return
        try:
            state.queue.put_nowait(message)
        except asyncio.QueueFull:
            self._queue_error(
                state,
                CodexBusyError(
                    "event buffer exceeded; consume events faster or increase event_buffer"
                ),
            )

    def _queue_error(self, state, error):
        state.failure = error
        while not state.queue.empty():
            state.queue.get_nowait()
        state.queue.put_nowait(error)

    def _fail(self, error):
        for state in self.active.values():
            if state.start_request is not None:
                self._queue_error(state, error)

    async def close(self):
        self.closing = True
        tasks = [state.task for state in self.active.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.transport.close()
        self.tools.close()
        self.callbacks.shutdown(wait=False, cancel_futures=True)
        self.loaded.clear()
