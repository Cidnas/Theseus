"""One event loop per client, shared by synchronous and asynchronous callers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import socket
import threading
from collections.abc import Coroutine
from contextlib import contextmanager
from typing import Any


def _socket_wakeups_available() -> bool:
    # Some restricted Linux hosts permit socketpair() but deny send(). Asyncio
    # silently ignores that error in call_soon_threadsafe(), leaving loops asleep.
    try:
        writer, reader = socket.socketpair()
        with writer, reader:
            writer.send(b"x")
        return True
    except OSError:
        return False


_NEEDS_HEARTBEAT = not _socket_wakeups_available()


class PipeWakeupLoop(asyncio.SelectorEventLoop):
    """Use a pipe wakeup on POSIX hosts that prohibit socketpair writes."""

    def __init__(self):
        super().__init__()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        self.add_reader(self.read_fd, self._drain_wakeup)

    def _drain_wakeup(self):
        try:
            while os.read(self.read_fd, 4096):
                pass
        except BlockingIOError:
            pass

    def call_soon_threadsafe(self, callback, *args, context=None):
        handle = super().call_soon_threadsafe(callback, *args, context=context)
        try:
            os.write(self.write_fd, b"x")
        except (BlockingIOError, OSError):
            pass
        return handle

    def close(self):
        if not self.is_closed():
            self.remove_reader(self.read_fd)
            os.close(self.read_fd)
            os.close(self.write_fd)
        super().close()


@contextmanager
def wakeup_fallback(loop):
    handle = None

    def tick():
        nonlocal handle
        handle = loop.call_later(0.01, tick)

    if _NEEDS_HEARTBEAT:
        tick()
    try:
        yield
    finally:
        if handle is not None:
            handle.cancel()


class Submission:
    def __init__(self, loop: asyncio.AbstractEventLoop, coroutine: Coroutine):
        self.loop = loop
        self.result: concurrent.futures.Future = concurrent.futures.Future()
        self.task: asyncio.Task | None = None
        self.cancelled = False

        def launch():
            self.task = loop.create_task(coroutine)
            self.task.add_done_callback(self._finished)
            if self.cancelled:
                self.task.cancel()

        try:
            loop.call_soon_threadsafe(launch)
        except RuntimeError:
            coroutine.close()
            raise

    def _finished(self, task: asyncio.Task):
        try:
            self.result.set_result(task.result())
        except BaseException as error:
            self.result.set_exception(error)

    def cancel(self):
        def cancel_task():
            self.cancelled = True
            if self.task is not None and not self.task.cancelling():
                self.task.cancel()

        self.loop.call_soon_threadsafe(cancel_task)

    async def wait(self) -> Any:
        loop = asyncio.get_running_loop()
        if not _NEEDS_HEARTBEAT or os.name != "posix":
            return await self._wait(asyncio.wrap_future(self.result))
        # Bridge a completed concurrent future through a pipe rather than the
        # caller's (possibly restricted) self-pipe socket. No per-run polling.
        read_fd, write_fd = os.pipe()
        future = loop.create_future()
        guard = threading.Lock()
        closed = False

        def ready():
            loop.remove_reader(read_fd)
            if not future.done():
                try:
                    future.set_result(self.result.result())
                except BaseException as error:
                    future.set_exception(error)

        def finished(_):
            with guard:
                if not closed:
                    os.write(write_fd, b"x")

        loop.add_reader(read_fd, ready)
        self.result.add_done_callback(finished)
        try:
            return await self._wait(future)
        finally:
            loop.remove_reader(read_fd)
            with guard:
                closed = True
                os.close(read_fd)
                os.close(write_fd)

    async def _wait(self, future) -> Any:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel()
            # Wait for backend interruption/cleanup before exposing cancellation.
            try:
                await asyncio.shield(future)
            except (Exception, asyncio.CancelledError):
                pass
            raise


class Runtime:
    def __init__(self):
        self.loop = (
            PipeWakeupLoop()
            if _NEEDS_HEARTBEAT and os.name == "posix"
            else asyncio.new_event_loop()
        )
        self.thread = threading.Thread(target=self._run, name="theseus-io", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coroutine: Coroutine) -> Submission:
        return Submission(self.loop, coroutine)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()
