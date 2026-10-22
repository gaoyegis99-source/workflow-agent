"""Safe parsing for inert Capability Studio JSON/YAML source imports."""

from __future__ import annotations

import json
import math
from typing import Any

import yaml
from platform_core.application.errors import RuleViolation


class _UniqueSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not supported")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError("YAML anchors are not supported")
        tag = getattr(event, "tag", None)
        if tag is not None and not tag.startswith("tag:yaml.org,2002:"):
            raise yaml.YAMLError("Custom YAML tags are not supported")
        return super().compose_node(parent, index)


def _mapping(loader: _UniqueSafeLoader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    value = {}
    for key, item in pairs:
        if not isinstance(key, str):
            raise yaml.YAMLError("Workflow mapping keys must be text")
        if key in value:
            raise yaml.YAMLError(f"Duplicate mapping key: {key}")
        value[key] = item
    return value


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping,
)


def _json_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"Duplicate mapping key: {key}")
        result[key] = value
    return result


def _validate_editable_json(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("Workflow source is too deeply nested")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ValueError("Integer is outside the editor's exact range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite numbers are not JSON data")
        return
    if isinstance(value, list):
        for item in value:
            _validate_editable_json(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Workflow mapping keys must be text")
        for item in value.values():
            _validate_editable_json(item, depth + 1)
        return
    raise ValueError("YAML value is not lossless JSON data")


def parse_source(content: str, source_format: str) -> dict[str, Any]:
    try:
        if source_format == "JSON":
            value = json.loads(content, object_pairs_hook=_json_pairs)
        elif source_format == "YAML":
            value = yaml.load(content, Loader=_UniqueSafeLoader)
        else:
            raise ValueError("Unsupported source format")
        _validate_editable_json(value)
    except (json.JSONDecodeError, ValueError, yaml.YAMLError) as problem:
        raise RuleViolation(
            "MALFORMED_REQUEST",
            f"Workflow source import could not be parsed: {problem}",
            422,
            "SAME_KEY",
        ) from problem
    if not isinstance(value, dict):
        raise RuleViolation(
            "MALFORMED_REQUEST",
            "Workflow source import must contain one object",
            422,
            "SAME_KEY",
        )
    return value
