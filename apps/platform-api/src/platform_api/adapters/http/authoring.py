"""Capability Studio draft, preview, and validation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from platform_contracts.authoring import (
    AuthoringDescriptor,
    AuthoringPreviewCreate,
    AuthoringPreviewView,
    AuthoringSourceImportCreate,
    AuthoringSourceImportView,
    AuthoringValidationCreate,
    AuthoringValidationReceipt,
    AuthoringValidationView,
    WorkflowCandidateCreate,
    WorkflowCandidatePage,
    WorkflowCandidateReceipt,
    WorkflowCandidateView,
    WorkflowDefinitionCreate,
    WorkflowDefinitionPage,
    WorkflowDefinitionView,
    WorkflowPackageDraftSource,
    WorkflowReleaseCreate,
    WorkflowReleaseExecutionCreate,
    WorkflowReleaseLifecycleCreate,
    WorkflowReleaseLifecyclePage,
    WorkflowReleaseLifecycleView,
    WorkflowReleasePage,
    WorkflowReleaseView,
    WorkflowRevisionCreate,
    WorkflowRevisionPage,
    WorkflowRevisionView,
    WorkflowTrialCreate,
    WorkflowTrialPage,
    WorkflowTrialReceipt,
    WorkflowTrialView,
)
from platform_contracts.operator_proposals import (
    OperatorProposalCreate,
    OperatorProposalPage,
    OperatorProposalRevisionCreate,
    OperatorProposalRevisionView,
    OperatorProposalSubmit,
    OperatorProposalView,
)
from platform_contracts.v1 import CommandReceipt
from platform_core.application.authoring import AuthoringService
from platform_core.application.authoring_release import AuthoringReleaseService
from platform_core.application.errors import RuleViolation
from platform_core.application.operator_proposals import OperatorProposalService

from platform_api.adapters.http.authoring_import import parse_source
from platform_api.adapters.http.control import (
    Actor,
    Cursor,
    Key,
    Limit,
    PathId,
    after,
    page,
    receipt,
)

router = APIRouter()
P = "/projects/{project_id}"


@router.get(P + "/workflow-releases", response_model=WorkflowReleasePage)
def project_releases(
    project_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = f"workflow-release-library/{project_id}"
    return page(
        release_service(request).project_releases(
            actor, project_id, after(cursor, scope), limit + 1
        ),
        limit,
        "release_id",
        scope,
    )


@router.post(P + "/authoring/imports", response_model=AuthoringSourceImportView)
def import_authoring_source(
    project_id: PathId,
    body: AuthoringSourceImportCreate,
    actor: Actor,
    request: Request,
):
    parsed = parse_source(body.content, body.format)
    try:
        source = WorkflowPackageDraftSource.model_validate(parsed).model_dump(
            mode="json", by_alias=True
        )
    except ValueError as problem:
        raise RuleViolation(
            "MALFORMED_REQUEST",
            "Imported workflow does not match the bounded package envelope",
            422,
            "SAME_KEY",
        ) from problem
    return service(request).import_source(actor, project_id, source, body.format)


def service(request: Request) -> AuthoringService:
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return AuthoringService(
        request.app.state.database,
        request.app.state.data,
        descriptor=request.app.state.authoring_descriptor,
        parameter_validator=request.app.state.authoring_parameter_validator,
        environment=request.app.state.authoring_environment,
    )


def release_service(request: Request) -> AuthoringReleaseService:
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return AuthoringReleaseService(
        request.app.state.database,
        request.app.state.data,
        request.app.state.execution,
        descriptor=request.app.state.authoring_descriptor,
        environment=request.app.state.authoring_environment,
        operator_catalog=request.app.state.operator_catalog,
    )


def proposal_service(request: Request) -> OperatorProposalService:
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return OperatorProposalService(
        request.app.state.database,
        operator_catalog=request.app.state.operator_catalog,
        release_lock=request.app.state.release_lock,
    )


@router.post(P + "/operator-proposals", response_model=OperatorProposalView, status_code=201)
def create_operator_proposal(
    project_id: PathId,
    body: OperatorProposalCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return proposal_service(request).create(actor, project_id, key, body.model_dump(mode="json"))


@router.get(P + "/operator-proposals", response_model=OperatorProposalPage)
def operator_proposals(
    project_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = f"operator-proposals/{project_id}"
    return page(
        proposal_service(request).proposals(actor, project_id, after(cursor, scope), limit + 1),
        limit,
        "proposal_id",
        scope,
    )


@router.get(P + "/operator-proposals/{proposal_id}", response_model=OperatorProposalView)
def operator_proposal(
    project_id: PathId,
    proposal_id: PathId,
    actor: Actor,
    request: Request,
):
    return proposal_service(request).proposal(actor, project_id, proposal_id)


@router.post(
    P + "/operator-proposals/{proposal_id}/revisions",
    response_model=OperatorProposalRevisionView,
    status_code=201,
)
def revise_operator_proposal(
    project_id: PathId,
    proposal_id: PathId,
    body: OperatorProposalRevisionCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return proposal_service(request).append(
        actor, project_id, proposal_id, key, body.model_dump(mode="json")
    )


@router.post(
    P + "/operator-proposals/{proposal_id}/submit",
    response_model=OperatorProposalView,
)
def submit_operator_proposal(
    project_id: PathId,
    proposal_id: PathId,
    body: OperatorProposalSubmit,
    actor: Actor,
    key: Key,
    request: Request,
):
    return proposal_service(request).submit(
        actor, project_id, proposal_id, key, body.model_dump(mode="json")
    )


@router.get(P + "/authoring/operator-descriptors", response_model=AuthoringDescriptor)
def descriptors(project_id: PathId, actor: Actor, request: Request):
    service(request).authorize(actor, project_id)
    return request.app.state.authoring_descriptor


@router.post(P + "/authoring/previews", response_model=AuthoringPreviewView)
def preview(
    project_id: PathId,
    body: AuthoringPreviewCreate,
    actor: Actor,
    request: Request,
):
    return service(request).preview(
        actor,
        project_id,
        body.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


@router.post(P + "/workflow-definitions", response_model=WorkflowDefinitionView, status_code=201)
def create_definition(
    project_id: PathId,
    body: WorkflowDefinitionCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return service(request).create(
        actor, project_id, key, body.model_dump(mode="json", by_alias=True)
    )


@router.get(P + "/workflow-definitions", response_model=WorkflowDefinitionPage)
def definitions(
    project_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = "workflow-definitions/" + project_id
    return page(
        service(request).definitions(actor, project_id, after(cursor, scope), limit + 1),
        limit,
        "definition_id",
        scope,
    )


@router.get(P + "/workflow-definitions/{definition_id}", response_model=WorkflowDefinitionView)
def definition(project_id: PathId, definition_id: PathId, actor: Actor, request: Request):
    return service(request).definition(actor, project_id, definition_id)


@router.post(
    P + "/workflow-definitions/{definition_id}/revisions",
    response_model=WorkflowRevisionView,
    status_code=201,
)
def append_revision(
    project_id: PathId,
    definition_id: PathId,
    body: WorkflowRevisionCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return service(request).append(
        actor,
        project_id,
        definition_id,
        key,
        body.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


@router.post(
    P + "/workflow-definitions/{definition_id}/validations",
    response_model=AuthoringValidationReceipt,
    status_code=202,
)
def request_validation(
    project_id: PathId,
    definition_id: PathId,
    body: AuthoringValidationCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return service(request).request_validation(
        actor,
        project_id,
        definition_id,
        key,
        body.model_dump(mode="json"),
    )


@router.get(
    P + "/authoring/validations/{validation_id}",
    response_model=AuthoringValidationView,
)
def validation(
    project_id: PathId,
    validation_id: PathId,
    actor: Actor,
    request: Request,
):
    return service(request).validation(actor, project_id, validation_id)


@router.get(
    P + "/workflow-definitions/{definition_id}/revisions",
    response_model=WorkflowRevisionPage,
)
def revisions(
    project_id: PathId,
    definition_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
):
    items = service(request).revisions(actor, project_id, definition_id, cursor or 0, limit + 1)
    more = len(items) > limit
    items = items[:limit]
    return {
        "items": items,
        "next_cursor": str(items[-1]["revision"]) if more else None,
    }


@router.get(
    P + "/workflow-definitions/{definition_id}/revisions/{revision}",
    response_model=WorkflowRevisionView,
)
def revision(
    project_id: PathId,
    definition_id: PathId,
    revision: Annotated[int, Path(ge=1)],
    actor: Actor,
    request: Request,
):
    return service(request).revision(actor, project_id, definition_id, revision)


@router.post(
    P + "/workflow-definitions/{definition_id}/candidates",
    response_model=WorkflowCandidateReceipt,
    status_code=202,
)
def create_candidate(
    project_id: PathId,
    definition_id: PathId,
    body: WorkflowCandidateCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return release_service(request).create_candidate(
        actor, project_id, definition_id, key, body.model_dump(mode="json")
    )


@router.get(
    P + "/workflow-definitions/{definition_id}/candidates",
    response_model=WorkflowCandidatePage,
)
def candidates(
    project_id: PathId,
    definition_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = f"workflow-candidates/{project_id}/{definition_id}"
    return page(
        release_service(request).candidates(
            actor, project_id, definition_id, after(cursor, scope), limit + 1
        ),
        limit,
        "candidate_id",
        scope,
    )


@router.get(
    P + "/workflow-definitions/{definition_id}/candidates/{candidate_id}",
    response_model=WorkflowCandidateView,
)
def candidate(
    project_id: PathId,
    definition_id: PathId,
    candidate_id: PathId,
    actor: Actor,
    request: Request,
):
    return release_service(request).candidate(actor, project_id, definition_id, candidate_id)


@router.post(
    P + "/workflow-definitions/{definition_id}/trial-runs",
    response_model=WorkflowTrialReceipt,
    status_code=202,
)
def create_trial(
    project_id: PathId,
    definition_id: PathId,
    body: WorkflowTrialCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    result = release_service(request).create_trial(
        actor,
        project_id,
        definition_id,
        key,
        body.model_dump(mode="json", exclude_none=True),
    )
    return result | {"command": receipt(result["command"], request)}


@router.get(
    P + "/workflow-definitions/{definition_id}/trial-runs",
    response_model=WorkflowTrialPage,
)
def trials(
    project_id: PathId,
    definition_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    scope = f"workflow-trials/{project_id}/{definition_id}"
    return page(
        release_service(request).trials(
            actor, project_id, definition_id, after(cursor, scope), limit + 1
        ),
        limit,
        "trial_id",
        scope,
    )


@router.get(
    P + "/workflow-definitions/{definition_id}/trial-runs/{trial_id}",
    response_model=WorkflowTrialView,
)
def trial(
    project_id: PathId,
    definition_id: PathId,
    trial_id: PathId,
    actor: Actor,
    request: Request,
):
    return release_service(request).trial(actor, project_id, definition_id, trial_id)


@router.post(
    P + "/workflow-definitions/{definition_id}/releases",
    response_model=WorkflowReleaseView,
    status_code=201,
)
def create_release(
    project_id: PathId,
    definition_id: PathId,
    body: WorkflowReleaseCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return release_service(request).create_release(
        actor, project_id, definition_id, key, body.model_dump(mode="json")
    )


@router.get(
    P + "/workflow-definitions/{definition_id}/releases",
    response_model=WorkflowReleasePage,
)
def releases(
    project_id: PathId,
    definition_id: PathId,
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    values = release_service(request).releases(
        actor, project_id, definition_id, cursor or "", limit + 1
    )
    more = len(values) > limit
    values = values[:limit]
    return {
        "items": values,
        "next_cursor": values[-1]["version_label"] if more else None,
    }


@router.get(
    P + "/workflow-definitions/{definition_id}/releases/{version_label}",
    response_model=WorkflowReleaseView,
)
def release(
    project_id: PathId,
    definition_id: PathId,
    version_label: Annotated[str, Path(min_length=5, max_length=64)],
    actor: Actor,
    request: Request,
):
    return release_service(request).release(actor, project_id, definition_id, version_label)


@router.get(
    P + "/workflow-definitions/{definition_id}/releases/{version_label}/lifecycle-revisions",
    response_model=WorkflowReleaseLifecyclePage,
)
def release_lifecycle(
    project_id: PathId,
    definition_id: PathId,
    version_label: Annotated[str, Path(min_length=5, max_length=64)],
    actor: Actor,
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
):
    try:
        after_revision = int(cursor or "0")
    except ValueError as problem:
        raise RuleViolation("MALFORMED_REQUEST", "Invalid lifecycle cursor", 400) from problem
    values = release_service(request).release_lifecycle(
        actor, project_id, definition_id, version_label, after_revision, limit + 1
    )
    more = len(values) > limit
    values = values[:limit]
    return {
        "items": values,
        "next_cursor": str(values[-1]["lifecycle_revision"]) if more else None,
    }


@router.post(
    P + "/workflow-definitions/{definition_id}/releases/{version_label}/lifecycle-revisions",
    response_model=WorkflowReleaseLifecycleView,
    status_code=201,
)
def transition_release(
    project_id: PathId,
    definition_id: PathId,
    version_label: Annotated[str, Path(min_length=5, max_length=64)],
    body: WorkflowReleaseLifecycleCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    return release_service(request).transition_release(
        actor,
        project_id,
        definition_id,
        version_label,
        key,
        body.model_dump(mode="json"),
    )


@router.post(
    P + "/workflow-definitions/{definition_id}/releases/{version_label}/executions",
    response_model=CommandReceipt,
    status_code=202,
)
def execute_release(
    project_id: PathId,
    definition_id: PathId,
    version_label: Annotated[str, Path(min_length=5, max_length=64)],
    body: WorkflowReleaseExecutionCreate,
    actor: Actor,
    key: Key,
    request: Request,
):
    command = release_service(request).execute_release(
        actor,
        project_id,
        definition_id,
        version_label,
        key,
        body.model_dump(mode="json", exclude_none=True),
    )
    return receipt(command, request)
