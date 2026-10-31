"""Composition root for the internal Execution HTTP API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.metadata import version

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from platform_contracts.engine_runtime import SELECTED_PROFILE_ID, profile
from platform_contracts.service import DeploymentIdentityMiddleware, ServiceFault
from sqlalchemy.exc import SQLAlchemyError

from platform_execution.adapters.repository import Repository
from platform_execution.api_actions import register_action_routes
from platform_execution.api_context import ExecutionApiContext
from platform_execution.api_contracts import plan_fingerprint, validate_execution_plan
from platform_execution.api_health import register_health_routes
from platform_execution.api_workflows import register_workflow_routes
from platform_execution.composition import ExecutionComposition
from platform_execution.database import Database

__all__ = (
    "create_app",
    "main",
    "plan_fingerprint",
    "validate_execution_plan",
)


def create_app(
    database=None,
    token=None,
    workflow_profile_id=SELECTED_PROFILE_ID,
    *,
    composition: ExecutionComposition | None = None,
):
    """Assemble the stable API without embedding route algorithms here."""

    composition = composition or (
        ExecutionComposition.from_environment()
        if database is None and token is None
        else ExecutionComposition.for_test(
            database_url="postgresql://unused", platform_token=token or "x" * 32
        )
    )
    owned = database is None
    database = database or Database(composition.database_url)
    platform_token = token or composition.platform_token
    operator_catalog_path = composition.catalog.operator_catalog_path
    operator_catalog = None
    if operator_catalog_path:
        from platform_execution.operator_catalog import load

        operator_catalog = load(operator_catalog_path)
    checker_catalog_path = composition.catalog.checker_catalog_path
    checker_catalog = None
    if checker_catalog_path:
        from platform_execution.checker_catalog import load as load_checker_catalog

        checker_catalog = load_checker_catalog(
            checker_catalog_path,
            composition.catalog.image("checker"),
            composition.catalog.image("gdal_checker"),
        )
    context = ExecutionApiContext(
        repository=Repository(database),
        composition=composition,
        platform_token=platform_token,
        workflow_profile=profile(workflow_profile_id),
        operator_catalog=operator_catalog,
        checker_catalog=checker_catalog,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        database.check_schema()
        yield
        if owned:
            database.close()

    app = FastAPI(
        title="V7 Execution",
        version=version("groundwater-execution-service"),
        lifespan=lifespan,
    )
    app.add_middleware(DeploymentIdentityMiddleware, deployment=composition.runtime.deployment)
    app.state.operator_catalog = operator_catalog
    app.state.checker_catalog = checker_catalog
    # This response field remains stable while a direct deployment supplies a
    # non-authorizing revision string instead of a Release Lock digest.
    app.state.release_digest = composition.runtime.deployment.deployment_revision
    app.state.runtime_config = composition.runtime

    @app.exception_handler(ServiceFault)
    async def fault(request: Request, exc: ServiceFault):
        del request
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request: Request, exc: SQLAlchemyError):
        del request
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
            {"code": "DEPENDENCY_UNAVAILABLE", "message": "Execution unavailable"},
            status_code=503,
        )

    register_health_routes(app, database=database, operator_catalog_path=operator_catalog_path)
    register_action_routes(app, context)
    register_workflow_routes(app, context)
    return app


def main() -> None:
    composition = ExecutionComposition.from_environment()
    uvicorn.run(
        create_app(composition=composition),
        host=composition.server_host,
        port=composition.server_port,
        access_log=False,
    )
