"""Environment mapping for Platform infrastructure executables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_contracts import DeploymentContext, ServiceEndpoint
from platform_core.application.catalog import load_catalog as load_operator_catalog


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _deployment(values: Mapping[str, str]) -> DeploymentContext:
    return DeploymentContext(
        deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
        deployment_id=values.get("V7_DEPLOYMENT_ID"),
        deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
    )


def _endpoint(values: Mapping[str, str], url_name: str, token_name: str) -> ServiceEndpoint | None:
    url = values.get(url_name)
    if url is None:
        return None
    return ServiceEndpoint(url, _required(values, token_name))


@dataclass(frozen=True)
class PlatformWorkerComposition:
    database_url: str
    deployment: DeploymentContext
    execution: ServiceEndpoint | None
    knowledge: ServiceEndpoint | None
    cwl_image: str | None
    operator_catalog: Mapping[str, object] | None
    heartbeat_file: Path | None = None

    @classmethod
    def from_environment(
        cls,
        values: Mapping[str, str] | None = None,
        *,
        lane: str = "package",
    ) -> PlatformWorkerComposition:
        values = os.environ if values is None else values
        if lane not in {"package", "action", "acceptance", "knowledge"}:
            raise ValueError(f"Unknown Platform worker lane: {lane}")
        operator_catalog = None
        if lane == "package":
            operator_path = Path(_required(values, "V7_OPERATOR_CATALOG"))
            operator_catalog = load_operator_catalog(operator_path)
        return cls(
            database_url=_required(values, "V7_DATABASE_URL"),
            deployment=_deployment(values),
            execution=_endpoint(values, "V7_EXECUTION_URL", "V7_PLATFORM_EXECUTION_TOKEN"),
            knowledge=_endpoint(values, "V7_KNOWLEDGE_URL", "V7_PLATFORM_KNOWLEDGE_TOKEN"),
            cwl_image=values.get("V7_CWL_IMAGE"),
            operator_catalog=operator_catalog,
            heartbeat_file=(
                Path(values["V7_WORKER_HEARTBEAT_FILE"])
                if values.get("V7_WORKER_HEARTBEAT_FILE")
                else None
            ),
        )


@dataclass(frozen=True)
class PlatformAdminComposition:
    database_url: str
    owner_token: str
    operator_catalog_path: Path | None
    deployment: DeploymentContext
    deployment_source_digest: str | None

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> PlatformAdminComposition:
        values = os.environ if values is None else values
        operator_path = values.get("V7_OPERATOR_CATALOG")
        return cls(
            database_url=_required(values, "V7_DATABASE_URL"),
            owner_token=values.get("V7_OWNER_TOKEN", ""),
            operator_catalog_path=Path(operator_path) if operator_path else None,
            deployment=_deployment(values),
            deployment_source_digest=values.get("V7_DEPLOYMENT_SOURCE_DIGEST"),
        )
