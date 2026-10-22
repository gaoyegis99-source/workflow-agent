"""Public workflow package endpoints."""

import hmac
import os
from pathlib import Path

from fastapi import APIRouter, Request
from platform_contracts.workflows import (
    WorkflowPackageCreate,
    WorkflowPackagePage,
    WorkflowPackageReceipt,
    WorkflowPackageView,
    WorkflowStepPage,
)
from platform_core.application.errors import RuleViolation
from platform_core.application.workflows import Workflows

from platform_api.adapters.http.control import Actor, Cursor, Key, Limit, PathId, after, page

router = APIRouter()
P = "/projects/{project_id}/workflow-packages"


def service(request: Request) -> Workflows:
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return Workflows(
        request.app.state.database,
        operator_catalog=request.app.state.operator_catalog,
    )


@router.post(P, status_code=202, response_model=WorkflowPackageReceipt)
def register(
    project_id: PathId, actor: Actor, key: Key, body: WorkflowPackageCreate, request: Request
):
    value = service(request).register(actor, project_id, key, body.model_dump(mode="json"))
    return {name: value[name] for name in ("package_id", "status", "status_url")}


@router.get(P, response_model=WorkflowPackagePage)
def packages(
    project_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = "workflow-packages/" + project_id
    return page(
        service(request).list(actor, project_id, after(cursor, scope), limit + 1),
        limit,
        "package_id",
        scope,
    )


@router.get(P + "/{package_id}", response_model=WorkflowPackageView)
def package(project_id: PathId, package_id: PathId, actor: Actor, request: Request):
    return service(request).get(actor, project_id, package_id)


@router.get(
    "/projects/{project_id}/executions/{execution_id}/steps", response_model=WorkflowStepPage
)
def steps(
    project_id: PathId,
    execution_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = "workflow-steps/" + project_id + "/" + execution_id
    return page(
        service(request).steps(actor, project_id, execution_id, after(cursor, scope), limit + 1),
        limit,
        "node_id",
        scope,
    )


internal = APIRouter(include_in_schema=False)


def require_execution(request):
    token = os.environ.get("V7_EXECUTION_PLATFORM_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + token
    ):
        raise RuleViolation("AUTHENTICATION_REQUIRED", "Service credential required", 401)


@internal.post("/internal/workflows/{action_id}/{execution_id}/nodes/{node_id}/permit")
def node_permit(
    action_id: PathId, execution_id: PathId, node_id: PathId, body: dict, request: Request
):
    require_execution(request)
    marker = os.environ.get("V7_MAINTENANCE_FILE")
    if marker and Path(marker).is_file():
        return {"decision": "WAIT", "reason": "DEPLOYMENT_PARKING"}
    return service(request).permit_node(action_id, execution_id, node_id, body)


@internal.post("/internal/workflows/{action_id}/{execution_id}/nodes/{node_id}/observations")
def node_observe(
    action_id: PathId, execution_id: PathId, node_id: PathId, body: dict, request: Request
):
    require_execution(request)
    # The action/execution binding is checked by permit before a slot exists;
    # observation updates are tied to the same action and immutable node ID.
    return {"recorded": service(request).observe_node(action_id, execution_id, node_id, body)}
