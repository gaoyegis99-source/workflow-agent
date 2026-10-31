"""Authenticated internal Knowledge HTTP API."""

import logging
import re
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from platform_contracts.knowledge import (
    KnowledgeFusionSearch,
    KnowledgeImportCreate,
    KnowledgeScopedSearch,
    KnowledgeScopedSemanticSearch,
    KnowledgeSearch,
    KnowledgeSemanticSearch,
    KnowledgeWithdraw,
    SemanticIndexProfile,
)
from platform_contracts.service import (
    DeploymentIdentityMiddleware,
    ServiceFault,
    authenticate,
)
from sqlalchemy.exc import SQLAlchemyError

from platform_knowledge.application import Knowledge
from platform_knowledge.blobs import Blobs
from platform_knowledge.composition import KnowledgeComposition
from platform_knowledge.database import Database
from platform_knowledge.repository import Repository

PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])")
Id = Annotated[str, Path(pattern=PATTERN)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
Actor = Annotated[str, Header(alias="X-Actor-ID", pattern=PATTERN)]
ReadReceipt = Annotated[str | None, Header(alias="X-Read-Receipt-ID", pattern=PATTERN)]


def create_app(
    database=None, blobs=None, credentials=None, *, composition=None, semantic_encoder=None
):
    composition = composition or (
        KnowledgeComposition.from_environment()
        if database is None
        else KnowledgeComposition.for_test()
    )
    owned = database is None
    database = database or Database(composition.database_url)
    blobs = blobs or Blobs(composition.storage_root)
    credentials = credentials or {"platform": composition.platform_token}
    service = Knowledge(Repository(database), blobs, semantic_encoder=semantic_encoder)

    @asynccontextmanager
    async def lifespan(app):
        database.check_schema()
        yield
        if owned:
            database.close()

    def platform(request: Request):
        return authenticate(request.headers.get("authorization"), credentials, {"platform"})

    app = FastAPI(
        title="V7 Knowledge",
        version=package_version("groundwater-knowledge-service"),
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
        logging.error("Knowledge database operation failed category=%s", type(exc).__name__)
        return JSONResponse(
            {"code": "DEPENDENCY_UNAVAILABLE", "message": "Knowledge store unavailable"},
            status_code=503,
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "knowledge"}

    @app.get("/readyz")
    def ready():
        database.check_schema()
        return {"status": "ready"}

    p = "/internal/projects/{project}/knowledge"

    @app.post(p + "/imports", status_code=202, dependencies=[Depends(platform)])
    def create_import(project: Id, body: KnowledgeImportCreate, actor: Actor, key: Key):
        return service.create_import(project, actor, key, body.model_dump(mode="json"))

    @app.get(p + "/imports/{import_id}", dependencies=[Depends(platform)])
    def get_import(project: Id, import_id: Id):
        return service.import_view(project, import_id)

    @app.get(p + "/documents", dependencies=[Depends(platform)])
    def documents(
        project: Id,
        after: str | None = Query(default=None, max_length=768),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return service.documents(project, after, limit)

    @app.get(p + "/documents/{document}/versions/{version}", dependencies=[Depends(platform)])
    def version(project: Id, document: Id, version: Id):
        return service.version(project, document, version)

    @app.post(p + "/search", dependencies=[Depends(platform)])
    def search(project: Id, body: KnowledgeSearch):
        return service.search(project, body.model_dump(mode="json"))

    @app.post(p + "/search-scoped", dependencies=[Depends(platform)])
    def search_scoped(project: Id, body: KnowledgeScopedSearch):
        return service.search(project, body.model_dump(mode="json"))

    @app.post(p + "/semantic-search", dependencies=[Depends(platform)])
    def semantic_search(project: Id, body: KnowledgeSemanticSearch):
        return service.semantic_search(project, body.model_dump(mode="json"))

    @app.post(p + "/semantic-search-scoped", dependencies=[Depends(platform)])
    def semantic_search_scoped(project: Id, body: KnowledgeScopedSemanticSearch):
        return service.semantic_search(project, body.model_dump(mode="json"))

    @app.post(
        p + "/semantic-indexes/{index_id}/rebuild",
        dependencies=[Depends(platform)],
    )
    def rebuild_semantic_index(project: Id, index_id: Id, body: SemanticIndexProfile):
        """Internal, explicit lifecycle operation; public reads cannot trigger it."""

        return service.rebuild_semantic_index(project, index_id, body)

    @app.post(p + "/fusion-search", dependencies=[Depends(platform)])
    def fusion_search(project: Id, body: KnowledgeFusionSearch):
        return service.fusion_search(project, body.model_dump(mode="json"))

    @app.get(
        p + "/documents/{document}/versions/{version}/units/{unit}",
        dependencies=[Depends(platform)],
    )
    def read(
        project: Id,
        document: Id,
        version: Id,
        unit: Id,
        actor: Actor,
        read_receipt: ReadReceipt = None,
    ):
        return service.read(project, document, version, unit, actor, read_receipt)

    @app.post(
        p + "/documents/{document}/versions/{version}/withdraw",
        dependencies=[Depends(platform)],
    )
    def withdraw(
        project: Id,
        document: Id,
        version: Id,
        body: KnowledgeWithdraw,
        actor: Actor,
        key: Key,
    ):
        return service.withdraw(
            project, document, version, actor, key, body.model_dump(mode="json")
        )

    @app.post("/internal/lifecycle-events/claim", dependencies=[Depends(platform)])
    def claim_lifecycle(body: dict):
        owner, seconds = body.get("owner"), body.get("lease_seconds", 30)
        if (
            not isinstance(owner, str)
            or not PATTERN.fullmatch(owner)
            or type(seconds) is not int
            or not 1 <= seconds <= 300
        ):
            raise ServiceFault("MALFORMED_REQUEST", "Invalid lifecycle claim", 422)
        return {"event": service.repo.claim_lifecycle(owner, seconds)}

    @app.post("/internal/lifecycle-events/{event_id}/ack", dependencies=[Depends(platform)])
    def ack_lifecycle(event_id: Id, body: dict):
        owner, generation = body.get("owner"), body.get("generation")
        if (
            not isinstance(owner, str)
            or not PATTERN.fullmatch(owner)
            or type(generation) is not int
            or generation < 1
        ):
            raise ServiceFault("MALFORMED_REQUEST", "Invalid lifecycle acknowledgement", 422)
        return service.repo.ack_lifecycle(event_id, owner, generation)

    @app.post("/internal/lifecycle-events/{event_id}/renew", dependencies=[Depends(platform)])
    def renew_lifecycle(event_id: Id, body: dict):
        owner, generation = body.get("owner"), body.get("generation")
        seconds = body.get("lease_seconds", 30)
        if (
            not isinstance(owner, str)
            or not PATTERN.fullmatch(owner)
            or type(generation) is not int
            or generation < 1
            or type(seconds) is not int
            or not 1 <= seconds <= 300
        ):
            raise ServiceFault("MALFORMED_REQUEST", "Invalid lifecycle renewal", 422)
        return service.repo.renew_lifecycle(event_id, owner, generation, seconds)

    app.state.service = service
    return app


def main():
    composition = KnowledgeComposition.from_environment()
    uvicorn.run(
        create_app(composition=composition),
        host=composition.host,
        port=composition.port,
        access_log=False,
    )
