"""A small, synchronous client for the Codex app-server protocol.

The public class deliberately exposes only the operations needed to construct
and run an agent. Transport lifecycle, JSON-RPC routing, streamed events, and
dynamic tool callbacks remain internal to the module.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from ._helpers import (
    ensure_private_directory,
    safe_child_path,
    safe_resource_path,
    skill_markdown,
    validate_json_object,
    validate_skill_name,
    validate_tool_name,
    write_private_text,
)


JsonObject: TypeAlias = dict[str, Any]
ToolHandler: TypeAlias = Callable[[JsonObject], Any]
_EOF = object()


class CodexAppServerError(RuntimeError):
    """Base error for the app-server client."""


class CodexProtocolError(CodexAppServerError):
    """Raised when app-server returns invalid JSON or a JSON-RPC error."""


class CodexTimeoutError(CodexAppServerError):
    """Raised when app-server does not finish an operation in time."""


class CodexServerExited(CodexAppServerError):
    """Raised when app-server exits before completing an operation."""


@dataclass(frozen=True, slots=True)
class _Tool:
    name: str
    description: str
    input_schema: JsonObject
    handler: ToolHandler

    def protocol_spec(self) -> JsonObject:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass(frozen=True, slots=True)
class _Agent:
    tools: tuple[str, ...]
    skills: tuple[str, ...]


class CodexAppServer:
    """Run Codex threads through an isolated, project-local app-server.

    By default all Codex state is written under ``<project>/.codex-agent``.
    Authentication is never silently copied from the host Codex installation;
    call :meth:`import_auth` explicitly or authenticate that local home.
    """

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        codex_home: str | os.PathLike[str] | None = None,
        server_command: Sequence[str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        if not self.project_root.is_dir():
            raise ValueError(f"project root does not exist: {self.project_root}")

        self.codex_home = (
            Path(codex_home).expanduser().resolve()
            if codex_home is not None
            else self.project_root / ".codex-agent"
        )
        ensure_private_directory(self.codex_home)
        ensure_private_directory(self.codex_home / "skills")

        command = server_command or ("codex", "app-server", "--stdio")
        if not command:
            raise ValueError("server_command cannot be empty")
        self._server_command = tuple(os.fspath(part) for part in command)
        self._timeout = _positive_timeout(timeout)

        self._tools: dict[str, _Tool] = {}
        self._skills: dict[str, Path] = {}
        self._agents: dict[str, _Agent] = {}
        self._loaded_threads: set[str] = set()
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[JsonObject | BaseException | object] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=100)
        self._request_id = 0
        self._operation_lock = threading.RLock()
        self._stdin_lock = threading.Lock()

        for skill_file in (self.codex_home / "skills").glob("*/SKILL.md"):
            self._skills[skill_file.parent.name] = skill_file

    def __enter__(self) -> CodexAppServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def import_auth(self, source_codex_home: str | os.PathLike[str]) -> Path:
        """Copy host credentials into the isolated home as an explicit action."""

        with self._operation_lock:
            if self._process is not None and self._process.poll() is None:
                raise CodexAppServerError("close app-server before importing authentication")
            source = Path(source_codex_home).expanduser().resolve() / "auth.json"
            if not source.is_file():
                raise FileNotFoundError(f"Codex auth file not found: {source}")
            target = self.codex_home / "auth.json"
            target.write_bytes(source.read_bytes())
            try:
                target.chmod(0o600)
            except OSError:
                pass
            return target

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        handler: ToolHandler,
    ) -> None:
        """Register a callable dynamic tool that can be attached to new agents."""

        validate_tool_name(name)
        if not description.strip():
            raise ValueError("tool description cannot be empty")
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        schema = validate_json_object(input_schema, label="input_schema")
        self._tools[name] = _Tool(
            name=name,
            description=description.strip(),
            input_schema=schema,
            handler=handler,
        )

    def add_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        *,
        resources: Mapping[str, str | bytes] | None = None,
    ) -> Path:
        """Create or replace a skill inside the isolated Codex home."""

        validate_skill_name(name)
        if not description.strip():
            raise ValueError("skill description cannot be empty")
        if not instructions.strip():
            raise ValueError("skill instructions cannot be empty")

        skill_dir = self.codex_home / "skills" / name
        ensure_private_directory(skill_dir)
        skill_file = skill_dir / "SKILL.md"
        write_private_text(skill_file, skill_markdown(name, description, instructions))

        for relative_name, content in (resources or {}).items():
            relative_path = safe_resource_path(relative_name)
            target = safe_child_path(skill_dir, relative_path)
            if isinstance(content, bytes):
                target.write_bytes(content)
            elif isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                raise TypeError("skill resources must contain str or bytes values")

        self._skills[name] = skill_file
        return skill_file

    def create_agent(
        self,
        *,
        tools: Sequence[str] = (),
        skills: Sequence[str] = (),
        model: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        developer_instructions: str | None = None,
    ) -> str:
        """Create a thread with exactly the requested registered capabilities."""

        with self._operation_lock:
            self._start_unlocked()
            tool_names = _unique_names(tools, self._tools, "tool")
            skill_names = _unique_names(skills, self._skills, "skill")
            if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
                raise ValueError(f"unsupported sandbox mode: {sandbox}")
            if approval_policy not in {"untrusted", "on-request", "never"}:
                raise ValueError(f"unsupported approval policy: {approval_policy}")
            params: JsonObject = {
                "cwd": str(_resolve_cwd(self.project_root, cwd)),
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "dynamicTools": [self._tools[name].protocol_spec() for name in tool_names],
                "config": self._skill_config(skill_names),
            }
            if model is not None:
                params["model"] = model
            if developer_instructions is not None:
                params["developerInstructions"] = developer_instructions

            result = self._request_unlocked("thread/start", params)
            thread_id = _nested_string(result, "thread", "id")
            if thread_id is None:
                raise CodexProtocolError("thread/start response did not contain thread.id")
            self._agents[thread_id] = _Agent(tool_names, skill_names)
            self._loaded_threads.add(thread_id)
            return thread_id

    def run(
        self,
        prompt: str,
        thread_id: str,
        *,
        timeout: float | None = None,
    ) -> list[JsonObject]:
        """Run one prompt and return every raw app-server message for the turn."""

        if not prompt.strip():
            raise ValueError("prompt cannot be empty")
        if not thread_id.strip():
            raise ValueError("thread_id cannot be empty")
        operation_timeout = self._timeout if timeout is None else _positive_timeout(timeout)

        with self._operation_lock:
            self._start_unlocked()
            agent = self._agents.setdefault(thread_id, _Agent((), ()))
            if thread_id not in self._loaded_threads:
                self._request_unlocked(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "dynamicTools": [
                            self._tools[name].protocol_spec()
                            for name in agent.tools
                            if name in self._tools
                        ],
                        "config": self._skill_config(agent.skills),
                    },
                )
                self._loaded_threads.add(thread_id)

            events: list[JsonObject] = []
            result = self._request_unlocked(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
                events=events,
                timeout=operation_timeout,
            )
            turn_id = _nested_string(result, "turn", "id")
            deadline = time.monotonic() + operation_timeout

            while True:
                message = self._next_message(deadline)
                events.append(message)
                if self._is_server_request(message):
                    self._answer_server_request(message)
                    continue
                if message.get("method") != "turn/completed":
                    continue
                params = message.get("params")
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    continue
                completed_turn_id = _nested_string(params, "turn", "id")
                if turn_id is None or completed_turn_id == turn_id:
                    return events

    def start(self) -> None:
        """Start app-server and perform the required initialize handshake."""

        with self._operation_lock:
            self._start_unlocked()

    def close(self) -> None:
        """Stop the child app-server process."""

        with self._operation_lock:
            process, self._process = self._process, None
            self._loaded_threads.clear()
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _start_unlocked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        self._messages = queue.Queue()
        self._stderr.clear()
        self._loaded_threads.clear()
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        environment["CODEX_SQLITE_HOME"] = str(self.codex_home)
        try:
            process = subprocess.Popen(
                self._server_command,
                cwd=self.project_root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as error:
            raise CodexAppServerError(
                f"could not start app-server: {' '.join(self._server_command)}"
            ) from error

        self._process = process
        threading.Thread(target=self._read_stdout, args=(process,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        try:
            self._request_unlocked(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codeagent",
                        "title": "codeAgent Python module",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _request_unlocked(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        events: list[JsonObject] | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        self._request_id += 1
        request_id = self._request_id
        request: JsonObject = {"method": method, "id": request_id}
        if params is not None:
            request["params"] = params
        self._send(request)
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)

        while True:
            message = self._next_message(deadline)
            if events is not None:
                events.append(message)
            if self._is_server_request(message):
                self._answer_server_request(message)
                continue
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if error is not None:
                raise CodexProtocolError(f"{method} failed: {error}")
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise CodexProtocolError(f"{method} returned a non-object result")
            return result

    def _answer_server_request(self, message: JsonObject) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if method != "item/tool/call" or not isinstance(params, dict):
            self._send(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unsupported server request: {method}"},
                }
            )
            return

        tool_name = params.get("tool")
        thread_id = params.get("threadId")
        agent = self._agents.get(thread_id) if isinstance(thread_id, str) else None
        allowed = agent is not None and tool_name in agent.tools
        tool = self._tools.get(tool_name) if isinstance(tool_name, str) and allowed else None
        if tool is None:
            result = _tool_result(f"unknown or unavailable tool: {tool_name}", success=False)
        else:
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                result = _tool_result("tool arguments must be a JSON object", success=False)
            else:
                try:
                    result = _tool_result(tool.handler(arguments), success=True)
                except Exception as error:  # Tool failures are data returned to the agent.
                    result = _tool_result(f"{type(error).__name__}: {error}", success=False)
        self._send({"id": request_id, "result": result})

    def _skill_config(self, selected: tuple[str, ...]) -> JsonObject:
        enabled = set(selected)
        return {
            "skills": {
                "config": [
                    {"path": str(path), "enabled": name in enabled}
                    for name, path in sorted(self._skills.items())
                ]
            }
        }

    def _send(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise self._server_exited()
        serialized = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._stdin_lock:
            try:
                process.stdin.write(serialized + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise self._server_exited() from error

    def _next_message(self, deadline: float) -> JsonObject:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexTimeoutError("timed out waiting for app-server")
        try:
            item = self._messages.get(timeout=remaining)
        except queue.Empty as error:
            raise CodexTimeoutError("timed out waiting for app-server") from error
        if item is _EOF:
            raise self._server_exited()
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, dict):
            raise CodexProtocolError("app-server produced an invalid message")
        return item

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._messages.put(CodexProtocolError(f"invalid app-server JSON: {line!r}"))
                    return
                self._messages.put(message)
        finally:
            self._messages.put(_EOF)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._stderr.append(line.rstrip())

    def _server_exited(self) -> CodexServerExited:
        detail = "\n".join(self._stderr).strip()
        suffix = f"\n{detail}" if detail else ""
        return CodexServerExited(f"Codex app-server exited unexpectedly{suffix}")

    @staticmethod
    def _is_server_request(message: JsonObject) -> bool:
        return "method" in message and "id" in message


def final_text(messages: Sequence[JsonObject]) -> str | None:
    """Return the last completed agent message from a raw turn response."""

    for message in reversed(messages):
        if message.get("method") != "item/completed":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                return text
    return None


def _positive_timeout(value: float) -> float:
    value = float(value)
    if value <= 0:
        raise ValueError("timeout must be greater than zero")
    return value


def _resolve_cwd(
    project_root: Path, cwd: str | os.PathLike[str] | None
) -> Path:
    if cwd is None:
        return project_root
    result = Path(cwd).expanduser()
    if not result.is_absolute():
        result = project_root / result
    result = result.resolve()
    if not result.is_dir():
        raise ValueError(f"working directory does not exist: {result}")
    return result


def _unique_names(
    names: Sequence[str], registry: Mapping[str, Any], capability: str
) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(names))
    missing = [name for name in result if name not in registry]
    if missing:
        raise KeyError(f"unknown {capability}(s): {', '.join(missing)}")
    return result


def _nested_string(value: Mapping[str, Any], first: str, second: str) -> str | None:
    nested = value.get(first)
    if not isinstance(nested, dict):
        return None
    result = nested.get(second)
    return result if isinstance(result, str) else None


def _tool_result(value: Any, *, success: bool) -> JsonObject:
    if isinstance(value, list) and all(
        isinstance(item, dict) and item.get("type") in {"inputText", "inputImage"}
        for item in value
    ):
        content = value
    elif isinstance(value, str):
        content = [{"type": "inputText", "text": value}]
    else:
        content = [
            {
                "type": "inputText",
                "text": json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
            }
        ]
    return {"contentItems": content, "success": success}
