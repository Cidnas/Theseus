"""Adapt ordinary typed Python functions into Codex dynamic tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, get_type_hints

from .app_server import CodexAppServer


ToolFunction = Callable[..., Any]
JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def register_tools(
    codex: CodexAppServer, functions: Iterable[ToolFunction]
) -> tuple[str, ...]:
    """Register typed Python functions and return their dynamic-tool names."""

    return tuple(_register_tool(codex, function) for function in functions)


def _register_tool(codex: CodexAppServer, function: ToolFunction) -> str:
    """Inspect and register one ordinary Python function as a dynamic tool."""

    if not inspect.isfunction(function):
        raise TypeError("tool must be a Python function")
    if inspect.iscoroutinefunction(function):
        raise TypeError("async tool functions are not supported")

    description = inspect.getdoc(function)
    if not description:
        raise ValueError(f"tool function {function.__name__!r} requires a docstring")

    codex.add_tool(
        function.__name__,
        description,
        _input_schema(function),
        _handler(function),
    )
    return function.__name__


def _handler(function: ToolFunction) -> Callable[[dict[str, Any]], Any]:
    @wraps(function)
    def call(arguments: dict[str, Any]) -> Any:
        return function(**arguments)

    return call


def _input_schema(function: ToolFunction) -> dict[str, Any]:
    signature = inspect.signature(function)
    try:
        hints = get_type_hints(function)
    except (NameError, TypeError) as error:
        raise TypeError(
            f"could not resolve annotations for tool {function.__name__!r}"
        ) from error

    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise TypeError(
                f"tool parameter {parameter.name!r} must accept keyword arguments"
            )
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            raise TypeError(
                f"tool parameter {parameter.name!r} requires a type annotation"
            )
        schema_type = JSON_TYPES.get(annotation)
        if schema_type is None:
            raise TypeError(
                f"tool parameter {parameter.name!r} must use str, int, float, or bool"
            )
        schema: dict[str, Any] = {"type": schema_type}
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            try:
                json.dumps(parameter.default)
            except (TypeError, ValueError) as error:
                raise TypeError(
                    f"default for tool parameter {parameter.name!r} must be JSON serializable"
                ) from error
            schema["default"] = parameter.default
        properties[parameter.name] = schema

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result
