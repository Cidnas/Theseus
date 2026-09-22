"""Private thread configuration and capability selection."""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._helpers import validate_json_object

JsonObject = dict[str, Any]
_INTEGRATION_CONFIG_KEYS = frozenset({"apps", "mcp_servers", "plugins", "tool_suggest"})


def _resolve_cwd(project_root: Path, cwd: str | os.PathLike[str] | None) -> Path:
    if cwd is None:
        return project_root
    result = Path(cwd).expanduser()
    if not result.is_absolute():
        result = project_root / result
    result = result.resolve()
    if not result.is_dir():
        raise ValueError(f"working directory does not exist: {result}")
    return result


def _prepare_integration_config(
    inherit_integrations: bool,
    integration_config: Mapping[str, Any] | None,
) -> JsonObject:
    if not isinstance(inherit_integrations, bool):
        raise TypeError("inherit_integrations must be a boolean")

    requested = (
        {}
        if integration_config is None
        else validate_json_object(integration_config, label="integration_config")
    )
    unsupported = sorted(set(requested) - _INTEGRATION_CONFIG_KEYS)
    if unsupported:
        raise ValueError(
            f"unsupported integration config section(s): {', '.join(unsupported)}"
        )
    for name, section in requested.items():
        if not isinstance(section, Mapping):
            raise TypeError(f"integration_config[{name!r}] must be an object")

    if inherit_integrations:
        result: JsonObject = {}
    else:
        result = {
            "features": {
                "apps": False,
                "plugins": False,
                "tool_suggest": False,
            },
            "apps": {"_default": {"enabled": False}},
            "mcp_servers": {},
            "plugins": {},
        }

    result = _merge_config(result, requested)
    features = result.setdefault("features", {})
    assert isinstance(features, dict)
    for section, feature in (
        ("apps", "apps"),
        ("plugins", "plugins"),
        ("tool_suggest", "tool_suggest"),
    ):
        if requested.get(section):
            features[feature] = True
    if not features:
        result.pop("features")
    return result


def _merge_config(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> JsonObject:
    result = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_config(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _unique_names(
    names: Sequence[str], registry: Mapping[str, Any], capability: str
) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(names))
    missing = [name for name in result if name not in registry]
    if missing:
        raise KeyError(f"unknown {capability}(s): {', '.join(missing)}")
    return result
