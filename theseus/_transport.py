"""Private asyncio JSON-RPC transport. Never executes application callbacks."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from ._version import __version__
from .errors import CodexProtocolError, CodexServerExited


class RequestRejected(CodexProtocolError):
    """A JSON-RPC error response: the requested operation was not accepted."""


class Transport:
    def __init__(
        self,
        command: Sequence[str],
        project: Path,
        home: Path,
        on_message: Callable,
        on_error: Callable,
    ):
        self.command, self.project, self.home = command, project, home
        self.on_message, self.on_error = on_message, on_error
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.tasks: list[asyncio.Task] = []
        self.failure: BaseException | None = None

    @property
    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.returncode is None
            and self.failure is None
        )

    async def start(self):
        environment = os.environ.copy()
        environment.update(CODEX_HOME=str(self.home), CODEX_SQLITE_HOME=str(self.home))
        self.failure = None
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.project,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self.tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._drain_stderr()),
        ]
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "theseus",
                    "title": "Theseus Python module",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized", "params": {}})

    async def send(self, message: dict):
        if not self.alive or self.process.stdin is None:
            raise CodexServerExited("Codex app-server connection is closed")
        try:
            self.process.stdin.write(
                (
                    json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode()
            )
            await self.process.stdin.drain()
        except (OSError, ConnectionError) as error:
            raise CodexServerExited("Codex app-server connection is closed") from error

    async def request(
        self, method: str, params: dict | None = None, *, raw: bool = False
    ) -> dict:
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(
                {"id": request_id, "method": method, "params": params or {}}
            )
            message = await future
            if message.get("error") is not None:
                # Do not embed server-provided prompts, arguments, or credentials in errors.
                code = (
                    message["error"].get("code")
                    if isinstance(message["error"], dict)
                    else None
                )
                raise RequestRejected(f"{method} rejected by app-server (code {code})")
            if not isinstance(message.get("result", {}), dict):
                raise CodexProtocolError(f"{method} returned a non-object result")
            return message if raw else message.get("result", {})
        finally:
            self.pending.pop(request_id, None)

    def _fail(self, error: BaseException):
        if self.failure is not None:
            return
        self.failure = error
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.on_error(error)

    async def _read(self):
        try:
            assert self.process and self.process.stdout
            while line := await self.process.stdout.readline():
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise CodexProtocolError(
                        "app-server returned invalid JSON"
                    ) from None
                if not isinstance(message, dict):
                    raise CodexProtocolError("app-server returned a non-object message")
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    self.on_message(message)
            self._fail(CodexServerExited("Codex app-server closed stdout"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)

    async def _drain_stderr(self):
        assert self.process and self.process.stderr
        # Drain without retaining potentially sensitive runtime output.
        while await self.process.stderr.read(65536):
            pass

    async def close(self):
        self._fail(CodexServerExited("Codex app-server was closed"))
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
