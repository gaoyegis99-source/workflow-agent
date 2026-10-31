"""Workflow-node, trace, and file routes for the Execution API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse
from platform_contracts.service import ServiceFault, digest, ensure

from platform_execution.api_context import ExecutionApiContext
from platform_execution.api_contracts import Id, plan_fingerprint
from platform_execution.trace import record as trace

LOG = logging.getLogger("uvicorn.error")
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}


def register_workflow_routes(app: FastAPI, context: ExecutionApiContext) -> None:
    """Register bridge routes without giving route handlers app-facade access."""

    repository = context.repository

    def normalize_engine_binding(body: dict) -> dict:
        epoch = body["epoch"]
        binding = body.get("engine_binding")
        legacy_present = "engine_job_id" in body
        legacy = body.get("engine_job_id")
        if binding is None:
            binding = {
                "kind": "toil",
                "id": legacy if legacy_present else None,
                "epoch": epoch,
            }
        identifier = binding.get("id") if isinstance(binding, dict) else None
        valid_identifier = identifier is None or (
            isinstance(identifier, str)
            and 0 < len(identifier) <= 512
            and all(ord(character) >= 32 and ord(character) != 127 for character in identifier)
        )
        ensure(
            isinstance(binding, dict)
            and set(binding) == {"kind", "id", "epoch"}
            and binding.get("kind") == "toil"
            and binding.get("epoch") == epoch
            and valid_identifier
            and (
                not legacy_present
                or legacy is None
                or isinstance(legacy, str)
                and legacy == identifier
            ),
            "INVALID_BINDING",
            "Invalid workflow engine binding",
            422,
        )
        return {"kind": "toil", "id": identifier, "epoch": epoch}

    def node_payload(root: dict, node: str, body: dict) -> dict:
        path = body.get("call_path")
        indices = body.get("scatter_indices")
        ensure(
            root["state"] == "RUNNING"
            and root["runtime"].get("phase") == "ENGINE_RUNNING"
            and type(body.get("epoch")) is int
            and body["epoch"] == root["runtime"].get("engine_epoch"),
            "REVISION_CONFLICT",
            "Workflow engine generation is stale",
            409,
        )
        expected = (
            "node_"
            + hashlib.sha256(
                json.dumps([root["execution_id"], path, indices], separators=(",", ":")).encode()
            ).hexdigest()
        )
        ensure(expected == node, "INVALID_BINDING", "Workflow node identity changed", 422)
        engine_binding = normalize_engine_binding(body)
        invocation = root["payload"]["compiled"]["invocations"].get("/".join(path or []))
        ensure(invocation is not None, "INVALID_BINDING", "Unknown workflow call path", 422)
        plan = root["payload"].get("execution_plan")
        if plan is not None:
            planned = next(
                (
                    item
                    for item in plan["nodes"]
                    if item["node_id"] == node
                    and item["call_path"] == path
                    and item["scatter_indices"] == indices
                ),
                None,
            )
            if planned is None:
                planned = next(
                    (
                        item
                        for item in plan["deferred_templates"]
                        if item["call_path"] == path and item["index_depth"] == len(indices or [])
                    ),
                    None,
                )
            target = invocation["target"]
            ensure(
                planned is not None
                and planned.get("condition") is not False
                and planned.get("operator_id") == target.get("operator_id")
                and planned.get("implementation_digest") == plan_fingerprint(target),
                "INVALID_BINDING",
                "Workflow invocation is outside the frozen execution plan",
                422,
            )

        root_bindings: list[dict] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                if value.get("class") == "File" and "binding" in value:
                    root_bindings.append(value["binding"])
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(root["payload"].get("inputs", {}))
        file_inputs = {
            name: value
            for name, value in body.get("inputs", {}).items()
            if isinstance(value, dict) and value.get("class") == "File" and "digest" in value
        }
        ensure(file_inputs, "INVALID_BINDING", "Workflow operator requires a File input", 422)
        node_bindings: dict[str, dict] = {}
        derived = False
        for port, frozen_file in file_inputs.items():
            digest_value = frozen_file["digest"]
            matches = [
                item
                for item in root_bindings
                if item.get("representation", {}).get("digest") == digest_value
            ]
            binding = matches[0] if len(matches) == 1 else None
            if binding is None:
                child_matches = []
                for child in repository.children(root["execution_id"]):
                    if child["state"] != "SUCCEEDED":
                        continue
                    for item in child["runtime"].get("files", []):
                        if item.get("digest") == digest_value:
                            child_matches.append((child, item))
                ensure(
                    len(child_matches) == 1,
                    "INVALID_BINDING",
                    "Workflow File input has no unique verified source",
                    422,
                )
                child, item = child_matches[0]
                source = child["runtime"]["manifest"]
                descriptor = source.get("asset_descriptor", {})
                source_manifest = descriptor or {
                    "fields": source.get("fields", []),
                    "row_count": source.get("row_count"),
                }
                binding = {
                    "selection": {
                        "derived_from": {
                            "execution_id": child["execution_id"],
                            "node_id": child["action_id"],
                            "representation_id": item["representation_id"],
                        }
                    },
                    "representation": {
                        key: item[key] for key in ("representation_id", "format", "digest", "size")
                    },
                    "manifest": source_manifest,
                    "semantics": source.get("semantics"),
                    "private_source": {
                        "execution_id": child["execution_id"],
                        "node_id": child["action_id"],
                        "filename": item["filename"],
                        "digest": item["digest"],
                    },
                }
                derived = True
            if port == "table":
                tabular = binding.get("manifest", {}).get("tabular", binding.get("manifest", {}))
                ensure(
                    binding.get("representation", {}).get("format") == "parquet"
                    and type(tabular.get("row_count")) is int
                    and 0 <= tabular["row_count"] <= 100_000
                    and isinstance(tabular.get("fields"), list)
                    and 0 < len(tabular["fields"]) <= 128
                    and all(
                        item.get("type") in {"string", "int64", "double", "bool"}
                        for item in tabular["fields"]
                    ),
                    "INVALID_BINDING",
                    "Workflow table input lacks a checked tabular descriptor",
                    422,
                )
            node_bindings[port] = binding
        durable_request = {
            key: value
            for key, value in body.items()
            if key not in {"trace", "engine_job_id", "engine_binding"}
        } | {
            "engine_job_id": engine_binding["id"],
            "engine_binding": engine_binding,
        }
        result = {
            "project_id": root["project_id"],
            "task_id": root["task_id"],
            "expected_spec_revision": root["payload"]["expected_spec_revision"],
            "target": invocation["target"],
            "kind": "OPERATOR",
            "bindings": node_bindings,
            "parameters": body.get("inputs", {}).get("parameters", {}),
            "parent_action_id": root["action_id"],
            "parent_execution_id": root["execution_id"],
            "node_request": durable_request | {"uses_derived": derived},
        }
        frozen_operator = root["payload"].get("_operator_snapshots", {}).get("/".join(path or []))
        if frozen_operator is not None:
            result["_operator_snapshot"] = frozen_operator
        if invocation["target"].get("execution_binding", {}).get("kind") == "REMOTE":
            frozen = root["payload"].get("_remote_bindings", {}).get("/".join(path or []))
            ensure(
                frozen is not None,
                "INVALID_BINDING",
                "Remote workflow binding is missing",
                422,
            )
            result["_remote_binding"] = frozen
        if set(node_bindings) == {"observations"}:
            result["binding"] = node_bindings["observations"]
        return result

    def node_view(row: dict, *, changed: bool = True) -> dict:
        return {
            "state": row["state"],
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "revision": row["revision"],
            "files": row["runtime"].get("files", []),
            "changed": changed,
            "retry_after_ms": 0 if changed else 1000,
        }

    def record_terminal_observation(
        root: dict, row: dict, instance: str, source_monotonic_ns: int
    ) -> None:
        if row["state"] not in _TERMINAL_STATES:
            return
        trace(
            repository,
            workflow_execution_id=root["execution_id"],
            node_action_id=row["action_id"],
            engine_epoch=root["runtime"].get("engine_epoch"),
            source="EXECUTION_API",
            event_type="NODE_TERMINAL_OBSERVED",
            discriminator=[instance, row["revision"]],
            source_monotonic_ns=source_monotonic_ns,
            attributes={
                "bridge_instance_id": instance,
                "revision": row["revision"],
                "state": row["state"],
            },
        )

    @app.post("/internal/workflows/{execution}/nodes/{node}", status_code=202)
    def submit_node(execution: Id, node: Id, body: dict, request: Request):
        received_monotonic_ns = time.monotonic_ns()
        root = repository.get(execution=execution)
        context.authenticate_workflow(request, root)
        bridge = body.get("trace")
        if bridge is not None:
            ensure(
                isinstance(bridge, dict)
                and set(bridge) == {"bridge_instance_id", "started_monotonic_ns"}
                and re.fullmatch(r"[0-9a-f]{32}", bridge.get("bridge_instance_id", ""))
                and type(bridge.get("started_monotonic_ns")) is int
                and bridge["started_monotonic_ns"] >= 0,
                "INVALID_BINDING",
                "Invalid workflow trace context",
                422,
            )
        payload = node_payload(root, node, body)
        created = False
        try:
            row = repository.get(action=node)
            previous = row["payload"]["node_request"]

            def stable(value: dict) -> dict:
                return {key: value[key] for key in ("call_path", "scatter_indices", "inputs")}

            ensure(
                row["parent_execution_id"] == execution and stable(previous) == stable(body),
                "IDEMPOTENCY_CONFLICT",
                "Workflow invocation changed",
            )
        except ServiceFault as problem:
            if problem.status != 404:
                raise
            remote = payload["target"].get("execution_binding", {}).get("kind") == "REMOTE"
            initial_runtime = None
            if remote:
                from platform_execution.implementations import remote_implementation

                remote_profile = remote_implementation(payload)
                initial_runtime = {
                    "implementation_profile": remote_profile,
                    "remote": {
                        "service_binding_id": payload["_remote_binding"]["service_binding_id"],
                        "binding_revision": payload["_remote_binding"]["revision"],
                        "state": "STAGING_INPUT",
                    },
                }
            row = repository.create(
                node,
                payload,
                parent_execution_id=execution,
                invocation_key=node,
                initial_runtime=initial_runtime,
                remote_binding=payload.get("_remote_binding"),
                remote_request_digest="sha256:" + digest(payload) if remote else None,
            )
            created = True
        if bridge is not None:
            trace(
                repository,
                workflow_execution_id=root["execution_id"],
                node_action_id=node,
                engine_epoch=body["epoch"],
                source="BRIDGE",
                event_type="BRIDGE_STARTED",
                discriminator=bridge["bridge_instance_id"],
                source_monotonic_ns=bridge["started_monotonic_ns"],
                attributes={"bridge_instance_id": bridge["bridge_instance_id"]},
            )
        binding = payload["node_request"]["engine_binding"]
        trace(
            repository,
            workflow_execution_id=root["execution_id"],
            node_action_id=node,
            engine_epoch=body["epoch"],
            source="BRIDGE",
            event_type="ENGINE_BINDING_OBSERVED",
            discriminator=[
                bridge["bridge_instance_id"] if bridge else "legacy",
                binding["kind"],
                binding["id"],
                binding["epoch"],
            ],
            source_monotonic_ns=received_monotonic_ns,
            attributes={"kind": binding["kind"], "binding_id": binding["id"]},
        )
        if created:
            trace(
                repository,
                workflow_execution_id=root["execution_id"],
                node_action_id=node,
                engine_epoch=body["epoch"],
                source="EXECUTION_API",
                event_type="NODE_ACCEPTED",
                discriminator="accepted",
                source_monotonic_ns=received_monotonic_ns,
                attributes={"created": True},
            )
        record_terminal_observation(
            root,
            row,
            bridge["bridge_instance_id"] if bridge else "legacy",
            time.monotonic_ns(),
        )
        LOG.info("workflow_node_post execution_id=%s node_id=%s db_reads=2", execution, node)
        return node_view(row)

    @app.get("/internal/workflows/{execution}/nodes/{node}")
    def query_node(
        execution: Id,
        node: Id,
        request: Request,
        after_revision: int = Query(ge=0),
        wait_seconds: float = Query(default=5.0, ge=0.0, le=5.0),
        bridge_instance_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
    ):
        """Wait for a newer durable node revision without retaining a DB transaction."""

        root = repository.get(execution=execution)
        context.authenticate_workflow(request, root)
        deadline = time.monotonic() + wait_seconds
        reads = 0
        while True:
            row = repository.get(action=node)
            reads += 1
            ensure(
                row["parent_execution_id"] == execution,
                "RESOURCE_NOT_FOUND",
                "Workflow node not found",
                404,
            )
            terminal = row["state"] in _TERMINAL_STATES
            changed = row["revision"] > after_revision
            if changed or terminal or time.monotonic() >= deadline:
                instance = bridge_instance_id or "legacy"
                record_terminal_observation(root, row, instance, time.monotonic_ns())
                LOG.info(
                    "workflow_node_get execution_id=%s node_id=%s revision=%s "
                    "changed=%s db_reads=%s",
                    execution,
                    node,
                    row["revision"],
                    changed,
                    reads + 1,
                )
                return node_view(row, changed=changed)
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    @app.post("/internal/workflows/{execution}/nodes/{node}/trace", status_code=204)
    def bridge_trace(execution: Id, node: Id, body: dict, request: Request):
        root = repository.get(execution=execution)
        context.authenticate_workflow(request, root)
        row = repository.get(action=node)
        ensure(
            row["parent_execution_id"] == execution
            and body.get("event_type") == "BRIDGE_RETURNED"
            and re.fullmatch(r"[0-9a-f]{32}", body.get("bridge_instance_id", ""))
            and type(body.get("source_monotonic_ns")) is int
            and body["source_monotonic_ns"] >= 0
            and type(body.get("revision")) is int
            and body["revision"] == row["revision"]
            and row["state"] == "SUCCEEDED",
            "INVALID_BINDING",
            "Invalid workflow trace fact",
            422,
        )
        trace(
            repository,
            workflow_execution_id=root["execution_id"],
            node_action_id=node,
            engine_epoch=root["runtime"].get("engine_epoch"),
            source="BRIDGE",
            event_type="BRIDGE_RETURNED",
            discriminator=[body["bridge_instance_id"], body["revision"]],
            source_monotonic_ns=body["source_monotonic_ns"],
            attributes={
                "bridge_instance_id": body["bridge_instance_id"],
                "revision": body["revision"],
            },
        )

    @app.get("/internal/workflows/{execution}/nodes/{node}/files/{representation}")
    def node_file(execution: Id, node: Id, representation: Id, request: Request):
        root = repository.get(execution=execution)
        context.authenticate_workflow(request, root)
        row = repository.get(action=node)
        ensure(
            row["parent_execution_id"] == execution and row["state"] == "SUCCEEDED",
            "PRECONDITION_UNRESOLVED",
            "Node output is not available",
        )
        item = next(
            (
                value
                for value in row["runtime"].get("files", [])
                if value["representation_id"] == representation
            ),
            None,
        )
        ensure(item is not None, "RESOURCE_NOT_FOUND", "Node output not found", 404)
        output_root = (
            context.composition.runtime.work_root / row["execution_id"] / "output"
        ).resolve()
        path = (output_root / item["filename"]).resolve()
        ensure(
            path.is_relative_to(output_root) and path.is_file(),
            "DEPENDENCY_UNAVAILABLE",
            "Node output unavailable",
            503,
        )
        return FileResponse(path, media_type="application/octet-stream")


__all__ = ("register_workflow_routes",)
