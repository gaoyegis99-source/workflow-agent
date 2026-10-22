"""Public P3 adapters: current project permissions precede every service call."""

import hmac
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from platform_contracts.science import (
    AcceptanceCreate,
    AcceptanceList,
    AcceptanceReceipt,
    AcceptanceView,
    AssetList,
    AssetView,
    DeliverableCreate,
    DeliverableList,
    DeliverableView,
    DocumentUploadFinalize,
    ExecutionCancel,
    ExecutionCreate,
    ExecutionView,
    HumanReviewCreate,
    OperatorList,
    OperatorView,
    SemanticRevision,
    SemanticRevisionView,
    SpatialExecutionCreate,
    SpatialSemanticRevision,
    SpatialUploadFinalize,
    TableExecutionCreate,
    UploadCreate,
    UploadFinalize,
    UploadList,
    UploadReceipt,
    UploadView,
)
from platform_contracts.v1 import CommandReceipt
from platform_contracts.workflows import WorkflowExecutionCreate
from platform_core.application.catalog import operators as catalog_operators
from platform_core.application.catalog import public_operator
from platform_core.application.errors import RuleViolation
from platform_core.application.science import Science
from platform_core.application.workflows import Workflows
from starlette.responses import StreamingResponse

from platform_api.adapters.http.control import Actor, Key, PathId, receipt

router = APIRouter()
P = "/projects/{project_id}"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def service(request):
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database not configured", 503, "SAME_KEY")
    return Science(
        request.app.state.database,
        request.app.state.data,
        request.app.state.execution,
        operator_catalog=request.app.state.operator_catalog,
    )


def _project_operators(request: Request, project_id: str, *, include_inactive: bool = False):
    """Merge project-scoped immutable remote bindings into public discovery."""

    items = catalog_operators(request.app.state.operator_catalog, include_inactive=include_inactive)
    if request.app.state.database is None:
        yield from items
        return
    with request.app.state.database.transaction() as uow:
        bindings = uow.service_bindings.list(project_id)
    for item in items:
        implementations = list(item["implementations"])
        for binding in bindings:
            if not binding["enabled"]:
                continue
            target = next(
                (
                    value
                    for value in binding["targets"]
                    if value["operator_id"] == item["operator_id"]
                    and value["version_id"] == item["version_id"]
                ),
                None,
            )
            if target is None:
                continue
            implementation_digest = target.get("implementation_digest") or binding["binding_digest"]
            implementations.append(
                {
                    "implementation_id": target["remote_implementation_id"],
                    "binding_kind": "REMOTE",
                    "runtime": "REMOTE",
                    "implementation_digest": implementation_digest,
                    "protocol": binding["protocol"],
                    "resource_profile": "remote-bound-v1",
                    "service_binding_id": binding["service_binding_id"],
                    "binding_revision": binding["revision"],
                }
            )
        item = public_operator(item, implementations=implementations)
        yield item


async def bounded_upload_body(request: Request) -> bytes:
    """Consume the public upload once before opening the internal request.

    Passing ``request.stream()`` directly to another HTTP client couples two
    independent ASGI connection lifetimes.  A stalled downstream connection can
    then leave the public request unread and the proxy task unable to finish.
    Uploads already have a public 20 MiB contract, so a bounded materialization
    gives the internal request a deterministic content length without permitting
    unbounded memory use.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_UPLOAD_BYTES:
                raise RuleViolation("MALFORMED_REQUEST", "Upload exceeds size limit", 413)
        except ValueError as exc:
            raise RuleViolation("MALFORMED_REQUEST", "Invalid Content-Length", 400) from exc

    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
            raise RuleViolation("MALFORMED_REQUEST", "Upload exceeds size limit", 413)
        content.extend(chunk)
    return bytes(content)


@router.post(P + "/uploads", status_code=201, response_model=UploadView)
def upload(project_id: PathId, actor: Actor, key: Key, body: UploadCreate, request: Request):
    return service(request).data_call(
        actor, project_id, "POST", "/uploads", key, body.model_dump(mode="json")
    )


@router.get(P + "/uploads", response_model=UploadList)
def uploads(
    project_id: PathId,
    actor: Actor,
    request: Request,
    after: Annotated[str | None, Query(max_length=768)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    return service(request).data_call(
        actor,
        project_id,
        "GET",
        "/uploads",
        params={"limit": limit, **({"after": after} if after is not None else {})},
    )


@router.get(P + "/uploads/{upload_id}", response_model=UploadView)
def get_upload(project_id: PathId, upload_id: PathId, actor: Actor, request: Request):
    return service(request).data_call(actor, project_id, "GET", "/uploads/" + upload_id)


@router.put(P + "/uploads/{upload_id}/content", response_model=UploadView)
async def content(project_id: PathId, upload_id: PathId, actor: Actor, request: Request):
    app = service(request)
    app.authorize(actor, project_id, "EDITOR")
    if app.data is None:
        raise RuleViolation(
            "DEPENDENCY_UNAVAILABLE", "Data service not configured", 503, "SAME_KEY"
        )
    body = await bounded_upload_body(request)
    return await app.data.upload(
        f"/internal/projects/{project_id}/uploads/{upload_id}/content", body
    )


@router.post(P + "/uploads/{upload_id}/finalize", status_code=202, response_model=UploadReceipt)
def finalize(
    project_id: PathId,
    upload_id: PathId,
    actor: Actor,
    key: Key,
    body: UploadFinalize | SpatialUploadFinalize | DocumentUploadFinalize,
    request: Request,
):
    return service(request).data_call(
        actor,
        project_id,
        "POST",
        f"/uploads/{upload_id}/finalize",
        key,
        body.model_dump(mode="json"),
    )


@router.get(P + "/assets", response_model=AssetList)
def assets(
    project_id: PathId,
    actor: Actor,
    request: Request,
    after: Annotated[str | None, Query(max_length=768)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    return service(request).data_call(
        actor,
        project_id,
        "GET",
        "/assets",
        params={"limit": limit, **({"after": after} if after is not None else {})},
    )


@router.get(P + "/assets/{asset_id}/versions/{version_id}", response_model=AssetView)
def asset(project_id: PathId, asset_id: PathId, version_id: PathId, actor: Actor, request: Request):
    return service(request).data_call(
        actor, project_id, "GET", f"/assets/{asset_id}/versions/{version_id}"
    )


@router.get(
    P + "/assets/{asset_id}/versions/{version_id}/representations/{representation_id}/content"
)
async def download(
    project_id: PathId,
    asset_id: PathId,
    version_id: PathId,
    representation_id: PathId,
    actor: Actor,
    request: Request,
):
    app = service(request)
    app.authorize(actor, project_id)
    if app.data is None:
        raise RuleViolation(
            "DEPENDENCY_UNAVAILABLE", "Data service not configured", 503, "SAME_KEY"
        )
    stream, digest, media_type = await app.data.download(
        f"/internal/projects/{project_id}/assets/{asset_id}"
        f"/versions/{version_id}/representations/{representation_id}/content"
    )
    return StreamingResponse(
        stream,
        media_type=media_type or "application/octet-stream",
        headers={"Digest": digest} if digest else {},
    )


@router.post(
    P + "/assets/{asset_id}/semantic-revisions",
    status_code=201,
    response_model=SemanticRevisionView,
)
def semantic(
    project_id: PathId,
    asset_id: PathId,
    actor: Actor,
    key: Key,
    body: SemanticRevision | SpatialSemanticRevision,
    request: Request,
):
    return service(request).data_call(
        actor,
        project_id,
        "POST",
        f"/assets/{asset_id}/semantic-revisions",
        key,
        body.model_dump(mode="json"),
    )


@router.get(P + "/operators", response_model=OperatorList)
def operators(project_id: PathId, actor: Actor, request: Request):
    service(request).authorize(actor, project_id)
    return {"items": list(_project_operators(request, project_id))}


@router.get(P + "/operators/{operator_id}/versions/{version_id}", response_model=OperatorView)
def operator(
    project_id: PathId, operator_id: PathId, version_id: str, actor: Actor, request: Request
):
    service(request).authorize(actor, project_id)
    for item in _project_operators(request, project_id, include_inactive=True):
        if item["operator_id"] == operator_id and item["version_id"] == version_id:
            return item
    raise RuleViolation("RESOURCE_NOT_FOUND", "Operator version not found", 404)


@router.post(P + "/executions", status_code=202, response_model=CommandReceipt)
def submit(
    project_id: PathId,
    actor: Actor,
    key: Key,
    body: ExecutionCreate | SpatialExecutionCreate | TableExecutionCreate | WorkflowExecutionCreate,
    request: Request,
):
    value = body.model_dump(mode="json")
    # Preserve pre-P5.2 idempotency digests when the optional linkage is absent.
    if value.get("research_context") is None:
        value.pop("research_context", None)
    if value["target"]["kind"] == "WORKFLOW":
        record = Workflows(
            request.app.state.database,
            request.app.state.data,
            request.app.state.execution,
            operator_catalog=request.app.state.operator_catalog,
        ).submit(actor, project_id, key, value)
    else:
        record = service(request).submit(actor, project_id, key, value)
    return receipt(record, request)


@router.get(P + "/executions/{execution_id}", response_model=ExecutionView)
def execution(project_id: PathId, execution_id: PathId, actor: Actor, request: Request):
    return service(request).execution_view(actor, project_id, execution_id)


@router.post(
    P + "/executions/{execution_id}/cancel", status_code=202, response_model=CommandReceipt
)
def cancel(
    project_id: PathId,
    execution_id: PathId,
    actor: Actor,
    key: Key,
    body: ExecutionCancel,
    request: Request,
):
    record = service(request).cancel(
        actor, project_id, execution_id, key, body.model_dump(mode="json")
    )
    return receipt(record, request)


@router.get(P + "/tasks/{task_id}/activities")
def activities(project_id: PathId, task_id: PathId, actor: Actor, request: Request):
    return {"items": service(request).activities(actor, project_id, task_id)}


internal = APIRouter(include_in_schema=False)


@internal.post("/internal/actions/{action_id}/permit")
def permit(action_id: PathId, body: dict, request: Request):
    token = os.environ.get("V7_EXECUTION_PLATFORM_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + token
    ):
        raise RuleViolation("AUTHENTICATION_REQUIRED", "Service credential required", 401)
    marker = os.environ.get("V7_MAINTENANCE_FILE")
    if marker and Path(marker).is_file():
        return {"decision": "WAIT", "reason": "DEPLOYMENT_PARKING"}
    return service(request).permit(action_id, body["execution_id"], body["attempt_id"])


@internal.post("/internal/actions/{action_id}/publication-permit")
def publication_permit(action_id: PathId, request: Request):
    token = os.environ.get("V7_EXECUTION_PLATFORM_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + token
    ):
        raise RuleViolation("AUTHENTICATION_REQUIRED", "Service credential required", 401)
    return service(request).permit_publication(action_id)


@router.post(P + "/tasks/{task_id}/deliverables", status_code=201, response_model=DeliverableView)
def candidate(
    project_id: PathId,
    task_id: PathId,
    actor: Actor,
    key: Key,
    body: DeliverableCreate,
    request: Request,
):
    return service(request).candidate(actor, project_id, task_id, key, body.model_dump(mode="json"))


@router.get(P + "/tasks/{task_id}/deliverables", response_model=DeliverableList)
def candidates(project_id: PathId, task_id: PathId, actor: Actor, request: Request):
    return {"items": service(request).candidates(actor, project_id, task_id)}


@router.post(P + "/tasks/{task_id}/acceptance", status_code=202, response_model=AcceptanceReceipt)
def accept(
    project_id: PathId,
    task_id: PathId,
    actor: Actor,
    key: Key,
    body: AcceptanceCreate,
    request: Request,
):
    return service(request).accept(actor, project_id, task_id, key, body.model_dump(mode="json"))


@router.get(P + "/tasks/{task_id}/acceptance", response_model=AcceptanceList)
def checks(project_id: PathId, task_id: PathId, actor: Actor, request: Request):
    return {"items": service(request).checks(actor, project_id, task_id)}


@router.get(P + "/tasks/{task_id}/acceptance/{acceptance_id}", response_model=AcceptanceView)
def check(
    project_id: PathId, task_id: PathId, acceptance_id: PathId, actor: Actor, request: Request
):
    return service(request).checks(actor, project_id, task_id, acceptance_id)[0]


@router.post(
    P + "/tasks/{task_id}/acceptance/{acceptance_id}/reviews",
    status_code=201,
)
def review(
    project_id: PathId,
    task_id: PathId,
    acceptance_id: PathId,
    actor: Actor,
    key: Key,
    body: HumanReviewCreate,
    request: Request,
):
    return service(request).review(
        actor,
        project_id,
        task_id,
        acceptance_id,
        key,
        body.model_dump(mode="json"),
    )
