"""Action submission and observation routes for the Execution API."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from platform_contracts.service import digest, ensure

from platform_execution.api_context import ExecutionApiContext
from platform_execution.api_contracts import Id, validate_execution_plan
from platform_execution.engine_package import validate as validate_engine_package


def register_action_routes(app: FastAPI, context: ExecutionApiContext) -> None:
    """Register public-internal Action endpoints with explicit route context."""

    repository = context.repository

    @app.post(
        "/internal/actions/{action}",
        dependencies=[Depends(context.authenticate_platform)],
        status_code=202,
    )
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
                if context.operator_catalog is not None:
                    try:
                        for frozen in frozen_operators.values():
                            exact_snapshot(context.operator_catalog, frozen)
                    except ValueError as error:
                        ensure(False, "INVALID_BINDING", str(error), 422)
        else:
            remote = target.get("execution_binding", {}).get("kind") == "REMOTE"
            if remote:
                from platform_execution.implementations import remote_implementation

                try:
                    remote_profile = remote_implementation(body)
                except (KeyError, TypeError, ValueError):
                    ensure(
                        False,
                        "UNSUPPORTED_TARGET",
                        "Remote binding is not registered",
                        422,
                    )
                if body.get("_operator_snapshot") is not None and context.operator_catalog:
                    from platform_execution.operator_catalog import exact_snapshot

                    try:
                        exact_snapshot(context.operator_catalog, body["_operator_snapshot"])
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

                    if context.operator_catalog is not None:
                        try:
                            exact_snapshot(context.operator_catalog, body["_operator_snapshot"])
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
            {"_engine_runtime_profile": context.workflow_profile} if kind == "WORKFLOW" else None
        )
        if kind != "WORKFLOW" and target.get("execution_binding", {}).get("kind") == "REMOTE":
            initial_runtime = {
                "implementation_profile": remote_profile,
                "remote": {
                    "service_binding_id": body["_remote_binding"]["service_binding_id"],
                    "binding_revision": body["_remote_binding"]["revision"],
                    "state": "STAGING_INPUT",
                },
            }
        row = repository.create(
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
        return repository.observation(row)

    @app.get("/internal/actions/{action}", dependencies=[Depends(context.authenticate_platform)])
    def query(action: Id):
        return repository.observation(repository.get(action=action))

    @app.post(
        "/internal/actions/{action}/cancel",
        dependencies=[Depends(context.authenticate_platform)],
    )
    def cancel(action: Id):
        return repository.observation(repository.cancel(action))

    @app.get(
        "/internal/projects/{project}/executions/{execution}",
        dependencies=[Depends(context.authenticate_platform)],
    )
    def execution(project: Id, execution: Id):
        return repository.view(repository.get(execution=execution, project=project))


__all__ = ("register_action_routes",)
