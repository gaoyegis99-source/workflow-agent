"""Public P5.1 Knowledge gateway with current project authorization on every call."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from platform_contracts.knowledge import (
    EvidenceRead,
    KnowledgeDocumentList,
    KnowledgeFusionSearch,
    KnowledgeFusionSearchResult,
    KnowledgeImportCreate,
    KnowledgeImportReceipt,
    KnowledgeImportView,
    KnowledgeSearch,
    KnowledgeSearchResult,
    KnowledgeSemanticSearch,
    KnowledgeSemanticSearchResult,
    KnowledgeVersionView,
    KnowledgeWithdraw,
)
from platform_core.application.errors import RuleViolation
from platform_core.application.science import Science

from platform_api.adapters.http.control import Actor, Key, PathId

router = APIRouter()
P = "/projects/{project_id}/knowledge"


def call(
    request,
    actor,
    project,
    method,
    suffix,
    *,
    role="VIEWER",
    key=None,
    body=None,
    params=None,
):
    Science(
        request.app.state.database,
        request.app.state.data,
        request.app.state.execution,
        operator_catalog=request.app.state.operator_catalog,
        checker_catalog=request.app.state.checker_catalog,
    ).authorize(actor, project, role)
    remote = request.app.state.knowledge
    if remote is None:
        raise RuleViolation(
            "DEPENDENCY_UNAVAILABLE", "Knowledge service is not configured", 503, "SAME_KEY"
        )
    headers = {"X-Actor-ID": actor.principal_id}
    if key is not None:
        headers["Idempotency-Key"] = key
    return remote.json(
        method,
        f"/internal/projects/{project}/knowledge" + suffix,
        headers=headers,
        **({"json": body} if body is not None else {}),
        **({"params": params} if params is not None else {}),
    )


@router.post(P + "/imports", status_code=202, response_model=KnowledgeImportReceipt)
def create_import(
    project_id: PathId, body: KnowledgeImportCreate, actor: Actor, key: Key, request: Request
):
    result = call(
        request,
        actor,
        project_id,
        "POST",
        "/imports",
        role="EDITOR",
        key=key,
        body=body.model_dump(mode="json"),
    )
    # The internal view carries worker recovery details.  The public accept
    # response deliberately exposes only the typed receipt.
    return KnowledgeImportReceipt.model_validate(result, extra="ignore")


@router.get(P + "/imports/{import_id}", response_model=KnowledgeImportView)
def get_import(project_id: PathId, import_id: PathId, actor: Actor, request: Request):
    return call(request, actor, project_id, "GET", "/imports/" + import_id)


@router.get(P + "/documents", response_model=KnowledgeDocumentList)
def documents(
    project_id: PathId,
    actor: Actor,
    request: Request,
    after: Annotated[str | None, Query(max_length=768)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    return call(
        request,
        actor,
        project_id,
        "GET",
        "/documents",
        params={"limit": limit, **({"after": after} if after is not None else {})},
    )


@router.get(
    P + "/documents/{document_id}/versions/{version_id}", response_model=KnowledgeVersionView
)
def version(
    project_id: PathId,
    document_id: PathId,
    version_id: PathId,
    actor: Actor,
    request: Request,
):
    return call(
        request, actor, project_id, "GET", f"/documents/{document_id}/versions/{version_id}"
    )


@router.post(P + "/search", response_model=KnowledgeSearchResult)
def search(project_id: PathId, body: KnowledgeSearch, actor: Actor, request: Request):
    return call(
        request,
        actor,
        project_id,
        "POST",
        "/search",
        body=body.model_dump(mode="json"),
    )


@router.post(P + "/semantic-search", response_model=KnowledgeSemanticSearchResult)
def semantic_search(
    project_id: PathId, body: KnowledgeSemanticSearch, actor: Actor, request: Request
):
    """Candidate discovery only; exact evidence remains a separate read route."""

    return call(
        request,
        actor,
        project_id,
        "POST",
        "/semantic-search",
        body=body.model_dump(mode="json"),
    )


@router.post(P + "/fusion-search", response_model=KnowledgeFusionSearchResult)
def fusion_search(project_id: PathId, body: KnowledgeFusionSearch, actor: Actor, request: Request):
    return call(
        request,
        actor,
        project_id,
        "POST",
        "/fusion-search",
        body=body.model_dump(mode="json"),
    )


@router.get(
    P + "/documents/{document_id}/versions/{version_id}/units/{unit_id}",
    response_model=EvidenceRead,
)
def read(
    project_id: PathId,
    document_id: PathId,
    version_id: PathId,
    unit_id: PathId,
    actor: Actor,
    request: Request,
):
    return call(
        request,
        actor,
        project_id,
        "GET",
        f"/documents/{document_id}/versions/{version_id}/units/{unit_id}",
    )


@router.post(
    P + "/documents/{document_id}/versions/{version_id}/withdraw",
    response_model=KnowledgeVersionView,
)
def withdraw(
    project_id: PathId,
    document_id: PathId,
    version_id: PathId,
    body: KnowledgeWithdraw,
    actor: Actor,
    key: Key,
    request: Request,
):
    return call(
        request,
        actor,
        project_id,
        "POST",
        f"/documents/{document_id}/versions/{version_id}/withdraw",
        role="OWNER",
        key=key,
        body=body.model_dump(mode="json"),
    )
