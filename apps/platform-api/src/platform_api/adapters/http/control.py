"""Authentication and HTTP conversion; all mutations use core application commands."""

import base64
import binascii
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from platform_contracts.agent import AgentAnswerCreate, AgentAnswerReceipt, AgentCurrentView
from platform_contracts.control import (
    BudgetView,
    ControlRequest,
    ExecutionSummaryPage,
    GrantCreate,
    GrantPage,
    GrantView,
    ModelUsageView,
    ProjectCreate,
    ProjectPage,
    ProjectView,
    Readiness,
    ReviseTask,
    TaskCreate,
    TaskPage,
    TaskView,
    WorkspaceSummaryView,
)
from platform_contracts.v1 import CommandReceipt, ErrorEnvelope, ExecutionRef, TaskRef
from platform_core.application.control import ControlService
from platform_core.application.errors import RuleViolation
from platform_core.application.ports import Identity
from starlette.responses import JSONResponse

router = APIRouter(
    responses={status: {"model": ErrorEnvelope} for status in [400, 401, 403, 404, 409, 422, 503]}
)
PREFIX = "/projects/{project_id}"
PathId = Annotated[str, Path(pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])"))]


def service(request: Request) -> ControlService:
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return ControlService(
        request.app.state.database,
        agent_runtime_profile=request.app.state.agent_runtime_profile,
        agent_model_catalog=request.app.state.agent_model_catalog,
        agent_tool_catalog=request.app.state.agent_tool_catalog,
        # The operator catalogue becomes part of an AgentRun only together
        # with a P7 v2 tool catalogue. This preserves already frozen P6 test
        # and recovery deployments; production composition requires both.
        operator_catalog=(
            request.app.state.operator_catalog
            if request.app.state.agent_tool_catalog is not None
            else None
        ),
    )


def identity(request: Request, authorization: Annotated[str | None, Header()] = None) -> Identity:
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 256:
        raise RuleViolation("AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)
    service(request)
    return request.app.state.database.authenticate(authorization[7:])


Actor = Annotated[Identity, Depends(identity)]
Application = Annotated[ControlService, Depends(service)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=256)]


def after(cursor: str | None, scope: str) -> str:
    if cursor is None:
        return ""
    try:
        decoded = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True).decode()
        saved_scope, identifier = decoded.rsplit(":", 1)
        if saved_scope != scope or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
            raise ValueError
        return identifier
    except (ValueError, UnicodeError, binascii.Error):
        raise RuleViolation("MALFORMED_REQUEST", "Invalid list cursor", 400) from None


def page(items: list[dict], limit: int, key: str, scope: str) -> dict:
    more = len(items) > limit
    items = items[:limit]
    cursor = (
        base64.urlsafe_b64encode(f"{scope}:{items[-1][key]}".encode()).decode() if more else None
    )
    return {"items": items, "next_cursor": cursor}


def receipt(record: dict, request: Request) -> CommandReceipt:
    error = None
    if record["error_code"]:
        error = ErrorEnvelope(
            code=record["error_code"],
            message=record["error_message"],
            request_id=request.state.request_id,
            retry_mode="NONE",
        )
    return CommandReceipt(
        command_id=record["command_id"],
        status=record["status"],
        action_id=record["action_id"],
        task_ref=TaskRef(task_id=record["task_id"], spec_revision=record["spec_revision"]),
        result=ExecutionRef(execution_id=record["execution_id"])
        if record["execution_id"]
        else None,
        error=error,
        status_url=f"/api/v1/projects/{record['project_id']}/commands/{record['command_id']}",
        request_id=request.state.request_id,
    )


@router.get("/ready", response_model=Readiness, responses={503: {"model": Readiness}})
def ready(request: Request):
    try:
        service(request)
        request.app.state.database.check_schema()
        from platform_core.application.catalog import validate_catalog

        validate_catalog(request.app.state.operator_catalog)
        if os.environ.get("V7_OPERATOR_CATALOG"):
            remote = request.app.state.execution.json("GET", "/readyz")
            if (
                remote.get("operator_catalog_digest")
                != request.app.state.operator_catalog["catalog_digest"]
            ):
                raise RuntimeError("Platform and Execution Operator Catalogs differ")
            if request.app.state.release_digest is not None and (
                remote.get("release_digest") != request.app.state.release_digest
            ):
                raise RuntimeError("Platform and Execution release locks differ")
        if not request.app.state.agent_configuration_complete:
            raise RuntimeError("Agent runtime and model catalogue must be configured together")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


@router.post("/projects", response_model=ProjectView, status_code=201)
def create_project(body: ProjectCreate, actor: Actor, app: Application, key: Key):
    return app.create_project(actor, key, body.name)


@router.get("/projects", response_model=ProjectPage)
def list_projects(actor: Actor, app: Application, limit: Limit = 50, cursor: Cursor = None):
    return page(
        app.projects(actor, after(cursor, "projects"), limit + 1), limit, "project_id", "projects"
    )


@router.get(PREFIX, response_model=ProjectView)
def get_project(project_id: PathId, actor: Actor, app: Application):
    return app.project(actor, project_id)


@router.get(PREFIX + "/workspace-summary", response_model=WorkspaceSummaryView)
def workspace_summary(project_id: PathId, actor: Actor, request: Request):
    value = service(request).workspace_summary(actor, project_id)
    if request.app.state.data is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Data service is not configured", 503)
    asset_count = 0
    asset_cursor = None
    seen_cursors = set()
    while True:
        asset_page = request.app.state.data.json(
            "GET",
            f"/internal/projects/{project_id}/assets",
            headers={"X-Actor-ID": actor.principal_id},
            params={"limit": 100, **({"after": asset_cursor} if asset_cursor else {})},
        )
        assets = asset_page["items"]
        asset_count += len(assets)
        next_cursor = asset_page.get("next_cursor")
        if next_cursor is None:
            break
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor == asset_cursor
            or next_cursor in seen_cursors
        ):
            raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Data asset paging did not advance", 503)
        seen_cursors.add(next_cursor)
        asset_cursor = next_cursor
    authoring = value.pop("authoring")
    return value | {
        "phase": "P8.2",
        "release_digest": request.app.state.release_digest,
        "resources": {
            "assets": asset_count,
            "operator_versions": len(request.app.state.authoring_descriptor["operators"]),
            "workflow_definitions": authoring["workflow_definitions"],
            "workflow_releases": authoring["workflow_releases"],
            "operator_materials": authoring["operator_proposals"],
        },
        "tasks": {
            state: value["tasks"].get(state, 0)
            for state in ("OPEN", "PAUSED", "CANCELLED", "COMPLETED")
        },
        "executions": {
            state: value["executions"].get(state, 0)
            for state in (
                "RECORDED",
                "DISPATCHING",
                "RECONCILING",
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "SEALED",
                "REJECTED",
            )
        },
    }


@router.get(PREFIX + "/executions", response_model=ExecutionSummaryPage)
def executions(
    project_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    after_cursor: Annotated[str | None, Query(alias="after", max_length=256)] = None,
    task_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")] = None,
    state: Annotated[
        str | None,
        Query(
            pattern=(
                r"^(RECORDED|DISPATCHING|RECONCILING|QUEUED|RUNNING|SUCCEEDED|FAILED|"
                r"CANCELLED|SEALED|REJECTED)$"
            )
        ),
    ] = None,
):
    scope = f"executions/{project_id}/{task_id or '*'}/{state or '*'}"
    return page(
        service(request).execution_summaries(
            actor,
            project_id,
            after(after_cursor, scope),
            limit + 1,
            task_id=task_id,
            state=state,
        ),
        limit,
        "action_id",
        scope,
    )


@router.post(PREFIX + "/tasks", response_model=TaskView, status_code=201)
def create_task(project_id: PathId, body: TaskCreate, actor: Actor, app: Application, key: Key):
    # Optional fields added after P2 must not change the digest of a replayed
    # historical request merely by materializing absent values as null.
    return app.create_task(
        actor,
        project_id,
        key,
        body.model_dump(mode="json", exclude_unset=True),
    )


@router.get(PREFIX + "/tasks", response_model=TaskPage)
def list_tasks(
    project_id: PathId, actor: Actor, app: Application, limit: Limit = 50, cursor: Cursor = None
):
    scope = "tasks/" + project_id
    return page(
        app.tasks(actor, project_id, after(cursor, scope), limit + 1), limit, "task_id", scope
    )


@router.get(PREFIX + "/tasks/{task_id}", response_model=TaskView)
def get_task(project_id: PathId, task_id: PathId, actor: Actor, app: Application):
    return app.task(actor, project_id, task_id)


@router.post(PREFIX + "/tasks/{task_id}/spec-revisions", response_model=TaskView, status_code=201)
def revise_task(
    project_id: PathId,
    task_id: PathId,
    body: ReviseTask,
    actor: Actor,
    app: Application,
    key: Key,
):
    return app.revise(
        actor,
        project_id,
        task_id,
        key,
        body.model_dump(mode="json", exclude_unset=True),
    )


def control_endpoint(operation: str):
    def endpoint(
        project_id: PathId,
        task_id: PathId,
        body: ControlRequest,
        request: Request,
        actor: Actor,
        app: Application,
        key: Key,
    ):
        return receipt(
            app.control(actor, project_id, task_id, operation, key, body.expected_control_version),
            request,
        )

    endpoint.__name__ = operation + "_task"
    return endpoint


for operation in ["pause", "resume", "cancel"]:
    router.add_api_route(
        PREFIX + "/tasks/{task_id}/" + operation,
        control_endpoint(operation),
        methods=["POST"],
        response_model=CommandReceipt,
    )


@router.get(PREFIX + "/tasks/{task_id}/budget", response_model=BudgetView)
def get_budget(project_id: PathId, task_id: PathId, actor: Actor, app: Application):
    return app.budget(actor, project_id, task_id)


@router.get(PREFIX + "/tasks/{task_id}/model-usage", response_model=ModelUsageView)
def get_model_usage(project_id: PathId, task_id: PathId, actor: Actor, request: Request):
    from platform_core.application.model_calls import AgentModelCoordinator

    return AgentModelCoordinator(request.app.state.database).usage(actor, project_id, task_id)


@router.get(PREFIX + "/tasks/{task_id}/agent", response_model=AgentCurrentView)
def get_agent(project_id: PathId, task_id: PathId, actor: Actor, request: Request):
    from platform_core.application.agent_kernel import AgentCoordinator

    return AgentCoordinator(request.app.state.database).task_view(actor, project_id, task_id)


@router.get(PREFIX + "/tasks/{task_id}/agent/events")
def get_agent_events(
    project_id: PathId,
    task_id: PathId,
    actor: Actor,
    request: Request,
    after: int = -1,
):
    """Return one resumable SSE projection; clients reconnect with the last id."""
    import json

    from platform_core.application.agent_kernel import AgentCoordinator
    from starlette.responses import StreamingResponse

    view = AgentCoordinator(request.app.state.database).task_view(actor, project_id, task_id)
    revision = view["wake_revision"]
    payload = view if revision > after else {"heartbeat": True, "wake_revision": revision}

    def stream():
        yield f"id: {revision}\nevent: agent\ndata: "
        yield json.dumps(payload, ensure_ascii=False, default=str)
        yield "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    PREFIX + "/tasks/{task_id}/questions/{question_id}/answers",
    response_model=AgentAnswerReceipt,
    status_code=201,
)
def answer_agent_question(
    project_id: PathId,
    task_id: PathId,
    question_id: PathId,
    body: AgentAnswerCreate,
    actor: Actor,
    request: Request,
    key: Key,
):
    from platform_core.application.agent_kernel import AgentCoordinator

    coordinator = AgentCoordinator(request.app.state.database)
    with request.app.state.database.transaction() as uow:
        uow.access.authorize(actor, project_id, "EDITOR")
        uow.tasks.get(project_id, task_id)
        run = uow.agents.run_by_task(task_id, required=True)
    result = coordinator.answer_question(
        actor,
        project_id,
        run["agent_run_id"],
        question_id,
        key,
        body.expected_spec_revision,
        body.answer,
    )
    return {
        key: result[key]
        for key in (
            "answer_id",
            "question_id",
            "agent_run_id",
            "task_spec_revision",
            "answer_digest",
            "answered_at",
        )
    }


@router.get(PREFIX + "/grants", response_model=GrantPage)
def list_grants(
    project_id: PathId, actor: Actor, app: Application, limit: Limit = 50, cursor: Cursor = None
):
    scope = "grants/" + project_id
    return page(
        app.grants(actor, project_id, after(cursor, scope), limit + 1), limit, "grant_id", scope
    )


@router.post(PREFIX + "/grants", response_model=GrantView, status_code=201)
def create_grant(project_id: PathId, body: GrantCreate, actor: Actor, app: Application, key: Key):
    return app.grant(
        actor,
        project_id,
        key,
        body.model_dump(mode="json", exclude_unset=True),
    )


@router.post(PREFIX + "/grants/{grant_id}/revoke", response_model=GrantView)
def revoke_grant(project_id: PathId, grant_id: PathId, actor: Actor, app: Application, key: Key):
    return app.revoke_grant(actor, project_id, grant_id, key)


@router.get(PREFIX + "/commands/{command_id}", response_model=CommandReceipt)
def get_command(
    project_id: PathId, command_id: PathId, request: Request, actor: Actor, app: Application
):
    return receipt(app.command(actor, project_id, command_id), request)
