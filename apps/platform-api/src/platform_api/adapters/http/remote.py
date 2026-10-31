"""Sanitized discovery of immutable remote-service binding revisions."""

from fastapi import APIRouter, Request
from platform_contracts.remote import ServiceBindingList, ServiceBindingView
from platform_core.application.science import Science

from platform_api.adapters.http.control import Actor, PathId

router = APIRouter()
P = "/projects/{project_id}/service-bindings"


def view(row):
    from urllib.parse import urlsplit

    parsed = urlsplit(row["endpoint"])
    origin = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    return {
        "service_binding_id": row["service_binding_id"],
        "revision": row["revision"],
        "protocol": row["protocol"],
        "enabled": row["enabled"],
        "endpoint_origin": origin,
        "tls_ca_ref": row.get("tls_ca_ref"),
        "targets": row["targets"],
        "limits": row["limits"],
        "binding_digest": row["binding_digest"],
    }


@router.get(P, response_model=ServiceBindingList)
def bindings(project_id: PathId, actor: Actor, request: Request):
    Science(
        request.app.state.database,
        request.app.state.data,
        request.app.state.execution,
        operator_catalog=request.app.state.operator_catalog,
        checker_catalog=request.app.state.checker_catalog,
    ).authorize(actor, project_id)
    with request.app.state.database.transaction() as uow:
        return {"items": [view(row) for row in uow.service_bindings.list(project_id)]}


@router.get(P + "/{service_binding_id}/revisions/{revision}", response_model=ServiceBindingView)
def binding(
    project_id: PathId, service_binding_id: PathId, revision: int, actor: Actor, request: Request
):
    Science(
        request.app.state.database,
        request.app.state.data,
        request.app.state.execution,
        operator_catalog=request.app.state.operator_catalog,
        checker_catalog=request.app.state.checker_catalog,
    ).authorize(actor, project_id)
    with request.app.state.database.transaction() as uow:
        return view(uow.service_bindings.get(project_id, service_binding_id, revision))
