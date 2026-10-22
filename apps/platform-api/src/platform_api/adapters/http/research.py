"""P5.2 research gateway; exact source facts are acquired before write transactions."""

from fastapi import APIRouter, Request
from platform_contracts.research import (
    AnalysisRegistrationView,
    AnalysisRevisionCreate,
    AnalysisRevisionView,
    ClaimRevisionCreate,
    ClaimRevisionView,
    RegisterAnalysis,
    ResearchView,
)
from platform_core.application.errors import RuleViolation
from platform_core.application.research import Research

from platform_api.adapters.http.control import Actor, Key, PathId

router = APIRouter()
P = "/projects/{project_id}/tasks/{task_id}"


def application(request):
    if request.app.state.database is None:
        raise RuleViolation("DEPENDENCY_UNAVAILABLE", "Database is not configured", 503, "SAME_KEY")
    return Research(request.app.state.database)


def resolve_source(request, actor, project, source):
    kind = source["kind"]
    if kind == "ASSET":
        ref = source["ref"]
        if ref["project_id"] != project:
            raise RuleViolation("SCOPE_DENIED", "Cross-project source denied", 403)
        if request.app.state.data is None:
            raise RuleViolation(
                "DEPENDENCY_UNAVAILABLE", "Data service is unavailable", 503, "SAME_KEY"
            )
        view = request.app.state.data.json(
            "GET",
            f"/internal/projects/{project}/assets/{ref['asset_id']}/versions/{ref['version_id']}",
        )
        if view.get("ref") != ref:
            raise RuleViolation("INVALID_BINDING", "Asset source identity differs", 409)
        return {"verified": True}
    if kind == "EVIDENCE":
        ref = source["ref"]
        if ref["project_id"] != project:
            raise RuleViolation("SCOPE_DENIED", "Cross-project source denied", 403)
        if request.app.state.knowledge is None:
            raise RuleViolation(
                "DEPENDENCY_UNAVAILABLE", "Knowledge service is unavailable", 503, "SAME_KEY"
            )
        evidence = request.app.state.knowledge.json(
            "GET",
            f"/internal/projects/{project}/knowledge/documents/{ref['document_id']}"
            f"/versions/{ref['version_id']}/units/{ref['unit_id']}",
            headers={"X-Actor-ID": actor.principal_id},
        )
        if evidence.get("evidence_ref") != ref:
            raise RuleViolation("INVALID_BINDING", "Evidence source identity differs", 409)
        if evidence.get("availability") != "ACTIVE":
            raise RuleViolation(
                "PRECONDITION_UNRESOLVED",
                "Withdrawn evidence cannot be added to a new research revision",
                409,
            )
        return {
            "verified": True,
            "read_receipt_id": evidence["read_receipt_id"],
            "lifecycle_revision": evidence["lifecycle_revision"],
            "availability": evidence["availability"],
            "epistemic_status": evidence["epistemic_status"],
            "review_state": evidence["review_state"],
        }
    if kind == "METHOD":
        return {"verified": True}
    if kind == "EXECUTION":
        return None
    raise RuleViolation("INVALID_BINDING", "Unsupported research source", 409)


def resolve_analysis_sources(request, actor, project, body):
    sources = [item.source.model_dump(mode="json") for item in body.input_roles]
    sources.extend({"kind": "METHOD", "ref": item.model_dump(mode="json")} for item in body.methods)
    sources.extend(item.model_dump(mode="json") for item in body.observed_sources)
    if body.groundwater_comparison:
        for series in body.groundwater_comparison.series:
            for conversion in (series.elevation_conversion, series.datum_conversion):
                if conversion:
                    sources.append(conversion.source.model_dump(mode="json"))
    return [resolve_source(request, actor, project, source) for source in sources]


@router.post(P + "/analysis-revisions", response_model=AnalysisRevisionView, status_code=201)
def create_analysis(
    project_id: PathId,
    task_id: PathId,
    body: AnalysisRevisionCreate,
    request: Request,
    actor: Actor,
    key: Key,
):
    app = application(request)
    app.authorize(actor, project_id, "EDITOR")
    resolved = resolve_analysis_sources(request, actor, project_id, body)
    return app.create_analysis(
        actor, project_id, task_id, key, body.model_dump(mode="json"), resolved
    )


@router.post(
    P + "/analysis-revisions/{revision_id}/register",
    response_model=AnalysisRegistrationView,
    status_code=201,
)
def register_analysis(
    project_id: PathId,
    task_id: PathId,
    revision_id: PathId,
    body: RegisterAnalysis,
    request: Request,
    actor: Actor,
    key: Key,
):
    return application(request).register(
        actor, project_id, task_id, revision_id, key, body.model_dump(mode="json")
    )


@router.post(P + "/claims", response_model=ClaimRevisionView, status_code=201)
def create_claim(
    project_id: PathId,
    task_id: PathId,
    body: ClaimRevisionCreate,
    request: Request,
    actor: Actor,
    key: Key,
):
    app = application(request)
    app.authorize(actor, project_id, "EDITOR")
    sources = [item.source.model_dump(mode="json") for item in body.sources]
    resolved = [resolve_source(request, actor, project_id, source) for source in sources]
    return app.create_claim(actor, project_id, task_id, key, body.model_dump(mode="json"), resolved)


@router.get(P + "/research", response_model=ResearchView)
def get_research(
    project_id: PathId,
    task_id: PathId,
    request: Request,
    actor: Actor,
):
    return application(request).view(actor, project_id, task_id)
