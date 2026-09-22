"""Convenient agent handles and bounded, application-neutral parallel runs."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import TYPE_CHECKING

from ._runtime import wakeup_fallback
from .errors import CodexBusyError, CodexCancelledError
from .models import RunEvent, RunResult, event_from_message

if TYPE_CHECKING:
    from .app_server import CodexAppServer


class Agent:
    """A resumable conversation with explicitly selected capabilities."""

    def __init__(self, client: CodexAppServer, thread_id: str):
        self._client = client
        self.id = thread_id

    def run(self, prompt: str, *, include_raw: bool = False, **options) -> RunResult:
        runtime = self._client._runtime
        if runtime is not None and threading.current_thread() is runtime.thread:
            raise RuntimeError("use run_async inside an async tool")
        try:
            return self._client._run_submission(
                prompt, self.id, keep_raw=include_raw, **options
            ).result.result()
        except asyncio.CancelledError:
            raise CodexCancelledError("run was cancelled") from None

    async def run_async(
        self, prompt: str, *, include_raw: bool = False, **options
    ) -> RunResult:
        with wakeup_fallback(asyncio.get_running_loop()):
            return await self._client._run_submission(
                prompt,
                self.id,
                keep_raw=include_raw,
                caller_loop=asyncio.get_running_loop(),
                **options,
            ).wait()

    def configure(self, **options):
        self._client.configure(self.id, **options)

    async def configure_async(self, **options):
        await self._client.configure_async(self.id, **options)

    def cancel(self) -> bool:
        return self._client.cancel(self.id)

    async def cancel_async(self) -> bool:
        return await self._client.cancel_async(self.id)

    def stream(
        self, prompt: str, *, buffer_size: int = 256, **options
    ) -> Iterator[RunEvent]:
        """Stream normalized events; closing the iterator interrupts its run."""
        from .app_server import _positive_int

        events = queue.Queue(_positive_int(buffer_size, "buffer_size"))
        sentinel = object()

        def receive(message):
            event = event_from_message(message)
            if event is not None:
                try:
                    events.put_nowait(event)
                except queue.Full:
                    raise CodexBusyError("stream buffer exceeded") from None

        submission = self._client._run_submission(
            prompt, self.id, on_event=receive, **options
        )

        def finished(_):
            try:
                events.put_nowait(sentinel)
            except queue.Full:
                pass  # The consumer also checks completion before waiting.

        submission.result.add_done_callback(finished)
        try:
            while not (submission.result.done() and events.empty()):
                event = events.get()
                if event is sentinel:
                    break
                yield event
            result = submission.result.result()
            yield RunEvent("completed", self.id, result.turn_id, result=result)
        except asyncio.CancelledError:
            raise CodexCancelledError("run was cancelled") from None
        finally:
            if not submission.result.done():
                submission.cancel()
                try:
                    submission.result.result()
                except (Exception, asyncio.CancelledError):
                    pass

    async def stream_async(
        self, prompt: str, *, buffer_size: int = 256, **options
    ) -> AsyncIterator[RunEvent]:
        """Async streaming; use contextlib.aclosing when stopping iteration early."""
        from .app_server import _positive_int

        loop = asyncio.get_running_loop()
        events = asyncio.Queue(_positive_int(buffer_size, "buffer_size"))

        async def put(event):
            await events.put(event)

        async def receive(message):
            event = event_from_message(message)
            if event is not None:
                await put(event)

        submission = self._client._run_submission(
            prompt, self.id, on_event=receive, caller_loop=loop, **options
        )
        result_future = asyncio.wrap_future(submission.result)
        next_event = None
        with wakeup_fallback(loop):
            try:
                while True:
                    if result_future.done() and events.empty():
                        break
                    next_event = asyncio.create_task(events.get())
                    await asyncio.wait(
                        (next_event, result_future), return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_event.done():
                        yield next_event.result()
                    else:
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                    next_event = None
                result = result_future.result()
                yield RunEvent("completed", self.id, result.turn_id, result=result)
            finally:
                if next_event is not None:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                if not result_future.done():
                    submission.cancel()
                try:
                    await asyncio.shield(result_future)
                except (Exception, asyncio.CancelledError):
                    pass


async def run_parallel(
    jobs: Sequence[tuple[Agent, str]], *, limit: int = 4, **options
) -> list[RunResult]:
    """Run agent/prompt pairs with bounded scheduling; return results in input order.

    On failure, cancel and clean up sibling runs before raising. No new agent
    permissions, prompts, or application-specific workflow are invented here.
    """
    from .app_server import _positive_int

    limit = _positive_int(limit, "limit")
    if len({(id(agent._client), agent.id) for agent, _ in jobs}) != len(jobs):
        raise ValueError("parallel jobs must use distinct agents")
    results: list[RunResult | None] = [None] * len(jobs)
    iterator = iter(enumerate(jobs))

    async def worker():
        for index, (agent, prompt) in iterator:
            results[index] = await agent.run_async(prompt, **options)

    tasks = [asyncio.create_task(worker()) for _ in range(min(limit, len(jobs)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return results
