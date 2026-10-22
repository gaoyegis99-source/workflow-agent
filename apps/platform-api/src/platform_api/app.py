"""Composition root. Assemble only capabilities implemented in this phase."""

import os
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from platform_contracts.authoring import (
    build_authoring_descriptor,
    validate_operator_parameters,
)
from platform_contracts.release import file_digest, production_mode, production_release
from platform_contracts.service import ReleaseIdentityMiddleware
from platform_core.application.authoring import semantic_environment
from platform_core.application.errors import RuleViolation
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import from_environment, knowledge_from_environment
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
from platform_api.adapters.http.knowledge import router as knowledge_router
from platform_api.adapters.http.remote import router as remote_router
from platform_api.adapters.http.research import router as research_router
from platform_api.adapters.http.science import internal
from platform_api.adapters.http.science import router as science_router
from platform_api.adapters.http.workflows import internal as workflow_internal
from platform_api.adapters.http.workflows import router as workflow_router


def create_app(
    database: Database | None = None,
    *,
    agent_runtime_profile: dict | None = None,
    agent_model_catalog: dict | None = None,
    agent_tool_catalog: dict | None = None,
    release_lock: dict | None = None,
) -> FastAPI:
    owned = database is None and bool(os.environ.get("V7_DATABASE_URL"))
    if owned:
        database = Database.from_environment()
    runtime_path = os.environ.get("V7_AGENT_RUNTIME_PROFILE")
    if agent_runtime_profile is None and runtime_path:
        from platform_core.application.runtime_config import load_runtime_profile

        agent_runtime_profile = load_runtime_profile(runtime_path)
    catalog_path = os.environ.get("V7_MODEL_CATALOG")
    if agent_model_catalog is None and catalog_path:
        from platform_core.application.model_catalog import load_catalog

        agent_model_catalog = load_catalog(catalog_path)
    tool_path = os.environ.get("V7_AGENT_TOOL_CATALOG")
    if agent_tool_catalog is None and tool_path:
        from platform_core.application.tool_catalog import load_tool_catalog

        agent_tool_catalog = load_tool_catalog(tool_path)
    from platform_core.application.catalog import configured_catalog

    operator_catalog = configured_catalog()
    authoring_descriptor = build_authoring_descriptor(operator_catalog)
    checker_path = os.environ.get("V7_CHECKER_CATALOG")
    cwl_image = os.environ.get("V7_CWL_IMAGE")
    checker_catalog = None
    if checker_path:
        from platform_core.application.checker_catalog import load_catalog as load_checker_catalog

        checker_catalog = load_checker_catalog(checker_path)
    release = release_lock
    if production_mode():
        operator_path = os.environ.get("V7_OPERATOR_CATALOG")
        if (
            not operator_path
            or not tool_path
            or agent_tool_catalog is None
            or not checker_path
            or checker_catalog is None
            or not cwl_image
        ):
            raise ValueError(
                "Production Platform requires operator, checker, tool and CWL validation inputs"
            )
        if agent_tool_catalog.get("catalog_id") not in {
            "p7-action-tools-v2",
            "p7-science-tools-v2",
        }:
            raise ValueError("Production cannot create Agent runs with a legacy tool catalogue")
        release = production_release(
            operator_path=operator_path,
            operator_catalog=operator_catalog,
            tool_path=tool_path,
            tool_catalog=agent_tool_catalog,
            catalogs={"checker_catalog": (checker_path, checker_catalog)},
            images={"cwl": cwl_image},
            version=version("groundwater-platform-api"),
        )
        studio_root = Path(__file__).parent / "studio_static"
        installed_assets = {
            path.relative_to(studio_root).as_posix(): file_digest(path)
            for path in sorted(studio_root.rglob("*"))
            if path.is_file()
        }
        if release.get("web_assets") != installed_assets:
            raise ValueError("Installed Capability Studio bundle differs from the release lock")
    authoring_environment = (
        semantic_environment(
            authoring_descriptor,
            cwl_image,
            release["release_digest"] if release else None,
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
            "P8.2 / A7: immutable workflow releases, lifecycle governance, "
            "maintenance-window upgrades, and recoverable deployment."
        ),
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.agent_runtime_profile = agent_runtime_profile
    app.state.agent_model_catalog = agent_model_catalog
    app.state.agent_tool_catalog = agent_tool_catalog
    app.state.checker_catalog = checker_catalog
    app.state.release_digest = release["release_digest"] if release else None
    app.state.release_lock = release
    # create_app fixtures do not enter lifespan; keep an independently validated
    # source catalogue available for synchronous TestClient-free use as well.
    app.state.operator_catalog = operator_catalog
    app.state.authoring_descriptor = authoring_descriptor
    app.state.authoring_parameter_validator = validate_operator_parameters
    app.state.authoring_environment = authoring_environment
    app.state.agent_configuration_complete = (agent_runtime_profile is None) == (
        agent_model_catalog is None
    )
    app.state.data, app.state.execution = from_environment()
    app.state.knowledge = knowledge_from_environment()
    app.add_middleware(ReleaseIdentityMiddleware)
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
    studio = Path(__file__).parent / "studio_static"
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
