"""Public client; protocol and execution machinery live in private modules."""

from __future__ import annotations

import asyncio
import copy
import math
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ._backend import AgentSpec, Backend
from ._config import (
    _merge_config,
    _prepare_integration_config,
    _resolve_cwd,
    _unique_names,
)
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
from ._runtime import Runtime, Submission, wakeup_fallback
from ._tools import Tool
from .errors import CodexAppServerError, CodexBusyError, CodexCancelledError
from .errors import CodexProtocolError as CodexProtocolError
from .errors import CodexServerExited as CodexServerExited
from .errors import CodexTimeoutError as CodexTimeoutError
from .models import ModelInfo

JsonObject = dict[str, Any]
ToolHandler = Callable[[JsonObject], Any]
EventHandler = Callable[[JsonObject], Any]


def _positive_timeout(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and greater than zero")
    return value


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _options(model=None, effort=None, summary=None, service_tier=None) -> dict:
    result = {}
    for key, value in (
        ("model", model),
        ("effort", effort),
        ("summary", summary),
        ("serviceTier", service_tier),
    ):
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
            result[key] = value
    return result


def _local_schema(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key in {"$ref", "$dynamicRef"}
                and isinstance(item, str)
                and not item.startswith("#")
            ):
                raise ValueError("tool schemas may only reference local definitions")
            _local_schema(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema(item)


class CodexAppServer:
    """Embed one persistent Codex process with a shared async execution backend.

    Existing string-ID methods remain supported. Prefer :meth:`agent` for a
    handle whose runs return :class:`RunResult` instead of protocol messages.
    """

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        codex_home: str | os.PathLike[str] | None = None,
        server_command: Sequence[str] | None = None,
        timeout: float = 120,
        max_concurrent_runs: int = 32,
        max_concurrent_tools: int = 16,
        tool_timeout: float = 120,
        cancel_timeout: float = 2,
        event_buffer: int = 4096,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        if not self.project_root.is_dir():
            raise ValueError(f"project root does not exist: {self.project_root}")
        self.codex_home = (
            Path(codex_home).expanduser().resolve()
            if codex_home is not None
            else self.project_root / ".theseus"
        )
        ensure_private_directory(self.codex_home)
        ensure_private_directory(self.codex_home / "skills")
        self._command = tuple(server_command or ("codex", "app-server", "--stdio"))
        self._timeout = _positive_timeout(timeout)
        self._backend_options = dict(
            timeout=self._timeout,
            max_runs=_positive_int(max_concurrent_runs, "max_concurrent_runs"),
            max_tools=_positive_int(max_concurrent_tools, "max_concurrent_tools"),
            tool_timeout=_positive_timeout(tool_timeout),
            cancel_timeout=_positive_timeout(cancel_timeout),
            event_buffer=_positive_int(event_buffer, "event_buffer"),
        )
        self._lock = threading.RLock()
        self._runtime: Runtime | None = None
        self._backend: Backend | None = None
        self._closing = False
        self._tools: dict[str, Tool] = {}
        self._agents: dict[str, AgentSpec] = {}
        self._skills = {
            file.parent.name: file
            for file in (self.codex_home / "skills").glob("*/SKILL.md")
        }

    def _submit(self, method: str, *args, operation=False, **kwargs) -> Submission:
        with self._lock:
            if self._closing:
                raise CodexBusyError("client is closing")
            if self._runtime is None:
                self._runtime = Runtime()
                self._backend = Backend(
                    self._command,
                    self.project_root,
                    self.codex_home,
                    self._agents,
                    **self._backend_options,
                )
            function = getattr(self._backend, method)
            coroutine = (
                self._backend.operation(function, *args, **kwargs)
                if operation
                else function(*args, **kwargs)
            )
            return self._runtime.submit(coroutine)

    def _sync(self, method, *args, **kwargs):
        if (
            self._runtime is not None
            and threading.current_thread() is self._runtime.thread
        ):
            raise RuntimeError("use the async API inside an async tool or callback")
        try:
            return self._submit(method, *args, **kwargs).result.result()
        except asyncio.CancelledError:
            raise CodexCancelledError("run was cancelled") from None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()

    async def __aenter__(self):
        await self.start_async()
        return self

    async def __aexit__(self, *_):
        await self.close_async()

    def start(self):
        self._sync("start", operation=True)

    async def start_async(self):
        await self._submit("start", operation=True).wait()

    def _begin_close(self):
        with self._lock:
            if self._runtime is None or self._closing:
                return None
            if threading.current_thread() is self._runtime.thread:
                raise RuntimeError(
                    "close the client from its owning application, not an async tool"
                )
            self._closing = True
            return self._runtime, self._runtime.submit(self._backend.close())

    def _finish_close(self, runtime):
        runtime.stop()
        with self._lock:
            self._runtime = self._backend = None
            self._closing = False

    def close(self):
        closing = self._begin_close()
        if closing is not None:
            runtime, submission = closing
            try:
                submission.result.result()
            finally:
                self._finish_close(runtime)

    async def close_async(self):
        closing = self._begin_close()
        if closing is not None:
            runtime, submission = closing
            try:
                # Closing is cleanup: do not propagate caller cancellation into it.
                future = asyncio.wrap_future(submission.result)
                try:
                    with wakeup_fallback(asyncio.get_running_loop()):
                        await asyncio.shield(future)
                except asyncio.CancelledError:
                    with wakeup_fallback(asyncio.get_running_loop()):
                        await asyncio.shield(future)
                    raise
            finally:
                self._finish_close(runtime)

    def import_auth(self, source_codex_home: str | os.PathLike[str]) -> Path:
        """Explicitly copy authentication into this project's isolated home."""
        with self._lock:
            if self._backend is not None and self._backend.transport.alive:
                raise CodexAppServerError(
                    "close app-server before importing authentication"
                )
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
    ):
        """Register a validated sync or async callback for new/resumed agents."""
        validate_tool_name(name)
        if not description.strip():
            raise ValueError("tool description cannot be empty")
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        schema = copy.deepcopy(validate_json_object(input_schema, label="input_schema"))
        _local_schema(schema)
        Draft202012Validator.check_schema(schema)
        with self._lock:
            self._tools[name] = Tool(
                name, description.strip(), schema, handler, Draft202012Validator(schema)
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

        with self._lock:
            self._skills[name] = skill_file
        return skill_file

    def _spec(
        self,
        *,
        tools=(),
        skills=(),
        model=None,
        effort=None,
        cwd=None,
        sandbox="workspace-write",
        approval_policy="never",
        developer_instructions=None,
        inherit_integrations=False,
        integration_config=None,
    ) -> AgentSpec:
        defaults = _options(model, effort)
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError(f"unsupported sandbox mode: {sandbox}")
        if approval_policy not in {"untrusted", "on-request", "never"}:
            raise ValueError(f"unsupported approval policy: {approval_policy}")
        with self._lock:
            names = _unique_names(tools, self._tools, "tool")
            selected = _unique_names(skills, self._skills, "skill")
            catalog = {name: self._tools[name] for name in names}
            config = _prepare_integration_config(
                inherit_integrations, integration_config
            )
            config = _merge_config(
                config,
                {
                    "skills": {
                        "config": [
                            {"path": str(path), "enabled": name in selected}
                            for name, path in sorted(self._skills.items())
                        ]
                    }
                },
            )
        if effort is not None:
            config["model_reasoning_effort"] = effort
        params = {
            "cwd": str(_resolve_cwd(self.project_root, cwd)),
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "dynamicTools": [tool.protocol_spec() for tool in catalog.values()],
            "config": config,
        }
        if model is not None:
            params["model"] = model
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        return AgentSpec(params, catalog, defaults)

    def create_agent(
        self,
        *,
        tools: Sequence[str] = (),
        skills: Sequence[str] = (),
        model: str | None = None,
        effort: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        developer_instructions: str | None = None,
        inherit_integrations: bool = False,
        integration_config: Mapping | None = None,
    ) -> str:
        """Create a thread with explicitly selected tools, skills, and integrations."""
        spec = self._spec(
            tools=tools,
            skills=skills,
            model=model,
            effort=effort,
            cwd=cwd,
            sandbox=sandbox,
            approval_policy=approval_policy,
            developer_instructions=developer_instructions,
            inherit_integrations=inherit_integrations,
            integration_config=integration_config,
        )
        return self._sync("create", spec, operation=True)

    async def create_agent_async(self, **options) -> str:
        return await self._submit(
            "create", self._spec(**options), operation=True
        ).wait()

    def resume_agent(
        self,
        thread_id: str,
        *,
        tools: Sequence[str] = (),
        skills: Sequence[str] = (),
        inherit_integrations: bool = False,
        integration_config: Mapping | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Reattach capabilities; contact the server lazily on the next run."""
        self._thread_id(thread_id)
        spec = self._spec(
            tools=tools,
            skills=skills,
            inherit_integrations=inherit_integrations,
            integration_config=integration_config,
            model=model,
            effort=effort,
        )
        return self._sync("resume", thread_id, spec)

    async def resume_agent_async(self, thread_id: str, **options) -> str:
        self._thread_id(thread_id)
        return await self._submit("resume", thread_id, self._spec(**options)).wait()

    def agent(self, **options):
        from .agent import Agent

        return Agent(self, self.create_agent(**options))

    async def agent_async(self, **options):
        from .agent import Agent

        return Agent(self, await self.create_agent_async(**options))

    def resume(self, thread_id: str, **options):
        from .agent import Agent

        return Agent(self, self.resume_agent(thread_id, **options))

    async def resume_async(self, thread_id: str, **options):
        from .agent import Agent

        return Agent(self, await self.resume_agent_async(thread_id, **options))

    @staticmethod
    def _thread_id(thread_id):
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id cannot be empty")

    def _run_submission(
        self,
        prompt,
        thread_id,
        *,
        timeout=None,
        on_event=None,
        output_schema=None,
        additional_context=None,
        model=None,
        effort=None,
        summary=None,
        service_tier=None,
        keep_raw=False,
        caller_loop=None,
    ):
        self._thread_id(thread_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt cannot be empty")
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            **_options(model, effort, summary, service_tier),
        }
        if output_schema is not None:
            params["outputSchema"] = validate_json_object(
                output_schema, label="output_schema"
            )
        if additional_context is not None:
            context = {}
            for source, value in additional_context.items():
                if not isinstance(source, str) or not source.strip():
                    raise ValueError(
                        "additional_context keys must be non-empty strings"
                    )
                if not isinstance(value, str):
                    raise TypeError("additional_context values must be strings")
                context[source] = {"kind": "application", "value": value}
            if context:
                params["additionalContext"] = context
        fallback = self._agents.get(thread_id) or self._spec()
        return self._submit(
            "run",
            thread_id,
            params,
            fallback,
            timeout=self._timeout if timeout is None else _positive_timeout(timeout),
            on_event=on_event,
            keep_raw=keep_raw,
            caller_loop=caller_loop,
        )

    def run(
        self,
        prompt: str,
        thread_id: str,
        *,
        timeout=None,
        on_event=None,
        output_schema=None,
        additional_context=None,
        model=None,
        effort=None,
        summary=None,
        service_tier=None,
    ) -> list[JsonObject]:
        """Return raw messages. Use agent.run() for structured results."""
        if (
            self._runtime is not None
            and threading.current_thread() is self._runtime.thread
        ):
            raise RuntimeError("use run_async inside an async tool")
        try:
            result = self._run_submission(
                prompt,
                thread_id,
                timeout=timeout,
                on_event=on_event,
                output_schema=output_schema,
                additional_context=additional_context,
                model=model,
                effort=effort,
                summary=summary,
                service_tier=service_tier,
                keep_raw=True,
            ).result.result()
            return list(result.raw_events)
        except asyncio.CancelledError:
            raise CodexCancelledError("run was cancelled") from None

    async def run_async(
        self, prompt: str, thread_id: str, **options
    ) -> list[JsonObject]:
        with wakeup_fallback(asyncio.get_running_loop()):
            result = await self._run_submission(
                prompt,
                thread_id,
                keep_raw=True,
                caller_loop=asyncio.get_running_loop(),
                **options,
            ).wait()
        return list(result.raw_events)

    def cancel(self, thread_id: str) -> bool:
        return self._sync("cancel", thread_id)

    async def cancel_async(self, thread_id: str) -> bool:
        return await self._submit("cancel", thread_id).wait()

    def configure(
        self,
        thread_id: str,
        *,
        model=None,
        effort=None,
        summary=None,
        service_tier=None,
    ):
        self._sync(
            "configure", thread_id, _options(model, effort, summary, service_tier)
        )

    async def configure_async(self, thread_id: str, **options):
        await self._submit("configure", thread_id, _options(**options)).wait()

    def models(self, *, include_hidden=False) -> tuple[ModelInfo, ...]:
        return self._sync("models", include_hidden, operation=True)

    async def models_async(self, *, include_hidden=False) -> tuple[ModelInfo, ...]:
        return await self._submit("models", include_hidden, operation=True).wait()


def final_text(messages: Sequence[JsonObject]) -> str | None:
    """Extract the last completed agent message from raw events."""
    for message in reversed(messages):
        if message.get("method") == "item/completed":
            item = message.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                return item["text"]
    return None
