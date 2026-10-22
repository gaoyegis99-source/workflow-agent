"""Frozen P6 tool catalogues.

P6.3 deployments may keep the read-only catalogue. P6.4 deployments opt in
to the action catalogue, whose write tools only select existing Platform use
cases; they do not accept commands, images, URLs, or arbitrary parameters.
"""

from __future__ import annotations

import json
from pathlib import Path

from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import require, valid_identifier

_READ_TOOLS = (
    ("task.read_current", ()),
    ("task.read_requirements", ()),
    ("plan.read_current", ()),
    ("data.list_assets", ()),
    ("data.read_asset_manifest", ("asset_id", "version_id")),
    ("data.read_semantic_revision", ("asset_id", "version_id", "revision")),
    ("knowledge.search", ("query",)),
    ("knowledge.read_evidence", ("document_id", "version_id", "unit_id")),
    ("capability.list_operators", ()),
    ("capability.list_workflows", ()),
    ("capability.list_checkers", ()),
    ("execution.read", ("execution_id",)),
    ("execution.read_steps", ("execution_id",)),
    ("research.read", ()),
    ("acceptance.read", ()),
)

_ACTION_TOOLS = (
    {
        "tool_id": "execution.submit_operator",
        "version": "1.0.0",
        "required": ["operator_id"],
        "optional": [],
        "value_shapes": {"operator_id": "IDENTIFIER"},
        "observation_type": "COMMAND_RECEIPT",
    },
    {
        "tool_id": "deliverable.register",
        "required": ["execution_ids"],
        "optional": [],
        "value_shapes": {"execution_ids": "IDENTIFIER_LIST"},
        "observation_type": "DELIVERABLE_RECEIPT",
    },
    {
        "tool_id": "deliverable.register_exact",
        "required": ["execution_id", "role_id", "output_port"],
        "optional": [],
        "value_shapes": {
            "execution_id": "IDENTIFIER",
            "role_id": "IDENTIFIER",
            "output_port": "IDENTIFIER",
        },
        "observation_type": "DELIVERABLE_RECEIPT",
    },
    {
        "tool_id": "acceptance.request",
        "required": ["deliverable_id"],
        "optional": ["retry_of", "analysis_registration_id"],
        "value_shapes": {
            "deliverable_id": "IDENTIFIER",
            "retry_of": "IDENTIFIER",
            "analysis_registration_id": "IDENTIFIER",
        },
        "observation_type": "ACCEPTANCE_RECEIPT",
    },
)

_P7_ACTION_TOOLS = (
    {
        "tool_id": "execution.submit_operator",
        "version": "2.0.0",
        "required": ["operator_id", "version_id"],
        "optional": [],
        "value_shapes": {"operator_id": "IDENTIFIER", "version_id": "VERSION"},
        "observation_type": "COMMAND_RECEIPT",
    },
    *_ACTION_TOOLS[1:],
)

_SCIENCE_TOOLS = (
    {
        "tool_id": "execution.submit_workflow",
        "required": ["package_id"],
        "optional": [],
        "value_shapes": {"package_id": "IDENTIFIER"},
        "observation_type": "COMMAND_RECEIPT",
    },
    {
        "tool_id": "research.create_analysis",
        "required": [],
        "optional": [],
        "value_shapes": {},
        "observation_type": "ANALYSIS_REVISION",
    },
    {
        "tool_id": "research.register_analysis",
        "required": ["revision_id"],
        "optional": [],
        "value_shapes": {"revision_id": "IDENTIFIER"},
        "observation_type": "ANALYSIS_REGISTRATION",
    },
    {
        "tool_id": "research.create_claim",
        "required": ["registration_id"],
        "optional": [],
        "value_shapes": {"registration_id": "IDENTIFIER"},
        "observation_type": "CLAIM_REVISION",
    },
)


def _read_spec(tool_id, required):
    optional = {
        "data.list_assets": ("after", "limit"),
        "knowledge.search": ("limit",),
        "capability.list_workflows": ("after", "limit"),
        "execution.read_steps": ("after", "limit"),
    }.get(tool_id, ())
    value_shapes = {
        "data.list_assets": {"after": "CURSOR", "limit": "LIMIT_100"},
        "knowledge.search": {"query": "SEARCH_QUERY", "limit": "LIMIT_20"},
        "capability.list_workflows": {"after": "CURSOR", "limit": "LIMIT_100"},
    }.get(tool_id, {})
    arguments = {
        "required": list(required),
        "optional": list(optional),
        "additional_properties": False,
    }
    if value_shapes:
        arguments["value_shapes"] = value_shapes
    return {
        "tool_id": tool_id,
        "version": "1.0.0",
        "operation_class": "READ_ONLY",
        "idempotency": "STABLE_TOOL_REQUEST",
        "required_scope": "TASK_PROJECT_EXACT",
        "observation_type": (
            "EVIDENCE_RECEIPT" if tool_id == "knowledge.read_evidence" else "READ_OBSERVATION"
        ),
        "arguments": arguments,
        "errors": [
            "INVALID_ARGUMENTS",
            "STALE_FRAME",
            "DENIED_SCOPE",
            "NOT_FOUND",
            "TEMPORARILY_UNAVAILABLE",
        ],
    }


def _action_spec(value):
    return {
        "tool_id": value["tool_id"],
        "version": value.get("version", "1.0.0"),
        "operation_class": "CONTROLLED_WRITE",
        "idempotency": "STABLE_TOOL_REQUEST",
        "required_scope": "TASK_PROJECT_EXACT_EDITOR",
        "observation_type": value["observation_type"],
        "arguments": {
            "required": value["required"],
            "optional": value["optional"],
            "additional_properties": False,
            "value_shapes": value["value_shapes"],
        },
        "errors": [
            "INVALID_ARGUMENTS",
            "STALE_FRAME",
            "DENIED_SCOPE",
            "NOT_FOUND",
            "BUDGET_BLOCKED",
            "TEMPORARILY_UNAVAILABLE",
        ],
    }


def build_tool_catalog(stage: str = "readonly") -> dict:
    require(
        stage in {"readonly", "actions", "science", "p7-actions", "p7-science"},
        "INVALID_BINDING",
        "Unknown tool stage",
        422,
    )
    specs = [_read_spec(tool_id, required) for tool_id, required in _READ_TOOLS]
    if stage in {"actions", "science", "p7-actions", "p7-science"}:
        selected = _P7_ACTION_TOOLS if stage.startswith("p7-") else _ACTION_TOOLS
        specs.extend(_action_spec(value) for value in selected)
    if stage in {"science", "p7-science"}:
        specs.extend(_action_spec(value) for value in _SCIENCE_TOOLS)
    tools = []
    for value in specs:
        value = dict(value)
        value["tool_digest"] = canonical_digest(value)
        tools.append(value)
    value = {
        "schema_version": {
            "readonly": 1,
            "actions": 2,
            "science": 3,
            "p7-actions": 4,
            "p7-science": 5,
        }[stage],
        "catalog_id": {
            "readonly": "p6-read-tools-v1",
            "actions": "p6-action-tools-v1",
            "science": "p6-science-tools-v1",
            "p7-actions": "p7-action-tools-v2",
            "p7-science": "p7-science-tools-v2",
        }[stage],
        "tools": tools,
    }
    value["catalog_digest"] = canonical_digest(value)
    return value


def validate_tool_catalog(value: dict) -> dict:
    expected = {
        candidate["catalog_id"]: candidate
        for candidate in (
            build_tool_catalog("readonly"),
            build_tool_catalog("actions"),
            build_tool_catalog("science"),
            build_tool_catalog("p7-actions"),
            build_tool_catalog("p7-science"),
        )
    }
    require(
        value == expected.get(value.get("catalog_id")),
        "INVALID_BINDING",
        "Tool catalogue differs from source",
    )
    return value


def load_tool_catalog(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Tool catalogue cannot be read") from error
    require(isinstance(value, dict), "INVALID_BINDING", "Invalid tool catalogue", 422)
    return validate_tool_catalog(value)


def write_tool_catalog(path: str | Path, stage: str = "actions") -> dict:
    value = build_tool_catalog(stage)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return value


def tool_spec(catalog: dict, tool_id: str, version: str) -> dict:
    validate_tool_catalog(catalog)
    match = next(
        (
            item
            for item in catalog["tools"]
            if item["tool_id"] == tool_id and item["version"] == version
        ),
        None,
    )
    require(match is not None, "UNSUPPORTED_TARGET", "Tool is not in the catalogue", 422)
    return match


def validate_tool_arguments(spec: dict, arguments: object) -> dict:
    require(
        isinstance(arguments, dict), "INVALID_ARGUMENTS", "Tool arguments must be an object", 422
    )
    required = set(spec["arguments"]["required"])
    optional = set(spec["arguments"]["optional"])
    require(
        required <= set(arguments) <= required | optional,
        "INVALID_ARGUMENTS",
        "Tool arguments do not match the frozen schema",
        422,
    )
    for key, value in arguments.items():
        shape = spec["arguments"].get("value_shapes", {}).get(key, "SCALAR")
        if shape == "IDENTIFIER":
            valid = valid_identifier(value)
        elif shape == "IDENTIFIER_LIST":
            valid = (
                isinstance(value, list)
                and 1 <= len(value) <= 8
                and len(set(value)) == len(value)
                and all(valid_identifier(item) for item in value)
            )
        elif shape == "VERSION":
            valid = (
                isinstance(value, str)
                and 1 <= len(value) <= 128
                and value[0].isalnum()
                and all(character.isalnum() or character in "_.-" for character in value)
                and value.lower() != "latest"
            )
        elif shape == "CURSOR":
            valid = isinstance(value, str) and 1 <= len(value) <= 512
        elif shape == "LIMIT_20":
            valid = type(value) is int and 1 <= value <= 20
        elif shape == "LIMIT_100":
            valid = type(value) is int and 1 <= value <= 100
        elif shape == "SEARCH_QUERY":
            valid = (
                isinstance(value, str)
                and 1 <= len(value) <= 200
                and bool(value.strip())
                and all(ord(character) >= 32 for character in value)
            )
        else:
            valid = (
                isinstance(value, str)
                and 0 < len(value) <= 1000
                or type(value) is int
                and 1 <= value <= 1000
            )
        require(valid, "INVALID_ARGUMENTS", f"Invalid {key} argument", 422)
    return dict(arguments)
