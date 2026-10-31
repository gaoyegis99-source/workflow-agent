"""Stable, credential-free contracts for V7 general Temporal workflows.

This module deliberately contains only deterministic value objects.  It can be
shared by API, Studio, Temporal workers and tests without making any of them
the owner of execution state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_NAMES = frozenset(
    {"api_key", "authorization", "password", "private_key", "secret", "token"}
)


class WorkflowContractError(ValueError):
    """A generic workflow contract is invalid, unsafe, or not portable."""


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise WorkflowContractError("workflow values must be JSON serializable") from error


def json_copy(value: object) -> Any:
    return json.loads(canonical_json(value))


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contains_secret_name(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                normalized in _SECRET_NAMES
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
            ):
                return True
            if contains_secret_name(child):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_secret_name(child) for child in value)
    return False


def validate_schema_instance(
    schema: Mapping[str, JsonValue], value: Mapping[str, JsonValue], label: str
) -> None:
    """Validate an already JSON-safe plugin value without leaking it in errors."""

    try:
        validator = Draft202012Validator(dict(schema))
        violation = next(iter(validator.iter_errors(dict(value))), None)
    except SchemaError as error:
        raise WorkflowContractError("plugin schema is not a valid JSON Schema") from error
    if violation is not None:
        raise WorkflowContractError(f"{label} does not satisfy the declared plugin schema")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkflowContractError(f"{label} must be a portable identifier")
    return value


def _version(value: object, label: str) -> str:
    result = _identifier(value, label)
    if result.lower() == "latest":
        raise WorkflowContractError(f"{label} must be fixed, not latest")
    return result


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise WorkflowContractError(f"{label} must be a sha256 digest")
    return value


def _mapping(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise WorkflowContractError(f"{label} must be an object")
    copied = json_copy(dict(value))
    if not isinstance(copied, dict):
        raise WorkflowContractError(f"{label} must be an object")
    return copied


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowContractError(f"{label} must be a list")
    return value


class PluginKind(StrEnum):
    AGENT = "agent"
    HTTP = "http"
    MCP = "mcp"
    CONTAINER = "container"


class PluginExecutionMode(StrEnum):
    PROCESS = "process"
    CONTAINER = "container"
    REMOTE = "remote"


@dataclass(frozen=True)
class PluginReference:
    plugin_id: str
    version: str
    package_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _identifier(self.plugin_id, "plugin_id"))
        object.__setattr__(self, "version", _version(self.version, "plugin version"))
        object.__setattr__(
            self, "package_digest", _digest(self.package_digest, "plugin package_digest")
        )

    @classmethod
    def from_mapping(cls, value: object) -> PluginReference:
        raw = _mapping(value, "plugin")
        if set(raw) != {"plugin_id", "version", "package_digest"}:
            raise WorkflowContractError("plugin requires plugin_id, version, and package_digest")
        return cls(**raw)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "package_digest": self.package_digest,
        }


@dataclass(frozen=True)
class PluginManifest:
    """An audited plugin declaration; credentials are reference names only."""

    plugin: PluginReference
    kind: PluginKind
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    permissions: tuple[str, ...]
    execution_mode: PluginExecutionMode
    timeout_seconds: int
    max_attempts: int
    credential_ref: str | None = None

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, PluginKind) else PluginKind(self.kind)
            mode = (
                self.execution_mode
                if isinstance(self.execution_mode, PluginExecutionMode)
                else PluginExecutionMode(self.execution_mode)
            )
        except ValueError as error:
            raise WorkflowContractError("plugin kind or execution mode is unsupported") from error
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "execution_mode", mode)
        input_schema = _mapping(self.input_schema, "plugin input_schema")
        output_schema = _mapping(self.output_schema, "plugin output_schema")
        if contains_secret_name(input_schema) or contains_secret_name(output_schema):
            raise WorkflowContractError("plugin schemas must not define credential fields")
        if _contains_external_schema_ref(input_schema) or _contains_external_schema_ref(
            output_schema
        ):
            raise WorkflowContractError("plugin schemas may not fetch external references")
        _validate_plugin_schema(input_schema)
        _validate_plugin_schema(output_schema)
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "output_schema", output_schema)
        permissions = tuple(_identifier(item, "plugin permission") for item in self.permissions)
        if not permissions or len(set(permissions)) != len(permissions):
            raise WorkflowContractError("plugin permissions must be a non-empty unique list")
        object.__setattr__(self, "permissions", permissions)
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 86_400
        ):
            raise WorkflowContractError("plugin timeout_seconds must be between 1 and 86400")
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or not 1 <= self.max_attempts <= 10
        ):
            raise WorkflowContractError("plugin max_attempts must be between 1 and 10")
        if self.credential_ref is not None:
            object.__setattr__(
                self, "credential_ref", _identifier(self.credential_ref, "credential_ref")
            )

    @classmethod
    def from_mapping(cls, value: object) -> PluginManifest:
        raw = _mapping(value, "plugin manifest")
        required = {
            "plugin",
            "kind",
            "input_schema",
            "output_schema",
            "permissions",
            "execution_mode",
            "timeout_seconds",
            "max_attempts",
            "credential_ref",
        }
        if set(raw) != required or not isinstance(raw["permissions"], list):
            raise WorkflowContractError("plugin manifest has unknown or missing fields")
        return cls(
            plugin=PluginReference.from_mapping(raw["plugin"]),
            kind=PluginKind(raw["kind"]),
            input_schema=_mapping(raw["input_schema"], "plugin input_schema"),
            output_schema=_mapping(raw["output_schema"], "plugin output_schema"),
            permissions=tuple(raw["permissions"]),  # type: ignore[arg-type]
            execution_mode=PluginExecutionMode(raw["execution_mode"]),
            timeout_seconds=raw["timeout_seconds"],  # type: ignore[arg-type]
            max_attempts=raw["max_attempts"],  # type: ignore[arg-type]
            credential_ref=raw["credential_ref"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "plugin": self.plugin.to_mapping(),
            "kind": self.kind.value,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "permissions": list(self.permissions),
            "execution_mode": self.execution_mode.value,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "credential_ref": self.credential_ref,
        }

    @property
    def manifest_digest(self) -> str:
        return digest(self.to_mapping())


class NodeKind(StrEnum):
    AGENT = "agent"
    HTTP = "http"
    MCP = "mcp"
    APPROVAL = "approval"
    TIMER = "timer"
    CONTAINER = "container"
    SUBWORKFLOW = "subworkflow"
    CONDITION = "condition"
    PARALLEL = "parallel"
    LOOP = "loop"


_PLUGIN_NODE_KINDS = {
    NodeKind.AGENT: PluginKind.AGENT,
    NodeKind.HTTP: PluginKind.HTTP,
    NodeKind.MCP: PluginKind.MCP,
    NodeKind.CONTAINER: PluginKind.CONTAINER,
}


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    kind: NodeKind
    config: Mapping[str, JsonValue] = field(default_factory=dict)
    children: tuple[tuple[WorkflowNode, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "node_id"))
        try:
            kind = self.kind if isinstance(self.kind, NodeKind) else NodeKind(self.kind)
        except ValueError as error:
            raise WorkflowContractError("workflow node kind is unsupported") from error
        object.__setattr__(self, "kind", kind)
        config = _mapping(self.config, f"configuration for {self.node_id}")
        if contains_secret_name(config):
            raise WorkflowContractError("workflow configuration must use credential references")
        object.__setattr__(self, "config", config)
        children = tuple(tuple(branch) for branch in self.children)
        if any(not isinstance(item, WorkflowNode) for branch in children for item in branch):
            raise WorkflowContractError("workflow children must be workflow nodes")
        object.__setattr__(self, "children", children)
        self._validate_shape()

    def _validate_shape(self) -> None:
        if self.kind in _PLUGIN_NODE_KINDS:
            allowed = {"plugin", "input"}
            if self.kind is NodeKind.AGENT:
                allowed.add("max_turns")
            if self.children or set(self.config) - allowed:
                raise WorkflowContractError("plugin node has unsupported fields")
            PluginReference.from_mapping(self.config.get("plugin"))
            _mapping(self.config.get("input", {}), "plugin input")
            if "max_turns" in self.config:
                turns = self.config["max_turns"]
                if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 32:
                    raise WorkflowContractError("agent max_turns must be between 1 and 32")
        elif self.kind is NodeKind.APPROVAL:
            if (
                self.children
                or set(self.config) != {"prompt"}
                or not isinstance(self.config["prompt"], str)
            ):
                raise WorkflowContractError("approval node requires a prompt")
        elif self.kind is NodeKind.TIMER:
            seconds = self.config.get("seconds")
            if (
                self.children
                or set(self.config) != {"seconds"}
                or not isinstance(seconds, int)
                or isinstance(seconds, bool)
                or not 1 <= seconds <= 2_592_000
            ):
                raise WorkflowContractError("timer node requires seconds between 1 and 2592000")
        elif self.kind is NodeKind.SUBWORKFLOW:
            if self.children or set(self.config) != {"workflow_id", "version"}:
                raise WorkflowContractError("subworkflow requires workflow_id and version")
            _identifier(self.config["workflow_id"], "subworkflow workflow_id")
            _version(self.config["version"], "subworkflow version")
        elif self.kind is NodeKind.CONDITION:
            if set(self.config) != {"path", "equals"} or len(self.children) != 2:
                raise WorkflowContractError("condition requires path, equals, then, and otherwise")
            if not isinstance(self.config["path"], str) or not self.config["path"].startswith("/"):
                raise WorkflowContractError("condition path must be a JSON pointer")
        elif self.kind is NodeKind.PARALLEL:
            if self.config or len(self.children) < 2:
                raise WorkflowContractError("parallel requires at least two branches")
        elif self.kind is NodeKind.LOOP:
            maximum = self.config.get("max_iterations")
            if set(self.config) != {"items_path", "max_iterations"} or len(self.children) != 1:
                raise WorkflowContractError(
                    "loop requires items_path, max_iterations, and one body"
                )
            if not isinstance(self.config["items_path"], str) or not self.config[
                "items_path"
            ].startswith("/"):
                raise WorkflowContractError("loop items_path must be a JSON pointer")
            if (
                not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or not 1 <= maximum <= 1_000
            ):
                raise WorkflowContractError("loop max_iterations must be between 1 and 1000")

    @classmethod
    def from_mapping(cls, value: object) -> WorkflowNode:
        raw = _mapping(value, "workflow node")
        if not {"id", "kind"} <= set(raw):
            raise WorkflowContractError("workflow node requires id and kind")
        kind = NodeKind(raw["kind"])
        allowed = {"id", "kind", "config"}
        children: tuple[tuple[WorkflowNode, ...], ...] = ()
        if kind is NodeKind.CONDITION:
            allowed |= {"then", "otherwise"}
            children = (
                tuple(cls.from_mapping(item) for item in _sequence(raw.get("then"), "then")),
                tuple(
                    cls.from_mapping(item) for item in _sequence(raw.get("otherwise"), "otherwise")
                ),
            )
        elif kind is NodeKind.PARALLEL:
            allowed.add("branches")
            children = tuple(
                tuple(cls.from_mapping(item) for item in _sequence(branch, "parallel branch"))
                for branch in _sequence(raw.get("branches"), "branches")
            )
        elif kind is NodeKind.LOOP:
            allowed.add("body")
            children = (
                tuple(cls.from_mapping(item) for item in _sequence(raw.get("body"), "body")),
            )
        if set(raw) - allowed:
            raise WorkflowContractError("workflow node has unknown fields")
        return cls(
            node_id=_identifier(raw["id"], "node id"),
            kind=kind,
            config=_mapping(raw.get("config", {}), "node configuration"),
            children=children,
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "id": self.node_id,
            "kind": self.kind.value,
            "config": dict(self.config),
        }
        if self.kind is NodeKind.CONDITION:
            result["then"] = [item.to_mapping() for item in self.children[0]]
            result["otherwise"] = [item.to_mapping() for item in self.children[1]]
        elif self.kind is NodeKind.PARALLEL:
            result["branches"] = [
                [item.to_mapping() for item in branch] for branch in self.children
            ]
        elif self.kind is NodeKind.LOOP:
            result["body"] = [item.to_mapping() for item in self.children[0]]
        return result


@dataclass(frozen=True)
class GeneralWorkflowSpec:
    workflow_id: str
    version: str
    release_digest: str
    plugins: tuple[PluginManifest, ...]
    steps: tuple[WorkflowNode, ...]
    schema_version: str = "v7-general-workflow-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workflow_id", _identifier(self.workflow_id, "workflow_id"))
        object.__setattr__(self, "version", _version(self.version, "workflow version"))
        object.__setattr__(self, "release_digest", _digest(self.release_digest, "release_digest"))
        if self.schema_version != "v7-general-workflow-v1":
            raise WorkflowContractError("workflow schema_version is unsupported")
        if not self.steps:
            raise WorkflowContractError("workflow must contain at least one node")
        nodes = list(_walk(self.steps))
        if len(nodes) > 2_000:
            raise WorkflowContractError("workflow may contain at most 2000 nodes")
        if len({node.node_id for node in nodes}) != len(nodes):
            raise WorkflowContractError("workflow node ids must be unique across nested branches")
        if _depth(self.steps) > 24:
            raise WorkflowContractError("workflow nesting is limited to 24 levels")
        identities = [
            (item.plugin.plugin_id, item.plugin.version, item.plugin.package_digest)
            for item in self.plugins
        ]
        if len(identities) != len(set(identities)):
            raise WorkflowContractError("plugin manifests must have unique exact references")
        manifests = {
            identity: item for identity, item in zip(identities, self.plugins, strict=True)
        }
        for node in nodes:
            expected_kind = _PLUGIN_NODE_KINDS.get(node.kind)
            if expected_kind is None:
                continue
            reference = PluginReference.from_mapping(node.config["plugin"])
            manifest = manifests.get(
                (reference.plugin_id, reference.version, reference.package_digest)
            )
            if manifest is None or manifest.kind is not expected_kind:
                raise WorkflowContractError(
                    "workflow node references an unavailable or incompatible plugin"
                )

    @classmethod
    def from_mapping(cls, value: object) -> GeneralWorkflowSpec:
        raw = _mapping(value, "workflow")
        required = {
            "schema_version",
            "workflow_id",
            "version",
            "release_digest",
            "plugins",
            "steps",
        }
        if set(raw) != required:
            raise WorkflowContractError("workflow has unknown or missing fields")
        return cls(
            workflow_id=_identifier(raw["workflow_id"], "workflow_id"),
            version=_version(raw["version"], "workflow version"),
            release_digest=_digest(raw["release_digest"], "release_digest"),
            plugins=tuple(
                PluginManifest.from_mapping(item) for item in _sequence(raw["plugins"], "plugins")
            ),
            steps=tuple(
                WorkflowNode.from_mapping(item) for item in _sequence(raw["steps"], "steps")
            ),
            schema_version=str(raw["schema_version"]),
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "release_digest": self.release_digest,
            "plugins": [item.to_mapping() for item in self.plugins],
            "steps": [item.to_mapping() for item in self.steps],
        }

    @property
    def definition_digest(self) -> str:
        return digest(self.to_mapping())


class TriggerKind(StrEnum):
    MANUAL = "manual"
    API = "api"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"


@dataclass(frozen=True)
class GovernanceFence:
    """The governance revisions frozen at creation and rechecked at action time."""

    project_id: str
    task_id: str
    actor_id: str
    authorization_revision: int
    budget_revision: int
    release_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(self, "actor_id", _identifier(self.actor_id, "actor_id"))
        for field_name in ("authorization_revision", "budget_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise WorkflowContractError(f"{field_name} must be a positive integer")
        object.__setattr__(
            self, "release_digest", _digest(self.release_digest, "fence release_digest")
        )

    @classmethod
    def from_mapping(cls, value: object) -> GovernanceFence:
        raw = _mapping(value, "governance fence")
        required = {
            "project_id",
            "task_id",
            "actor_id",
            "authorization_revision",
            "budget_revision",
            "release_digest",
        }
        if set(raw) != required:
            raise WorkflowContractError("governance fence has unknown or missing fields")
        return cls(**raw)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "actor_id": self.actor_id,
            "authorization_revision": self.authorization_revision,
            "budget_revision": self.budget_revision,
            "release_digest": self.release_digest,
        }


@dataclass(frozen=True)
class WorkflowInvocation:
    run_id: str
    workflow: GeneralWorkflowSpec
    inputs: Mapping[str, JsonValue]
    fence: GovernanceFence
    trigger: TriggerKind = TriggerKind.MANUAL
    task_queue: str = "v7-general-workflows"
    subworkflows: tuple[GeneralWorkflowSpec, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        if not isinstance(self.workflow, GeneralWorkflowSpec):
            raise WorkflowContractError("workflow must be a GeneralWorkflowSpec")
        if not isinstance(self.fence, GovernanceFence):
            raise WorkflowContractError("fence must be a GovernanceFence")
        try:
            trigger = (
                self.trigger if isinstance(self.trigger, TriggerKind) else TriggerKind(self.trigger)
            )
        except ValueError as error:
            raise WorkflowContractError("trigger is unsupported") from error
        object.__setattr__(self, "trigger", trigger)
        inputs = _mapping(self.inputs, "workflow inputs")
        if contains_secret_name(inputs):
            raise WorkflowContractError("workflow inputs must use credential references")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "task_queue", _identifier(self.task_queue, "task_queue"))
        if self.workflow.release_digest != self.fence.release_digest:
            raise WorkflowContractError("workflow release and governance fence do not match")
        subworkflows = tuple(self.subworkflows)
        if any(not isinstance(item, GeneralWorkflowSpec) for item in subworkflows):
            raise WorkflowContractError("subworkflows must be workflow definitions")
        object.__setattr__(self, "subworkflows", subworkflows)
        definitions = (self.workflow, *subworkflows)
        catalog = {(item.workflow_id, item.version): item for item in definitions}
        if len(catalog) != len(definitions):
            raise WorkflowContractError("subworkflow catalog has duplicate versions")
        edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for identity, definition in catalog.items():
            references: set[tuple[str, str]] = set()
            for node in _walk(definition.steps):
                if node.kind is NodeKind.SUBWORKFLOW:
                    reference = (str(node.config["workflow_id"]), str(node.config["version"]))
                    if reference not in catalog:
                        raise WorkflowContractError(
                            "subworkflow is not included in the frozen invocation"
                        )
                    references.add(reference)
            edges[identity] = references
        _reject_subworkflow_cycles(edges)

    @classmethod
    def from_mapping(cls, value: object) -> WorkflowInvocation:
        raw = _mapping(value, "workflow invocation")
        required = {
            "run_id",
            "workflow",
            "inputs",
            "fence",
            "trigger",
            "task_queue",
            "subworkflows",
        }
        if set(raw) != required:
            raise WorkflowContractError("workflow invocation has unknown or missing fields")
        return cls(
            run_id=_identifier(raw["run_id"], "run_id"),
            workflow=GeneralWorkflowSpec.from_mapping(raw["workflow"]),
            inputs=_mapping(raw["inputs"], "workflow inputs"),
            fence=GovernanceFence.from_mapping(raw["fence"]),
            trigger=TriggerKind(raw["trigger"]),
            task_queue=_identifier(raw["task_queue"], "task_queue"),
            subworkflows=tuple(
                GeneralWorkflowSpec.from_mapping(item)
                for item in _sequence(raw["subworkflows"], "subworkflows")
            ),
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow.to_mapping(),
            "inputs": dict(self.inputs),
            "fence": self.fence.to_mapping(),
            "trigger": self.trigger.value,
            "task_queue": self.task_queue,
            "subworkflows": [item.to_mapping() for item in self.subworkflows],
        }


class OutcomeStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECONCILING = "RECONCILING"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ActionCommand:
    run_id: str
    node_key: str
    action_id: str
    idempotency_key: str
    workflow_digest: str
    plugin: PluginReference
    inputs: Mapping[str, JsonValue]
    fence: GovernanceFence
    agent_max_turns: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        if not isinstance(self.node_key, str) or not self.node_key or len(self.node_key) > 512:
            raise WorkflowContractError("node_key is invalid")
        action_id = _identifier(self.action_id, "action_id")
        idempotency_key = _identifier(self.idempotency_key, "idempotency_key")
        workflow_digest = _digest(self.workflow_digest, "workflow_digest")
        if not isinstance(self.plugin, PluginReference):
            raise WorkflowContractError("action plugin must be a PluginReference")
        if not isinstance(self.fence, GovernanceFence):
            raise WorkflowContractError("action fence must be a GovernanceFence")
        agent_max_turns = self.agent_max_turns
        if agent_max_turns is not None and (
            not isinstance(agent_max_turns, int)
            or isinstance(agent_max_turns, bool)
            or not 1 <= agent_max_turns <= 32
        ):
            raise WorkflowContractError("action agent_max_turns must be between 1 and 32")
        inputs = _mapping(self.inputs, "action inputs")
        if contains_secret_name(inputs):
            raise WorkflowContractError("action inputs must not contain credentials")
        object.__setattr__(self, "inputs", inputs)
        stable = _action_identity(
            run_id=self.run_id,
            node_key=self.node_key,
            workflow_digest=workflow_digest,
            plugin=self.plugin,
            inputs=inputs,
            release_digest=self.fence.release_digest,
            agent_max_turns=agent_max_turns,
        )
        if action_id != "act-" + stable or idempotency_key != "idem-" + stable:
            raise WorkflowContractError("action identity does not match its frozen command")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "workflow_digest", workflow_digest)
        object.__setattr__(self, "agent_max_turns", agent_max_turns)

    @classmethod
    def create(
        cls,
        invocation: WorkflowInvocation,
        node_key: str,
        plugin: PluginReference,
        inputs: Mapping[str, JsonValue],
        *,
        agent_max_turns: int | None = None,
    ) -> ActionCommand:
        workflow_digest = invocation.workflow.definition_digest
        stable = _action_identity(
            run_id=invocation.run_id,
            node_key=node_key,
            workflow_digest=workflow_digest,
            plugin=plugin,
            inputs=inputs,
            release_digest=invocation.workflow.release_digest,
            agent_max_turns=agent_max_turns,
        )
        return cls(
            run_id=invocation.run_id,
            node_key=node_key,
            action_id="act-" + stable[:48],
            idempotency_key="idem-" + stable[:48],
            workflow_digest=workflow_digest,
            plugin=plugin,
            inputs=inputs,
            fence=invocation.fence,
            agent_max_turns=agent_max_turns,
        )

    @classmethod
    def from_mapping(cls, value: object) -> ActionCommand:
        raw = _mapping(value, "action command")
        required = {
            "run_id",
            "node_key",
            "action_id",
            "idempotency_key",
            "workflow_digest",
            "plugin",
            "inputs",
            "fence",
        }
        fields = set(raw)
        if fields != required and fields != required | {"agent_max_turns"}:
            raise WorkflowContractError("action command has unknown or missing fields")
        return cls(
            run_id=_identifier(raw["run_id"], "run_id"),
            node_key=str(raw["node_key"]),
            action_id=_identifier(raw["action_id"], "action_id"),
            idempotency_key=_identifier(raw["idempotency_key"], "idempotency_key"),
            workflow_digest=_digest(raw["workflow_digest"], "workflow_digest"),
            plugin=PluginReference.from_mapping(raw["plugin"]),
            inputs=_mapping(raw["inputs"], "action inputs"),
            fence=GovernanceFence.from_mapping(raw["fence"]),
            agent_max_turns=raw.get("agent_max_turns"),  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "node_key": self.node_key,
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "workflow_digest": self.workflow_digest,
            "plugin": self.plugin.to_mapping(),
            "inputs": dict(self.inputs),
            "fence": self.fence.to_mapping(),
            "agent_max_turns": self.agent_max_turns,
        }


@dataclass(frozen=True)
class NodeOutcome:
    action_id: str
    status: OutcomeStatus
    output: Mapping[str, JsonValue] | None = None
    error_code: str | None = None
    external_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _identifier(self.action_id, "action_id"))
        try:
            status = (
                self.status
                if isinstance(self.status, OutcomeStatus)
                else OutcomeStatus(self.status)
            )
        except ValueError as error:
            raise WorkflowContractError("action outcome status is unsupported") from error
        object.__setattr__(self, "status", status)
        if self.output is not None:
            output = _mapping(self.output, "action output")
            if contains_secret_name(output):
                raise WorkflowContractError("action output must not contain credentials")
            object.__setattr__(self, "output", output)
        if status is OutcomeStatus.SUCCEEDED and self.output is None:
            raise WorkflowContractError("successful action outcome requires output")
        if status is not OutcomeStatus.SUCCEEDED and not self.error_code:
            raise WorkflowContractError("non-successful action outcome requires error_code")
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _identifier(self.error_code, "error_code"))
        if self.external_reference is not None:
            object.__setattr__(
                self,
                "external_reference",
                _identifier(self.external_reference, "external_reference"),
            )

    @classmethod
    def from_mapping(cls, value: object) -> NodeOutcome:
        raw = _mapping(value, "action outcome")
        required = {"action_id", "status", "output", "error_code", "external_reference"}
        if set(raw) != required:
            raise WorkflowContractError("action outcome has unknown or missing fields")
        return cls(
            action_id=_identifier(raw["action_id"], "action_id"),
            status=OutcomeStatus(raw["status"]),
            output=_mapping(raw["output"], "action output") if raw["output"] is not None else None,
            error_code=raw["error_code"] if isinstance(raw["error_code"], str) else None,
            external_reference=raw["external_reference"]
            if isinstance(raw["external_reference"], str)
            else None,
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "output": dict(self.output) if self.output is not None else None,
            "error_code": self.error_code,
            "external_reference": self.external_reference,
        }


def _walk(nodes: Sequence[WorkflowNode]):
    for node in nodes:
        yield node
        for branch in node.children:
            yield from _walk(branch)


def _depth(nodes: Sequence[WorkflowNode], current: int = 1) -> int:
    if not nodes:
        return current
    return max(
        [current, *(_depth(branch, current + 1) for node in nodes for branch in node.children)]
    )


def _reject_subworkflow_cycles(edges: Mapping[tuple[str, str], set[tuple[str, str]]]) -> None:
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(identity: tuple[str, str]) -> None:
        if identity in visiting:
            raise WorkflowContractError("subworkflow catalog contains a cycle")
        if identity in visited:
            return
        visiting.add(identity)
        for child in edges[identity]:
            visit(child)
        visiting.remove(identity)
        visited.add(identity)

    for identity in edges:
        visit(identity)


def _action_identity(
    *,
    run_id: str,
    node_key: str,
    workflow_digest: str,
    plugin: PluginReference,
    inputs: Mapping[str, JsonValue],
    release_digest: str,
    agent_max_turns: int | None = None,
) -> str:
    payload: dict[str, JsonValue] = {
        "run_id": run_id,
        "node_key": node_key,
        "workflow_digest": workflow_digest,
        "plugin": plugin.to_mapping(),
        "inputs": dict(inputs),
        "release_digest": release_digest,
    }
    # Preserve validation of pre-P9 commands that did not carry an Agent
    # turn bound, while binding the value whenever the Agent runtime uses one.
    if agent_max_turns is not None:
        payload["agent_max_turns"] = agent_max_turns
    return digest(payload)[7:55]


def _contains_external_schema_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "$ref" and (not isinstance(child, str) or not child.startswith("#")):
                return True
            if _contains_external_schema_ref(child):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_external_schema_ref(child) for child in value)
    return False


def _validate_plugin_schema(schema: Mapping[str, JsonValue]) -> None:
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as error:
        raise WorkflowContractError("plugin schema is not a valid JSON Schema") from error
