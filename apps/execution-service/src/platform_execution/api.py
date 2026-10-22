"""Internal Execution API; durable submission precedes all container work."""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from platform_contracts.engine_runtime import SELECTED_PROFILE_ID, profile
from platform_contracts.release import production_mode, production_release
from platform_contracts.service import (
    ReleaseIdentityMiddleware,
    ServiceFault,
    authenticate,
    digest,
    ensure,
)
from sqlalchemy.exc import SQLAlchemyError

from platform_execution.adapters.repository import Repository
from platform_execution.database import Database
from platform_execution.engine_package import validate as validate_engine_package
from platform_execution.trace import record as trace

Id = Annotated[str, Path(pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])"))]
LOG = logging.getLogger("uvicorn.error")


def plan_fingerprint(value):
    return "sha256:" + digest(value)


def validate_execution_plan(action, body):
    """Defend the immutable Execution payload; legacy workflows may omit a plan."""
    plan = body.get("execution_plan")
    if plan is None:
        return
    ensure(
        isinstance(plan, dict)
        and set(plan)
        == {
            "schema_version",
            "package_digest",
            "input_digest",
            "nodes",
            "deferred_templates",
            "plan_digest",
        }
        and plan.get("schema_version") == 1
        and plan.get("package_digest") == body.get("compiled", {}).get("manifest_digest")
        and plan.get("input_digest") == plan_fingerprint(body.get("inputs", {}))
        and plan.get("plan_digest")
        == plan_fingerprint({key: value for key, value in plan.items() if key != "plan_digest"})
        and isinstance(plan.get("nodes"), list)
        and isinstance(plan.get("deferred_templates"), list)
        and len(plan["nodes"]) <= 1000
        and len(plan["deferred_templates"]) <= 1000,
        "INVALID_BINDING",
        "Invalid immutable workflow execution plan",
        422,
    )
    execution = Repository.identity(action)
    invocations = body["compiled"]["invocations"]
    node_ids = set()
    executable = 0
    for node in plan["nodes"]:
        ensure(
            isinstance(node, dict)
            and set(node)
            == {
                "node_id",
                "call_path",
                "scatter_indices",
                "dependencies",
                "operator_id",
                "implementation_digest",
                "input_binding_digest",
                "condition",
                "output_ports",
            }
            and isinstance(node.get("call_path"), list)
            and all(isinstance(part, str) and part for part in node["call_path"])
            and isinstance(node.get("scatter_indices"), list)
            and all(type(index) is int and index >= 0 for index in node["scatter_indices"])
            and type(node.get("condition")) in {type(None), bool}
            and isinstance(node.get("dependencies"), list)
            and all(isinstance(item, str) for item in node["dependencies"])
            and isinstance(node.get("output_ports"), list)
            and all(isinstance(item, str) for item in node["output_ports"]),
            "INVALID_BINDING",
            "Invalid planned workflow node",
            422,
        )
        invocation = invocations.get("/".join(node["call_path"]))
        target = invocation.get("target") if invocation else None
        ensure(
            invocation is not None
            and node["node_id"]
            == "node_"
            + hashlib.sha256(
                json.dumps(
                    [execution, node["call_path"], node["scatter_indices"]],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            and node["node_id"] not in node_ids
            and node["operator_id"] == target.get("operator_id")
            and node["implementation_digest"] == plan_fingerprint(target)
            and node["output_ports"] == sorted(invocation["outputs"]),
            "INVALID_BINDING",
            "Planned workflow node does not match the frozen package",
            422,
        )
        node_ids.add(node["node_id"])
        executable += node["condition"] is not False
    ensure(
        executable <= body.get("max_node_executions", 0)
        and all(set(node["dependencies"]) <= node_ids for node in plan["nodes"]),
        "INVALID_BINDING",
        "Workflow plan dependencies or node limit are invalid",
        422,
    )
    template_ids = set()
    for template in plan["deferred_templates"]:
        ensure(
            isinstance(template, dict)
            and set(template)
            == {
                "template_id",
                "call_path",
                "index_depth",
                "dependencies",
                "operator_id",
                "implementation_digest",
                "input_binding_digest",
                "condition",
                "output_ports",
            }
            and isinstance(template.get("call_path"), list)
            and all(isinstance(part, str) and part for part in template["call_path"])
            and type(template.get("index_depth")) is int
            and 0 <= template["index_depth"] <= 32
            and template.get("condition") is None
            and isinstance(template.get("dependencies"), list)
            and all(isinstance(item, str) for item in template["dependencies"])
            and set(template["dependencies"]) <= node_ids
            and isinstance(template.get("output_ports"), list)
            and all(isinstance(item, str) for item in template["output_ports"]),
            "INVALID_BINDING",
            "Invalid deferred workflow template",
            422,
        )
        invocation = invocations.get("/".join(template["call_path"]))
        target = invocation.get("target") if invocation else None
        ensure(
            invocation is not None
            and isinstance(template.get("template_id"), str)
            and template["template_id"].startswith("template_")
            and template["template_id"] not in template_ids
            and template["operator_id"] == target.get("operator_id")
            and template["implementation_digest"] == plan_fingerprint(target)
            and template["output_ports"] == sorted(invocation["outputs"]),
            "INVALID_BINDING",
            "Deferred workflow template does not match the frozen package",
            422,
        )
        template_ids.add(template["template_id"])


def create_app(database=None, token=None, workflow_profile_id=SELECTED_PROFILE_ID):
    owned = database is None
    database = database or Database(os.environ["V7_EXECUTION_DATABASE_URL"])
    token = token or os.environ["V7_PLATFORM_EXECUTION_TOKEN"]
    workflow_profile = profile(workflow_profile_id)
    repo = Repository(database)
    operator_catalog_path = os.environ.get("V7_OPERATOR_CATALOG")
    if production_mode() and not operator_catalog_path:
        raise ValueError("Production Execution requires V7_OPERATOR_CATALOG")
    if operator_catalog_path:
        from platform_execution.operator_catalog import load

        operator_catalog = load(operator_catalog_path)
    else:
        operator_catalog = None
    checker_catalog_path = os.environ.get("V7_CHECKER_CATALOG")
    checker_catalog = None
    if checker_catalog_path:
        from platform_execution.checker_catalog import load as load_checker_catalog

        checker_catalog = load_checker_catalog(
            checker_catalog_path,
            os.environ.get("V7_CHECKER_IMAGE"),
            os.environ.get("V7_GDAL_CHECKER_IMAGE"),
        )
    if production_mode() and checker_catalog is None:
        raise ValueError("Production Execution requires V7_CHECKER_CATALOG")
    release = (
        production_release(
            operator_path=operator_catalog_path,
            operator_catalog=operator_catalog,
            catalogs={"checker_catalog": (checker_catalog_path, checker_catalog)},
            images={
                "operator": os.environ.get("V7_OPERATOR_IMAGE"),
                "qgis": os.environ.get("V7_SPATIAL_QGIS_IMAGE_ID"),
                "spatial_native": os.environ.get("V7_SPATIAL_NATIVE_IMAGE_ID"),
                "checker": os.environ.get("V7_CHECKER_IMAGE"),
                "gdal_checker": os.environ.get("V7_GDAL_CHECKER_IMAGE"),
                "tabular": os.environ.get("V7_TABULAR_OPERATOR_IMAGE"),
            },
            version=version("groundwater-execution-service"),
        )
        if production_mode()
        else None
    )

    @asynccontextmanager
    async def lifespan(app):
        database.check_schema()
        yield
        if owned:
            database.close()

    def auth(request: Request):
        authenticate(request.headers.get("authorization"), {"platform": token}, {"platform"})

    app = FastAPI(
        title="V7 Execution", version=version("groundwater-execution-service"), lifespan=lifespan
    )
    app.add_middleware(ReleaseIdentityMiddleware)
    app.state.operator_catalog = operator_catalog
    app.state.checker_catalog = checker_catalog
    app.state.release_digest = release["release_digest"] if release else None

    @app.exception_handler(ServiceFault)
    async def fault(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, exc):
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        logging.error(
            "Execution database operation failed category=%s sqlstate=%s constraint=%s detail=%s",
            type(exc).__name__,
            getattr(original, "sqlstate", None),
            getattr(diagnostics, "constraint_name", None),
            str(exc).replace("\n", " ")[:500],
        )
        return JSONResponse(
            {"code": "DEPENDENCY_UNAVAILABLE", "message": "Execution unavailable"}, status_code=503
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "execution"}

    @app.get("/readyz")
    def ready():
        database.check_schema()
        operator_catalog_path = os.environ.get("V7_OPERATOR_CATALOG")
        ensure(
            operator_catalog_path is None or app.state.operator_catalog is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Operator Catalog is not loaded",
            503,
        )
        return {
            "status": "ready",
            "operator_catalog_digest": (
                app.state.operator_catalog["catalog_digest"]
                if app.state.operator_catalog is not None
                else None
            ),
            "release_digest": app.state.release_digest,
        }

    @app.post("/internal/actions/{action}", dependencies=[Depends(auth)], status_code=202)
    def submit(action: Id, body: dict):
        ensure(
            "engine_runtime_profile" not in body and "_engine_runtime_profile" not in body,
            "INVALID_BINDING",
            "Workflow engine runtime profile is service controlled",
            422,
        )
        target = body.get("target", {})
        kind = body.get("kind", "OPERATOR")
        if kind == "WORKFLOW":
            compiled = body.get("compiled", {})
            ensure(
                target.get("kind") == "WORKFLOW"
                and compiled.get("profile") == "v7-cwl-p4-v1"
                and target.get("package_ref", {}).get("manifest_digest")
                == compiled.get("manifest_digest")
                and 0 < len(compiled.get("invocations", {})) <= 1000,
                "UNSUPPORTED_TARGET",
                "Only frozen workflow packages are accepted",
                422,
            )
            validate_execution_plan(action, body)
            validate_engine_package(body)
            frozen_operators = body.get("_operator_snapshots")
            if frozen_operators is not None:
                from platform_execution.operator_catalog import exact_snapshot

                ensure(
                    set(frozen_operators) == set(compiled["invocations"]),
                    "INVALID_BINDING",
                    "Workflow Operator Catalog snapshot is incomplete",
                    422,
                )
                if app.state.operator_catalog is not None:
                    try:
                        for frozen in frozen_operators.values():
                            exact_snapshot(app.state.operator_catalog, frozen)
                    except ValueError as error:
                        ensure(False, "INVALID_BINDING", str(error), 422)
        else:
            remote = target.get("execution_binding", {}).get("kind") == "REMOTE"
            if remote:
                from platform_execution.implementations import remote_implementation

                try:
                    profile = remote_implementation(body)
                except (KeyError, TypeError, ValueError):
                    ensure(False, "UNSUPPORTED_TARGET", "Remote binding is not registered", 422)
                if body.get("_operator_snapshot") is not None and app.state.operator_catalog:
                    from platform_execution.operator_catalog import exact_snapshot

                    try:
                        exact_snapshot(app.state.operator_catalog, body["_operator_snapshot"])
                    except ValueError as error:
                        ensure(False, "INVALID_BINDING", str(error), 422)
            else:
                from platform_execution.implementations import implementation

                try:
                    identity, _ = implementation(body)
                except (KeyError, TypeError, ValueError):
                    ensure(
                        False,
                        "UNSUPPORTED_TARGET",
                        "Only registered implementations are accepted",
                        422,
                    )
                if kind == "OPERATOR" and body.get("_operator_snapshot") is not None:
                    from platform_execution.operator_catalog import exact_snapshot

                    if app.state.operator_catalog is not None:
                        try:
                            exact_snapshot(app.state.operator_catalog, body["_operator_snapshot"])
                        except ValueError as error:
                            ensure(False, "INVALID_BINDING", str(error), 422)
                if target.get("operator_id") == "open-science-check":
                    run = body.get("checker_run", {})
                    plan = body.get("coverage_plan", {})
                    planned = next(
                        (
                            value
                            for value in plan.get("checker_runs", [])
                            if value.get("run_key") == run.get("run_key")
                        ),
                        None,
                    )
                    ensure(
                        plan.get("schema_version") == 3
                        and run.get("implementation_id") == identity
                        and run.get("capability_id") == identity
                        and run.get("catalog_digest") == plan.get("checker_catalog")
                        and planned is not None
                        and all(run.get(key) == planned.get(key) for key in planned),
                        "INVALID_BINDING",
                        "Checker run differs from the frozen capability plan",
                        422,
                    )
        initial_runtime = (
            {"_engine_runtime_profile": workflow_profile} if kind == "WORKFLOW" else None
        )
        if kind != "WORKFLOW" and target.get("execution_binding", {}).get("kind") == "REMOTE":
            from platform_contracts.service import digest

            initial_runtime = {
                "implementation_profile": profile,
                "remote": {
                    "service_binding_id": body["_remote_binding"]["service_binding_id"],
                    "binding_revision": body["_remote_binding"]["revision"],
                    "state": "STAGING_INPUT",
                },
            }
        row = repo.create(
            action,
            body,
            initial_runtime=initial_runtime,
            remote_binding=(
                body["_remote_binding"]
                if target.get("execution_binding", {}).get("kind") == "REMOTE"
                else None
            ),
            remote_request_digest=(
                "sha256:" + digest(body)
                if target.get("execution_binding", {}).get("kind") == "REMOTE"
                else None
            ),
        )
        return repo.observation(row)

    @app.get("/internal/actions/{action}", dependencies=[Depends(auth)])
    def query(action: Id):
        return repo.observation(repo.get(action=action))

    @app.post("/internal/actions/{action}/cancel", dependencies=[Depends(auth)])
    def cancel(action: Id):
        return repo.observation(repo.cancel(action))

    @app.get("/internal/projects/{project}/executions/{execution}", dependencies=[Depends(auth)])
    def execution(project: Id, execution: Id):
        return repo.view(repo.get(execution=execution, project=project))

    def workflow_auth(request, root):
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        expected = root["runtime"].get("bridge_token_digest", "")
        ensure(
            len(supplied) >= 32
            and len(expected) == 64
            and hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected),
            "AUTHENTICATION_REQUIRED",
            "Workflow engine credential required",
            401,
        )

    def normalize_engine_binding(body):
        epoch = body["epoch"]
        binding = body.get("engine_binding")
        legacy_present = "engine_job_id" in body
        legacy = body.get("engine_job_id")
        if binding is None:
            binding = {"kind": "toil", "id": legacy if legacy_present else None, "epoch": epoch}
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

    def node_payload(root, node, body):
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

        root_bindings = []

        def collect(value):
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
        node_bindings, derived = {}, False
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
                for child in repo.children(root["execution_id"]):
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
            ensure(frozen is not None, "INVALID_BINDING", "Remote workflow binding is missing", 422)
            result["_remote_binding"] = frozen
        if set(node_bindings) == {"observations"}:
            result["binding"] = node_bindings["observations"]
        return result

    def node_view(row, *, changed=True):
        return {
            "state": row["state"],
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "revision": row["revision"],
            "files": row["runtime"].get("files", []),
            "changed": changed,
            "retry_after_ms": 0 if changed else 1000,
        }

    def record_terminal_observation(root, row, instance, source_monotonic_ns):
        if row["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}:
            return
        trace(
            repo,
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
        root = repo.get(execution=execution)
        workflow_auth(request, root)
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
            row = repo.get(action=node)
            previous = row["payload"]["node_request"]

            def stable(value):
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
                from platform_contracts.service import digest

                from platform_execution.implementations import remote_implementation

                profile = remote_implementation(payload)
                initial_runtime = {
                    "implementation_profile": profile,
                    "remote": {
                        "service_binding_id": payload["_remote_binding"]["service_binding_id"],
                        "binding_revision": payload["_remote_binding"]["revision"],
                        "state": "STAGING_INPUT",
                    },
                }
            row = repo.create(
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
                repo,
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
            repo,
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
                repo,
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
        LOG.info(
            "workflow_node_post execution_id=%s node_id=%s db_reads=2",
            execution,
            node,
        )
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
        root = repo.get(execution=execution)
        workflow_auth(request, root)
        deadline = time.monotonic() + wait_seconds
        reads = 0
        while True:
            row = repo.get(action=node)
            reads += 1
            ensure(
                row["parent_execution_id"] == execution,
                "RESOURCE_NOT_FOUND",
                "Workflow node not found",
                404,
            )
            terminal = row["state"] in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "REJECTED",
                "SEALED",
            }
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
        root = repo.get(execution=execution)
        workflow_auth(request, root)
        row = repo.get(action=node)
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
            repo,
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
        root = repo.get(execution=execution)
        workflow_auth(request, root)
        row = repo.get(action=node)
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
        path = os.path.join(
            os.environ["V7_EXECUTION_WORKDIR"], row["execution_id"], "output", item["filename"]
        )
        ensure(os.path.isfile(path), "DEPENDENCY_UNAVAILABLE", "Node output unavailable", 503)
        return FileResponse(path, media_type="application/octet-stream")

    return app


def main():
    uvicorn.run(
        create_app(),
        host=os.environ.get("V7_EXECUTION_HOST", "127.0.0.1"),
        port=int(os.environ.get("V7_EXECUTION_PORT", "8012")),
        access_log=False,
    )
