"""Execution transport adapter for the shared immutable engine-package contract."""

from platform_contracts.engine_package import (
    EnginePackageViolation,
    validate_engine_package,
)
from platform_contracts.service import ServiceFault


def validate(body):
    """Validate the required flat engine package in an Execution request."""

    package = body.get("engine_package")
    if package is None:
        raise ServiceFault("INVALID_BINDING", "Workflow engine package is required", 422)
    try:
        return validate_engine_package(
            package,
            body.get("compiled", {}),
            body.get("execution_plan", {}),
        )
    except EnginePackageViolation as error:
        raise ServiceFault(error.code, error.message, error.status) from error
