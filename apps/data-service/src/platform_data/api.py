"""Authenticated internal Data HTTP surface. Public authorization remains in Platform."""

import logging
import re
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from platform_contracts.science import (
    DocumentUploadFinalize,
    InputSelection,
    SemanticRevision,
    SpatialSelection,
    SpatialSemanticRevision,
    SpatialUploadFinalize,
    TableSelection,
    UploadCreate,
    UploadFinalize,
)
from platform_contracts.service import (
    DeploymentIdentityMiddleware,
    ServiceFault,
    authenticate,
    ensure,
)
from sqlalchemy.exc import SQLAlchemyError

from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.application.data import Data
from platform_data.application.publication import Publication
from platform_data.composition import DataComposition
from platform_data.database import Database
from platform_data.domain.model import MAX_BYTES

PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])")
Id = Annotated[str, Path(pattern=PATTERN)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
Actor = Annotated[str, Header(alias="X-Actor-ID", pattern=PATTERN)]


def create_app(database=None, blobs=None, credentials=None, *, composition=None):
    composition = composition or (
        DataComposition.from_environment() if database is None else DataComposition.for_test()
    )
    owned = database is None
    database = database or Database(composition.database_url)
    blobs = blobs or Blobs(composition.storage_root)
    credentials = credentials or composition.credentials
    service = Data(Repository(database), blobs)

    @asynccontextmanager
    async def lifespan(app):
        database.check_schema()
        yield
        if owned:
            database.close()

    def authenticated(request: Request):
        return authenticate(
            request.headers.get("authorization"),
            credentials,
            {"platform", "execution", "knowledge"},
        )

    def platform(request: Request):
        return authenticate(request.headers.get("authorization"), credentials, {"platform"})

    app = FastAPI(
        title="V7 Data",
        version=package_version("groundwater-data-service"),
        lifespan=lifespan,
    )
    app.add_middleware(DeploymentIdentityMiddleware, deployment=composition.deployment)

    @app.exception_handler(ServiceFault)
    async def fault(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return JSONResponse(
            {"code": "MALFORMED_REQUEST", "message": "Invalid request"}, status_code=422
        )

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, exc):
        original = getattr(exc, "orig", None)
        logging.error(
            "Data database operation failed category=%s sqlstate=%s detail=%s",
            type(exc).__name__,
            getattr(original, "sqlstate", None),
            str(exc).replace("\n", " ")[:500],
        )
        return JSONResponse(
            {"code": "DEPENDENCY_UNAVAILABLE", "message": "Data store unavailable"}, status_code=503
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "data"}

    @app.get("/readyz")
    def ready():
        database.check_schema()
        return {"status": "ready"}

    p = "/internal/projects/{project}"

    @app.post(p + "/uploads", status_code=201, dependencies=[Depends(platform)])
    def create(project: Id, body: UploadCreate, actor: Actor, key: Key):
        return service.create(project, actor, key, body.model_dump(mode="json"))

    @app.get(p + "/uploads", dependencies=[Depends(platform)])
    def uploads(
        project: Id,
        after: str | None = Query(default=None, max_length=768),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return service.list_page(project, "uploads", after, limit)

    @app.get(p + "/uploads/{id}", dependencies=[Depends(platform)])
    def upload(project: Id, id: Id):
        return service.get(project, id)

    @app.put(p + "/uploads/{id}/content", dependencies=[Depends(platform)])
    async def content(project: Id, id: Id, request: Request):
        expected = service.get(project, id)["size"]
        data = bytearray()
        async for part in request.stream():
            ensure(
                len(data) + len(part) <= min(expected, MAX_BYTES),
                "MALFORMED_REQUEST",
                "Upload exceeds declared size",
                413,
            )
            data.extend(part)
        return service.content(project, id, bytes(data))

    @app.post(p + "/uploads/{id}/finalize", status_code=202, dependencies=[Depends(platform)])
    def finalize(
        project: Id,
        id: Id,
        body: UploadFinalize | SpatialUploadFinalize | DocumentUploadFinalize,
        actor: Actor,
        key: Key,
    ):
        return service.finalize(project, actor, id, key, body.model_dump(mode="json"))

    @app.get(p + "/assets", dependencies=[Depends(platform)])
    def assets(
        project: Id,
        after: str | None = Query(default=None, max_length=768),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return service.list_page(project, "assets", after, limit)

    @app.get(p + "/assets/{id}/versions/{version}", dependencies=[Depends(authenticated)])
    def asset(project: Id, id: Id, version: Id):
        return service.asset(project, id, version)

    @app.get(
        p + "/assets/{id}/versions/{version}/representations/{representation}/content",
        dependencies=[Depends(authenticated)],
    )
    def representation(project: Id, id: Id, version: Id, representation: Id):
        path, metadata = service.representation(project, id, version, representation)
        media_type = {
            "png": "image/png",
            "json": "application/json",
            "geopackage": "application/geopackage+sqlite3",
            "geotiff": "image/tiff",
            "parquet": "application/vnd.apache.parquet",
            "markdown": "text/markdown; charset=utf-8",
            "text": "text/plain; charset=utf-8",
        }.get(metadata.get("format"), "application/octet-stream")
        return FileResponse(path, media_type=media_type, headers={"Digest": metadata["digest"]})

    @app.post(
        p + "/assets/{id}/semantic-revisions", status_code=201, dependencies=[Depends(platform)]
    )
    def semantic(
        project: Id,
        id: Id,
        body: SemanticRevision | SpatialSemanticRevision,
        actor: Actor,
        key: Key,
    ):
        return service.semantic(project, actor, id, key, body.model_dump(mode="json"))

    @app.post(p + "/resolve", dependencies=[Depends(authenticated)])
    def resolve(project: Id, body: InputSelection | SpatialSelection | TableSelection):
        return service.resolve(project, body.model_dump(mode="json"))

    publications = Publication(service.repo, blobs)

    def execution_auth(request: Request):
        return authenticate(request.headers.get("authorization"), credentials, {"execution"})

    pub = p + "/publications/{execution}"

    @app.get(pub, dependencies=[Depends(authenticated)])
    def publication(project: Id, execution: Id):
        return publications.get(project, execution)

    @app.post(pub + "/prepare", dependencies=[Depends(execution_auth)])
    def prepare(project: Id, execution: Id, body: dict):
        return publications.prepare(project, execution, body["generation"], body["manifest"])

    @app.put(pub + "/files/{representation}", dependencies=[Depends(execution_auth)])
    async def file(
        project: Id, execution: Id, representation: Id, request: Request, generation: int
    ):
        content = bytearray()
        async for part in request.stream():
            ensure(
                len(content) + len(part) <= 100 * 1024 * 1024,
                "MALFORMED_REQUEST",
                "Output exceeds limit",
                413,
            )
            content.extend(part)
        return publications.file(project, execution, generation, representation, bytes(content))

    @app.post(pub + "/commit", dependencies=[Depends(execution_auth)])
    def commit(project: Id, execution: Id, body: dict):
        return publications.commit(project, execution, body["generation"])

    @app.post(pub + "/abort", dependencies=[Depends(execution_auth)])
    def abort(project: Id, execution: Id, body: dict):
        return publications.abort(project, execution, body["generation"])

    app.state.service = service
    return app


def main():
    composition = DataComposition.from_environment()
    uvicorn.run(
        create_app(composition=composition),
        host=composition.host,
        port=composition.port,
        access_log=False,
    )
