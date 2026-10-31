from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from platform_temporal_workflows import (
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    V6ImportTarget,
    WorkflowContractError,
    import_v6_bundle,
)
from platform_temporal_workflows.models import digest

RELEASE = "sha256:" + "a" * 64
TOOL = PluginReference("http.imported", "1", "sha256:" + "b" * 64)


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin=TOOL,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("remote.write",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=30,
        max_attempts=2,
        credential_ref="service_binding",
    )


def _tool_step(step_id: str, *, approval_required: bool) -> dict[str, object]:
    return {
        "id": step_id,
        "kind": "tool",
        "config": {
            "tool": {
                "tool_id": TOOL.plugin_id,
                "version": TOOL.version,
                "package_digest": TOOL.package_digest,
            },
            "arguments": {"value": {"$ref": "/input"}},
            "approval_required": approval_required,
        },
    }


def _workflow() -> dict[str, object]:
    return {
        "schema_version": "v6-light-workflow-v1",
        "workflow_id": "v6-imported",
        "version": "1",
        "steps": [
            _tool_step("write", approval_required=True),
            {
                "id": "route",
                "kind": "condition",
                "config": {"path": "/input", "equals": "yes"},
                "then": [_tool_step("then-write", approval_required=False)],
                "otherwise": [_tool_step("otherwise-write", approval_required=False)],
            },
            {
                "id": "fanout",
                "kind": "parallel",
                "config": {},
                "branches": [
                    [_tool_step("left", approval_required=False)],
                    [_tool_step("right", approval_required=False)],
                ],
            },
        ],
    }


def _write_bundle(
    path: Path,
    *,
    workflow: dict[str, object] | None = None,
    tools: list[dict[str, object]] | None = None,
    file_metadata_extra: dict[str, object] | None = None,
) -> None:
    if workflow is None:
        workflow = _workflow()
    if tools is None:
        tools = [
            {
                "tool_id": TOOL.plugin_id,
                "version": TOOL.version,
                "package_digest": TOOL.package_digest,
                "permissions": ["remote.write"],
                "credential_ref": "service_binding",
            }
        ]
    dependencies: dict[str, object] = {"tool-package": "1"}
    file_content = b"input-for-target-binding\n"
    files = {
        "input.txt": {
            "sha256": "sha256:" + hashlib.sha256(file_content).hexdigest(),
            "bytes": len(file_content),
        }
    }
    if file_metadata_extra is not None:
        files["input.txt"].update(file_metadata_extra)
    metadata = {
        "schema_version": "v6-light-bundle-v1",
        "workflow_digest": digest(workflow),
        "tools_digest": digest(tools),
        "dependencies_digest": digest(dependencies),
        "files": files,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(metadata, sort_keys=True))
        archive.writestr("workflow.json", json.dumps(workflow, sort_keys=True))
        archive.writestr("tools.json", json.dumps(tools, sort_keys=True))
        archive.writestr("dependencies.json", json.dumps(dependencies, sort_keys=True))
        archive.writestr("files/input.txt", file_content)


def _target(*, bind_credential: bool = True) -> V6ImportTarget:
    return V6ImportTarget(
        release_digest=RELEASE,
        plugins=(_manifest(),),
        allowed_permissions=frozenset({"remote.write"}),
        credential_bindings=frozenset({"service_binding"}) if bind_credential else frozenset(),
    )


def test_v6_bundle_converts_the_supported_declarative_subset(tmp_path: Path) -> None:
    bundle = tmp_path / "workflow.v6bundle"
    source_workflow = _workflow()
    _write_bundle(bundle, workflow=source_workflow)

    result = import_v6_bundle(bundle, _target())

    assert result.source_workflow_digest == digest(source_workflow)
    assert result.required_files["input.txt"].startswith("sha256:")
    assert result.workflow.release_digest == RELEASE
    assert [node.kind for node in result.workflow.steps] == [
        NodeKind.APPROVAL,
        NodeKind.HTTP,
        NodeKind.CONDITION,
        NodeKind.PARALLEL,
    ]
    condition = result.workflow.steps[2]
    assert condition.children[0][0].kind is NodeKind.HTTP
    assert condition.children[1][0].kind is NodeKind.HTTP
    parallel = result.workflow.steps[3]
    assert [branch[0].kind for branch in parallel.children] == [NodeKind.HTTP, NodeKind.HTTP]


def test_v6_import_requires_rebound_credentials_and_rejects_agent_drivers(tmp_path: Path) -> None:
    bundle = tmp_path / "workflow.v6bundle"
    _write_bundle(bundle)
    with pytest.raises(WorkflowContractError, match="credential"):
        import_v6_bundle(bundle, _target(bind_credential=False))

    agent_bundle = tmp_path / "agent.v6bundle"
    agent_workflow = {
        "schema_version": "v6-light-workflow-v1",
        "workflow_id": "v6-agent",
        "version": "1",
        "steps": [
            {
                "id": "plan",
                "kind": "agent",
                "config": {"driver": "openai", "input": {}, "max_turns": 2, "tools": []},
            }
        ],
    }
    _write_bundle(agent_bundle, workflow=agent_workflow, tools=[])
    with pytest.raises(WorkflowContractError, match="Agent driver state"):
        import_v6_bundle(agent_bundle, _target())


def test_v6_import_rejects_unrecognized_file_metadata(tmp_path: Path) -> None:
    bundle = tmp_path / "workflow.v6bundle"
    _write_bundle(bundle, file_metadata_extra={"untrusted": "value"})

    with pytest.raises(WorkflowContractError, match="file manifest"):
        import_v6_bundle(bundle, _target())
