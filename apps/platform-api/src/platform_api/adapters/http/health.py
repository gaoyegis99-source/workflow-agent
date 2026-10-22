from fastapi import APIRouter
from platform_contracts import CONTRACT_VERSION
from platform_contracts.v1 import ErrorEnvelope, HealthResponse
from platform_core.application.health import read_process_health

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={500: {"model": ErrorEnvelope}},
    summary="Process liveness; use /ready for database readiness",
)
def health() -> HealthResponse:
    state = read_process_health()
    return HealthResponse(
        status=state.status,
        service="platform-api",
        phase="P8.2",
        contract_version=CONTRACT_VERSION,
    )
