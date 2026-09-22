"""Adapt documented Python functions into schema-checked dynamic tools."""

from __future__ import annotations

import inspect
import json
import types
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator

from .app_server import CodexAppServer

JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


def register_tools(
    codex: CodexAppServer, functions: Iterable[Callable[..., Any]]
) -> tuple[str, ...]:
    """Register sync/async functions with primitive, list, dict, union, or Literal inputs."""
    prepared = [_prepare(function) for function in functions]
    for function, description, schema in prepared:
        codex.add_tool(function.__name__, description, schema, _handler(function))
    return tuple(function.__name__ for function, _, _ in prepared)


def _handler(function: Callable) -> Callable:
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def call_async(arguments):
            return await function(**arguments)

        return call_async

    @wraps(function)
    def call(arguments):
        return function(**arguments)

    return call


def _prepare(function):
    if not inspect.isfunction(function):
        raise TypeError("tool must be a Python function")
    description = inspect.getdoc(function)
    if not description:
        raise ValueError(f"tool function {function.__name__!r} requires a docstring")
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as error:
        raise TypeError(
            f"could not resolve annotations for tool {function.__name__!r}"
        ) from error
    properties, required = {}, []
    for parameter in inspect.signature(function).parameters.values():
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
        schema = _schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            json.dumps(parameter.default, allow_nan=False)
            if not Draft202012Validator(schema).is_valid(parameter.default):
                raise TypeError(
                    f"default for {parameter.name!r} does not match its annotation"
                )
            schema["default"] = parameter.default
        properties[parameter.name] = schema
    result = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        result["required"] = required
    return function, description, result


def _schema(annotation) -> dict:
    origin, arguments = get_origin(annotation), get_args(annotation)
    if annotation in JSON_TYPES:
        return {"type": JSON_TYPES[annotation]}
    if origin is Annotated:
        schema = _schema(arguments[0])
        descriptions = [value for value in arguments[1:] if isinstance(value, str)]
        if descriptions:
            schema["description"] = " ".join(descriptions)
        return schema
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(item) for item in arguments]}
    if origin is Literal:
        if not arguments or any(type(value) not in JSON_TYPES for value in arguments):
            raise TypeError("Literal tool parameters must contain JSON scalar values")
        return {"enum": list(arguments)}
    if origin is list and len(arguments) == 1:
        return {"type": "array", "items": _schema(arguments[0])}
    if origin is dict and len(arguments) == 2 and arguments[0] is str:
        return {"type": "object", "additionalProperties": _schema(arguments[1])}
    raise TypeError(f"unsupported tool parameter annotation: {annotation!r}")
