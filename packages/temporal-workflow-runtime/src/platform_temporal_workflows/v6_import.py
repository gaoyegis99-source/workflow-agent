"""Restricted, credential-free V6 bundle import for a V7 release target.

The importer intentionally handles only the declarative subset whose semantics
can be made explicit in a V7 ``GeneralWorkflowSpec``.  It never imports a V6
journal, SDK checkpoint, callable, package code, or credential value.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import (
    GeneralWorkflowSpec,
    JsonValue,
    NodeKind,
    PluginKind,
    PluginManifest,
    PluginReference,
    WorkflowContractError,
    WorkflowNode,
    contains_secret_name,
    digest,
    json_copy,
)

_BUNDLE_SCHEMA = "v6-light-bundle-v1"
_WORKFLOW_SCHEMA = "v6-light-workflow-v1"
_REQUIRED_ENTRIES = {
    "manifest.json",
    "workflow.json",
    "tools.json",
    "dependencies.json",
}
_FILES_PREFIX = "files/"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class V6ImportTarget:
    """The V7 release and target-local bindings available to an importer."""

    release_digest: str
    plugins: tuple[PluginManifest, ...]
    allowed_permissions: frozenset[str] = frozenset()
    credential_bindings: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if _DIGEST.fullmatch(self.release_digest) is None:
            raise WorkflowContractError("V7 import release_digest must be a sha256 digest")
        plugins = tuple(self.plugins)
        if any(not isinstance(item, PluginManifest) for item in plugins):
            raise WorkflowContractError("V7 import target plugins must be exact manifests")
        identities = {_plugin_identity(item.plugin) for item in plugins}
        if len(identities) != len(plugins):
            raise WorkflowContractError("V7 import target has duplicate plugin references")
        object.__setattr__(self, "plugins", plugins)
        for name in ("allowed_permissions", "credential_bindings"):
            values = frozenset(getattr(self, name))
            if any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in values
            ):
                raise WorkflowContractError(f"V7 import {name} must contain portable identifiers")
            object.__setattr__(self, name, values)

    @property
    def plugin_index(self) -> dict[tuple[str, str, str], PluginManifest]:
        return {_plugin_identity(item.plugin): item for item in self.plugins}


@dataclass(frozen=True)
class V6ImportResult:
    """A V7 definition plus target-bound files/dependencies for release intake."""

    workflow: GeneralWorkflowSpec
    source_workflow_digest: str
    dependencies: Mapping[str, JsonValue]
    required_files: Mapping[str, str]


@dataclass(frozen=True)
class _SourceTool:
    reference: PluginReference
    permissions: tuple[str, ...]
    credential_ref: str | None


def import_v6_bundle(source: str | Path, target: V6ImportTarget) -> V6ImportResult:
    """Validate and translate one supported V6 bundle without writing state.

    The caller binds ``required_files`` into a controlled V7 execution target;
    this function does not unpack files or install dependencies as a side
    effect.  V6 Agent steps and subworkflows deliberately fail because their
    driver/checkpoint and catalogue semantics are not portable to V7.
    """

    entries = _read_entries(Path(source))
    manifest, raw_workflow, raw_tools, dependencies, required_files = _validate_bundle(entries)
    del manifest
    tools = _source_tools(raw_tools)
    source_workflow_digest = digest(raw_workflow)
    used_plugins: dict[tuple[str, str, str], PluginManifest] = {}
    source_ids: set[str] = set()
    output_ids: set[str] = set()
    steps = _convert_steps(
        _steps(raw_workflow),
        target,
        tools,
        used_plugins,
        source_ids,
        output_ids,
    )
    workflow = GeneralWorkflowSpec(
        workflow_id=_identifier(raw_workflow.get("workflow_id"), "V6 workflow_id"),
        version=_identifier(raw_workflow.get("version"), "V6 workflow version"),
        release_digest=target.release_digest,
        plugins=tuple(used_plugins[key] for key in sorted(used_plugins)),
        steps=tuple(steps),
    )
    return V6ImportResult(
        workflow=workflow,
        source_workflow_digest=source_workflow_digest,
        dependencies=dependencies,
        required_files=required_files,
    )


def _read_entries(source: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len({info.filename for info in infos}) != len(infos):
                raise WorkflowContractError("V6 bundle contains duplicate archive members")
            total = 0
            entries: dict[str, bytes] = {}
            for info in infos:
                _safe_member(info.filename)
                if info.is_dir() or info.filename.endswith("/"):
                    raise WorkflowContractError("V6 bundle must not contain directory entries")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise WorkflowContractError("V6 bundle must not contain symlinks")
                total += info.file_size
                if total > _MAX_ARCHIVE_BYTES:
                    raise WorkflowContractError("V6 bundle exceeds the import size limit")
                entries[info.filename] = archive.read(info)
            return entries
    except WorkflowContractError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise WorkflowContractError("V6 bundle cannot be opened") from error


def _validate_bundle(
    entries: Mapping[str, bytes],
) -> tuple[
    Mapping[str, JsonValue],
    Mapping[str, JsonValue],
    Sequence[Mapping[str, JsonValue]],
    Mapping[str, JsonValue],
    Mapping[str, str],
]:
    if not _REQUIRED_ENTRIES <= set(entries):
        raise WorkflowContractError("V6 bundle is missing required metadata")
    if any(
        name not in _REQUIRED_ENTRIES and not name.startswith(_FILES_PREFIX) for name in entries
    ):
        raise WorkflowContractError("V6 bundle contains an unsupported archive member")
    manifest = _json_object(entries["manifest.json"], "V6 manifest")
    workflow = _json_object(entries["workflow.json"], "V6 workflow")
    tools_value = _json_value(entries["tools.json"], "V6 tools")
    dependencies = _json_object(entries["dependencies.json"], "V6 dependencies")
    if not isinstance(tools_value, list) or any(
        not isinstance(item, Mapping) for item in tools_value
    ):
        raise WorkflowContractError("V6 tools must be a list of objects")
    tools = tuple(_json_mapping(item, "V6 tool") for item in tools_value)
    if (
        contains_secret_name(workflow)
        or contains_secret_name(tools)
        or contains_secret_name(dependencies)
    ):
        raise WorkflowContractError("V6 bundle metadata must not contain credentials")
    required_manifest = {
        "schema_version",
        "workflow_digest",
        "tools_digest",
        "dependencies_digest",
        "files",
    }
    if set(manifest) != required_manifest or manifest.get("schema_version") != _BUNDLE_SCHEMA:
        raise WorkflowContractError("V6 bundle schema is unsupported")
    if manifest.get("workflow_digest") != digest(workflow):
        raise WorkflowContractError("V6 workflow digest does not match its bundle")
    if manifest.get("tools_digest") != digest(list(tools)):
        raise WorkflowContractError("V6 tool digest does not match its bundle")
    if manifest.get("dependencies_digest") != digest(dependencies):
        raise WorkflowContractError("V6 dependency digest does not match its bundle")
    _validate_v6_workflow_header(workflow)
    required_files = _validate_files(entries, manifest.get("files"))
    return manifest, workflow, tools, dependencies, required_files


def _validate_v6_workflow_header(workflow: Mapping[str, JsonValue]) -> None:
    if set(workflow) != {"schema_version", "workflow_id", "version", "steps"}:
        raise WorkflowContractError("V6 workflow has unknown or missing fields")
    if workflow.get("schema_version") != _WORKFLOW_SCHEMA:
        raise WorkflowContractError("V6 workflow schema is unsupported")
    _identifier(workflow.get("workflow_id"), "V6 workflow_id")
    version = _identifier(workflow.get("version"), "V6 workflow version")
    if version.lower() == "latest":
        raise WorkflowContractError("V6 workflow version must be fixed")
    _steps(workflow)


def _validate_files(entries: Mapping[str, bytes], raw: object) -> Mapping[str, str]:
    if not isinstance(raw, Mapping):
        raise WorkflowContractError("V6 file manifest must be an object")
    expected = {_FILES_PREFIX + str(name) for name in raw}
    actual = {name for name in entries if name.startswith(_FILES_PREFIX)}
    if expected != actual:
        raise WorkflowContractError("V6 file manifest does not match archive files")
    files: dict[str, str] = {}
    for name, details in raw.items():
        if not isinstance(name, str) or not isinstance(details, Mapping):
            raise WorkflowContractError("V6 file manifest is invalid")
        if set(details) != {"sha256", "bytes"}:
            raise WorkflowContractError("V6 file manifest is invalid")
        _safe_member(_FILES_PREFIX + name)
        sha256 = details.get("sha256")
        size = details.get("bytes")
        content = entries[_FILES_PREFIX + name]
        actual_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if sha256 != actual_digest or type(size) is not int or size != len(content):
            raise WorkflowContractError("V6 file digest does not match archive content")
        files[name] = actual_digest
    return files


def _source_tools(
    raw_tools: Sequence[Mapping[str, JsonValue]],
) -> dict[tuple[str, str, str], _SourceTool]:
    result: dict[tuple[str, str, str], _SourceTool] = {}
    for raw in raw_tools:
        reference = PluginReference.from_mapping(
            {
                "plugin_id": raw.get("tool_id"),
                "version": raw.get("version"),
                "package_digest": raw.get("package_digest"),
            }
        )
        permissions_value = raw.get("permissions", [])
        if not isinstance(permissions_value, list) or any(
            not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
            for item in permissions_value
        ):
            raise WorkflowContractError("V6 tool permissions must be portable identifiers")
        if len(set(permissions_value)) != len(permissions_value):
            raise WorkflowContractError("V6 tool permissions must be unique")
        credential_ref = raw.get("credential_ref")
        if credential_ref is not None and (
            not isinstance(credential_ref, str) or _IDENTIFIER.fullmatch(credential_ref) is None
        ):
            raise WorkflowContractError("V6 tool credential_ref is invalid")
        source = _SourceTool(reference, tuple(permissions_value), credential_ref)
        identity = _plugin_identity(reference)
        if identity in result:
            raise WorkflowContractError("V6 bundle has duplicate tool references")
        result[identity] = source
    return result


def _convert_steps(
    raw_steps: Sequence[object],
    target: V6ImportTarget,
    source_tools: Mapping[tuple[str, str, str], _SourceTool],
    used_plugins: dict[tuple[str, str, str], PluginManifest],
    source_ids: set[str],
    output_ids: set[str],
) -> list[WorkflowNode]:
    result: list[WorkflowNode] = []
    for raw_value in raw_steps:
        raw = _json_mapping(raw_value, "V6 workflow step")
        source_id = _identifier(raw.get("id"), "V6 step id")
        if source_id in source_ids:
            raise WorkflowContractError("V6 workflow step IDs must be unique")
        source_ids.add(source_id)
        kind = raw.get("kind")
        config = _step_config(raw)
        if kind == "tool":
            _require_fields(raw, {"id", "kind", "config"}, "V6 tool step")
            source_tool = _source_tool(config, source_tools)
            plugin = _target_plugin(source_tool, target)
            if plugin.kind is PluginKind.AGENT:
                raise WorkflowContractError("V6 tool step cannot target a V7 Agent plugin")
            node_kind = {
                PluginKind.HTTP: NodeKind.HTTP,
                PluginKind.MCP: NodeKind.MCP,
                PluginKind.CONTAINER: NodeKind.CONTAINER,
            }.get(plugin.kind)
            if node_kind is None:  # exhaustive guard if a new plugin kind is added
                raise WorkflowContractError("V6 tool maps to an unsupported V7 plugin kind")
            approval = config.get("approval_required", True)
            if not isinstance(approval, bool):
                raise WorkflowContractError("V6 tool approval_required must be boolean")
            if approval:
                approval_id = _generated_approval_id(source_id, output_ids)
                result.append(
                    WorkflowNode(
                        approval_id,
                        NodeKind.APPROVAL,
                        {"prompt": f"Approve imported V6 tool {source_id}"},
                    )
                )
            _add_output_id(source_id, output_ids)
            result.append(
                WorkflowNode(
                    source_id,
                    node_kind,
                    {
                        "plugin": plugin.plugin.to_mapping(),
                        "input": _arguments(config),
                    },
                )
            )
            used_plugins[_plugin_identity(plugin.plugin)] = plugin
            continue
        if kind == "approval":
            _require_fields(raw, {"id", "kind", "config"}, "V6 approval step")
            if set(config) != {"prompt"} or not isinstance(config.get("prompt"), str):
                raise WorkflowContractError("V6 approval step requires a prompt")
            _add_output_id(source_id, output_ids)
            result.append(WorkflowNode(source_id, NodeKind.APPROVAL, dict(config)))
            continue
        if kind == "condition":
            _require_fields(raw, {"id", "kind", "config", "then", "otherwise"}, "V6 condition")
            if set(config) != {"path", "equals"} or not isinstance(config.get("path"), str):
                raise WorkflowContractError("V6 condition is invalid")
            then_steps = _convert_steps(
                _sequence(raw.get("then"), "V6 condition then"),
                target,
                source_tools,
                used_plugins,
                source_ids,
                output_ids,
            )
            otherwise_steps = _convert_steps(
                _sequence(raw.get("otherwise"), "V6 condition otherwise"),
                target,
                source_tools,
                used_plugins,
                source_ids,
                output_ids,
            )
            _add_output_id(source_id, output_ids)
            result.append(
                WorkflowNode(
                    source_id,
                    NodeKind.CONDITION,
                    dict(config),
                    (tuple(then_steps), tuple(otherwise_steps)),
                )
            )
            continue
        if kind == "parallel":
            _require_fields(raw, {"id", "kind", "config", "branches"}, "V6 parallel")
            if config:
                raise WorkflowContractError("V6 parallel step must not have configuration")
            branches = _sequence(raw.get("branches"), "V6 parallel branches")
            if len(branches) < 2:
                raise WorkflowContractError("V6 parallel requires at least two branches")
            converted = tuple(
                tuple(
                    _convert_steps(
                        _sequence(branch, "V6 parallel branch"),
                        target,
                        source_tools,
                        used_plugins,
                        source_ids,
                        output_ids,
                    )
                )
                for branch in branches
            )
            _add_output_id(source_id, output_ids)
            result.append(WorkflowNode(source_id, NodeKind.PARALLEL, {}, converted))
            continue
        if kind == "agent":
            raise WorkflowContractError(
                "V6 Agent driver state is not portable; publish a V7 Agent plugin instead"
            )
        if kind == "subworkflow":
            raise WorkflowContractError(
                "V6 subworkflows require an explicit V7 published-workflow mapping"
            )
        raise WorkflowContractError("V6 workflow contains an unsupported step kind")
    return result


def _step_config(raw: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    config = raw.get("config", {})
    if not isinstance(config, Mapping) or contains_secret_name(config):
        raise WorkflowContractError("V6 step configuration is invalid or sensitive")
    return _json_mapping(config, "V6 step configuration")


def _source_tool(
    config: Mapping[str, JsonValue], source_tools: Mapping[tuple[str, str, str], _SourceTool]
) -> _SourceTool:
    if set(config) - {"tool", "arguments", "approval_required"}:
        raise WorkflowContractError("V6 tool step has unsupported fields")
    tool_value = config.get("tool")
    if not isinstance(tool_value, Mapping):
        raise WorkflowContractError("V6 tool step requires an exact tool reference")
    reference = PluginReference.from_mapping(
        {
            "plugin_id": tool_value.get("tool_id"),
            "version": tool_value.get("version"),
            "package_digest": tool_value.get("package_digest"),
        }
    )
    source = source_tools.get(_plugin_identity(reference))
    if source is None:
        raise WorkflowContractError("V6 workflow references a tool missing from its bundle")
    _arguments(config)
    return source


def _arguments(config: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    arguments = config.get("arguments", {})
    if not isinstance(arguments, Mapping) or contains_secret_name(arguments):
        raise WorkflowContractError("V6 tool arguments are invalid or sensitive")
    return _json_mapping(arguments, "V6 tool arguments")


def _target_plugin(source: _SourceTool, target: V6ImportTarget) -> PluginManifest:
    plugin = target.plugin_index.get(_plugin_identity(source.reference))
    if plugin is None:
        raise WorkflowContractError("V7 target does not trust the required exact V6 tool")
    if not set(source.permissions) <= set(plugin.permissions):
        raise WorkflowContractError("V6 tool permissions exceed its reviewed V7 plugin manifest")
    if not set(source.permissions) <= target.allowed_permissions:
        raise WorkflowContractError("V7 target does not grant a required V6 tool permission")
    if source.credential_ref is not None and plugin.credential_ref != source.credential_ref:
        raise WorkflowContractError("V6 credential reference does not match the V7 plugin")
    credential_ref = source.credential_ref or plugin.credential_ref
    if credential_ref is not None and credential_ref not in target.credential_bindings:
        raise WorkflowContractError("V7 target has not bound a required credential reference")
    return plugin


def _steps(workflow: Mapping[str, JsonValue]) -> Sequence[object]:
    return _sequence(workflow.get("steps"), "V6 workflow steps")


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowContractError(f"{label} must be a list")
    return value


def _require_fields(raw: Mapping[str, JsonValue], required: set[str], label: str) -> None:
    if set(raw) != required:
        raise WorkflowContractError(f"{label} has unknown or missing fields")


def _generated_approval_id(source_id: str, output_ids: set[str]) -> str:
    candidate = "v6-approval-" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:32]
    _add_output_id(candidate, output_ids)
    return candidate


def _add_output_id(value: str, output_ids: set[str]) -> None:
    if value in output_ids:
        raise WorkflowContractError("V6 import would create duplicate V7 node IDs")
    output_ids.add(value)


def _plugin_identity(reference: PluginReference) -> tuple[str, str, str]:
    return reference.plugin_id, reference.version, reference.package_digest


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowContractError(f"{label} must be a portable identifier")
    return value


def _safe_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or value.is_absolute()
        or ".." in value.parts
    ):
        raise WorkflowContractError("V6 bundle has an unsafe archive path")
    return value


def _json_value(content: bytes, label: str) -> JsonValue:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowContractError(f"{label} is not valid UTF-8 JSON") from error
    copied = json_copy(value)
    return copied


def _json_object(content: bytes, label: str) -> Mapping[str, JsonValue]:
    return _json_mapping(_json_value(content, label), label)


def _json_mapping(value: object, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise WorkflowContractError(f"{label} must be an object")
    copied = json_copy(dict(value))
    if not isinstance(copied, dict):
        raise WorkflowContractError(f"{label} must be an object")
    return copied
