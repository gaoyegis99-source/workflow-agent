"""Public wire contracts; these types do not implement business operations."""

from .release import ReleaseContext
from .runtime_context import (
    CatalogContext,
    ContainerIdentity,
    DeploymentContext,
    ExecutionRuntimeConfig,
    ServiceEndpoint,
)

CONTRACT_VERSION = "1"

__all__ = (
    "CONTRACT_VERSION",
    "CatalogContext",
    "ContainerIdentity",
    "DeploymentContext",
    "ExecutionRuntimeConfig",
    "ReleaseContext",
    "ServiceEndpoint",
)
