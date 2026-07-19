"""Validation and filesystem helpers for :mod:`codeagent.app_server`."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_tool_name(name: str) -> str:
    if not _TOOL_NAME.fullmatch(name):
        raise ValueError(
            "tool names must contain 1-128 letters, numbers, underscores, or hyphens"
        )
    return name


def validate_skill_name(name: str) -> str:
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError(
            "skill names must be lowercase kebab-case and at most 64 characters"
        )
    return name


def validate_json_object(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    result = dict(value)
    try:
        json.dumps(result)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON serializable") from error
    return result


def safe_resource_path(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"unsafe skill resource path: {path!r}")
    if candidate.parts[0] == "SKILL.md":
        raise ValueError("resources cannot replace SKILL.md")
    return candidate


def safe_child_path(root: Path, relative_path: PurePosixPath) -> Path:
    target = root.joinpath(*relative_path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"skill resource escapes its directory: {relative_path}")
    return target


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Codex home must be a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        # Some filesystems do not implement POSIX permissions.
        pass


def write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def skill_markdown(name: str, description: str, instructions: str) -> str:
    # JSON strings are valid YAML scalars and avoid hand-written escaping bugs.
    return (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description.strip(), ensure_ascii=False)}\n"
        "---\n\n"
        f"{instructions.strip()}\n"
    )
