"""Composition root. Assemble only capabilities implemented in this phase."""

from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from platform_contracts.authoring import (
    build_authoring_descriptor,
    validate_operator_parameters,
)
from platform_contracts.service import DeploymentIdentityMiddleware
from platform_core.application.authoring import semantic_environment
from platform_core.application.errors import RuleViolation
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import data_client, execution_client, knowledge_client
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from platform_api.adapters.http.authoring import router as authoring_router
from platform_api.adapters.http.control import router as control_router
from platform_api.adapters.http.errors import (
    RequestIdMiddleware,
    dependency_error,
    http_error,
    internal_error,
    rule_error,
    validation_error,
)
from platform_api.adapters.http.health import router
from platform_api.adapters.http.identity import IdentityResolver
from platform_api.adapters.http.internal_authorization import InternalExecutionPolicy
from platform_api.adapters.http.knowledge import router as knowledge_router
from platform_api.adapters.http.remote import router as remote_router
from platform_api.adapters.http.research import router as research_router
from platform_api.adapters.http.science import internal
from platform_api.adapters.http.science import router as science_router
from platform_api.adapters.http.workflows import internal as workflow_internal
from platform_api.adapters.http.workflows import router as workflow_router
from platform_api.composition import PlatformComposition


def create_app(
    database: Database | None = None,
    *,
    agent_runtime_profile: dict | None = None,
    agent_model_catalog: dict | None = None,
    agent_tool_catalog: dict | None = None,
    deployment_evidence: dict | None = None,
    # Historical test fixtures used this private constructor argument. It is
    # never read from a file or environment in a direct deployment.
    release_lock: dict | None = None,
    composition: PlatformComposition | None = None,
) -> FastAPI:
    composition = composition or (
        PlatformComposition.from_environment()
        if database is None
        else PlatformComposition.for_test()
    )
    owned = database is None and composition.database_url is not None
    if owned:
        database = Database(composition.database_url)
    runtime_path = composition.agent_runtime_profile_path
    if agent_runtime_profile is None and runtime_path:
        from platform_core.application.runtime_config import load_runtime_profile

        agent_runtime_profile = load_runtime_profile(runtime_path)
    catalog_path = composition.model_catalog_path
    if agent_model_catalog is None and catalog_path:
        from platform_core.application.model_catalog import load_catalog

        agent_model_catalog = load_catalog(catalog_path)
    tool_path = composition.agent_tool_catalog_path
    if agent_tool_catalog is None and tool_path:
        from platform_core.application.tool_catalog import load_tool_catalog

        agent_tool_catalog = load_tool_catalog(tool_path)
    operator_catalog = composition.operator_catalog
    authoring_descriptor = build_authoring_descriptor(operator_catalog)
    checker_path = composition.catalog.checker_catalog_path
    cwl_image = composition.cwl_image
    checker_catalog = composition.checker_catalog
    if composition.deployment.is_direct:
        operator_path = composition.catalog.operator_catalog_path
        if (
            not operator_path
            or not tool_path
            or agent_tool_catalog is None
            or not checker_path
            or checker_catalog is None
            or not cwl_image
        ):
            raise ValueError("Direct Platform requires operator, checker, tool and CWL inputs")
        if agent_tool_catalog.get("catalog_id") not in {
            "p7-action-tools-v2",
            "p7-science-tools-v2",
        }:
            raise ValueError(
                "Direct Platform cannot create Agent runs with a legacy tool catalogue"
            )
    authoring_environment = (
        semantic_environment(
            authoring_descriptor,
            cwl_image,
            composition.deployment.deployment_revision,
        )
        if cwl_image
        else None
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            if database:
                database.check_schema()
            app.state.operator_catalog = operator_catalog
            yield
        finally:
            if owned:
                database.close()

    app = FastAPI(
        title="Groundwater Platform V7",
        version=version("groundwater-platform-api"),
        description=(
            "P8 / A7: immutable workflow releases, lifecycle governance, "
            "direct-Compose maintenance, and recoverable deployment."
        ),
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.identity_resolver = IdentityResolver(
        database,
        mode=composition.identity_mode,
        proxy_token=composition.trusted_proxy_token,
    )
    app.state.agent_runtime_profile = agent_runtime_profile
    app.state.agent_model_catalog = agent_model_catalog
    app.state.agent_tool_catalog = agent_tool_catalog
    app.state.checker_catalog = checker_catalog
    # ``release_digest`` remains in existing read models for wire
    # compatibility.  It is now only the optional deployment revision and is
    # never obtained from, or validated against, a Release Lock.
    app.state.release_digest = composition.deployment.deployment_revision
    if deployment_evidence is not None and release_lock is not None:
        raise ValueError("Provide deployment_evidence once")
    app.state.deployment_evidence = (
        deployment_evidence
        or release_lock
        or {
            "deployment_id": composition.deployment.deployment_id,
            # Database/read-model fields keep ``release_digest`` for historical
            # wire compatibility; the value is direct deployment metadata only.
            "release_digest": composition.deployment.deployment_revision,
        }
    )
    app.state.deployment_context = composition.deployment
    app.state.catalog_context = composition.catalog
    app.state.internal_execution_policy = InternalExecutionPolicy(
        composition.internal_execution.token,
        composition.internal_execution.maintenance_file,
    )
    # create_app fixtures do not enter lifespan; keep an independently validated
    # source catalogue available for synchronous TestClient-free use as well.
    app.state.operator_catalog = operator_catalog
    app.state.authoring_descriptor = authoring_descriptor
    app.state.authoring_parameter_validator = validate_operator_parameters
    app.state.authoring_environment = authoring_environment
    app.state.agent_configuration_complete = (agent_runtime_profile is None) == (
        agent_model_catalog is None
    )
    app.state.data = data_client(composition.data, deployment=composition.deployment)
    app.state.execution = execution_client(composition.execution, deployment=composition.deployment)
    app.state.knowledge = knowledge_client(composition.knowledge, deployment=composition.deployment)
    app.add_middleware(DeploymentIdentityMiddleware, deployment=composition.deployment)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, internal_error)
    app.add_exception_handler(RuleViolation, rule_error)
    app.add_exception_handler(StorageError, dependency_error)
    app.include_router(router, prefix="/api/v1")
    app.include_router(control_router, prefix="/api/v1")
    app.include_router(authoring_router, prefix="/api/v1")
    app.include_router(science_router, prefix="/api/v1")
    app.include_router(workflow_router, prefix="/api/v1")
    app.include_router(remote_router, prefix="/api/v1")
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(research_router, prefix="/api/v1")
    app.include_router(workflow_internal)
    app.include_router(internal)
    studio = composition.studio_root
    if composition.deployment.is_direct and not (
        (studio / "index.html").is_file() and (studio / "assets").is_dir()
    ):
        raise ValueError("Direct Platform requires a packaged Capability Studio bundle")
    app.mount(
        "/assets",
        StaticFiles(directory=studio / "assets", check_dir=False),
        name="web-assets",
    )

    def platform_page():
        return FileResponse(
            studio / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; connect-src 'self'; "
                    "img-src 'self' blob: data:; font-src 'self'; "
                    "script-src 'self'; worker-src 'self' blob:; "
                    "style-src 'self' 'unsafe-inline'; "
                    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/", include_in_schema=False)
    def platform_root():
        return platform_page()

    @app.get("/projects/{spa_path:path}", include_in_schema=False)
    def platform_project_route(spa_path: str):
        return platform_page()

    return app
