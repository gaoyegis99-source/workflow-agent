"""Agent worker composition root; it owns V7 environment mapping."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from platform_contracts import CatalogContext, DeploymentContext, ServiceEndpoint
from platform_core.application.catalog import load_catalog as load_operator_catalog
from platform_core.application.checker_catalog import load_catalog as load_checker_catalog
from platform_core.application.model_catalog import load_catalog, selected_profile


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def deployment_context(values: Mapping[str, str]) -> DeploymentContext:
    return DeploymentContext(
        deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
        deployment_id=values.get("V7_DEPLOYMENT_ID"),
        deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
    )


@dataclass(frozen=True)
class AgentWorkerComposition:
    database_url: str
    checkpoint_database_url: str
    runtime_profile_path: Path
    model_catalog_path: Path
    model_catalog: Mapping[str, object]
    model_profile: Mapping[str, object]
    model_provider_secret: str | None = field(repr=False)
    tool_catalog_path: Path
    embedding_catalog_path: Path | None
    catalog: CatalogContext
    operator_catalog: Mapping[str, object]
    checker_catalog: Mapping[str, object]
    deployment: DeploymentContext
    data: ServiceEndpoint
    knowledge: ServiceEndpoint
    execution: ServiceEndpoint
    worker_id: str | None
    telemetry_endpoint: str | None
    heartbeat_file: Path | None = None

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> AgentWorkerComposition:
        values = os.environ if values is None else values
        deployment = deployment_context(values)
        images = {
            key: value
            for key, value in {
                "checker": values.get("V7_CHECKER_IMAGE"),
                "gdal_checker": values.get("V7_GDAL_CHECKER_IMAGE"),
            }.items()
            if value is not None
        }
        operator_path = Path(_required(values, "V7_OPERATOR_CATALOG"))
        checker_path = Path(_required(values, "V7_CHECKER_CATALOG"))
        catalog = CatalogContext(
            operator_catalog_path=operator_path,
            checker_catalog_path=checker_path,
            images=images,
        )
        checker_images = {
            "CHECKER": catalog.image("checker"),
            "GDAL_CHECKER": catalog.image("gdal_checker"),
        }
        if any(value is None for value in checker_images.values()):
            raise RuntimeError("V7_CHECKER_CATALOG_IMAGES_REQUIRED")
        model_catalog_path = Path(_required(values, "V7_MODEL_CATALOG"))
        model_catalog = load_catalog(model_catalog_path)
        model_profile = selected_profile(model_catalog)
        credential_name = model_profile.get("credential_env")
        if not isinstance(credential_name, str) or not credential_name:
            raise RuntimeError("Model profile credential binding is invalid")
        model_provider_secret = (
            _required(values, credential_name)
            if model_profile.get("provider") == "OPENAI_RESPONSES"
            else None
        )
        return cls(
            database_url=_required(values, "V7_DATABASE_URL"),
            checkpoint_database_url=_required(values, "V7_AGENT_CHECKPOINT_DATABASE_URL"),
            runtime_profile_path=Path(_required(values, "V7_AGENT_RUNTIME_PROFILE")),
            model_catalog_path=model_catalog_path,
            model_catalog=model_catalog,
            model_profile=model_profile,
            model_provider_secret=model_provider_secret,
            tool_catalog_path=Path(_required(values, "V7_AGENT_TOOL_CATALOG")),
            embedding_catalog_path=(
                Path(values["V7_EMBEDDING_CATALOG"]) if values.get("V7_EMBEDDING_CATALOG") else None
            ),
            catalog=catalog,
            operator_catalog=load_operator_catalog(operator_path),
            checker_catalog=load_checker_catalog(checker_path, checker_images),
            deployment=deployment,
            data=ServiceEndpoint(
                _required(values, "V7_DATA_URL"), _required(values, "V7_PLATFORM_DATA_TOKEN")
            ),
            knowledge=ServiceEndpoint(
                _required(values, "V7_KNOWLEDGE_URL"),
                _required(values, "V7_PLATFORM_KNOWLEDGE_TOKEN"),
            ),
            execution=ServiceEndpoint(
                _required(values, "V7_EXECUTION_URL"),
                _required(values, "V7_PLATFORM_EXECUTION_TOKEN"),
            ),
            worker_id=values.get("V7_AGENT_WORKER_ID"),
            telemetry_endpoint=values.get("V7_AGENT_OTEL_ENDPOINT"),
            heartbeat_file=(
                Path(values["V7_WORKER_HEARTBEAT_FILE"])
                if values.get("V7_WORKER_HEARTBEAT_FILE")
                else None
            ),
        )


@dataclass(frozen=True)
class AuthoringComposition:
    database_url: str
    cwl_image: str
    catalog: CatalogContext
    operator_catalog: Mapping[str, object]
    deployment: DeploymentContext
    data: ServiceEndpoint | None
    execution: ServiceEndpoint | None
    heartbeat_file: Path | None = None

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> AuthoringComposition:
        values = os.environ if values is None else values
        deployment = deployment_context(values)
        operator_path = Path(_required(values, "V7_OPERATOR_CATALOG"))
        catalog = CatalogContext(
            operator_catalog_path=operator_path,
            images={"cwl": values.get("V7_CWL_IMAGE")},
        )
        return cls(
            database_url=_required(values, "V7_DATABASE_URL"),
            cwl_image=_required(values, "V7_CWL_IMAGE"),
            catalog=catalog,
            operator_catalog=load_operator_catalog(operator_path),
            deployment=deployment,
            data=(
                ServiceEndpoint(values["V7_DATA_URL"], values["V7_PLATFORM_DATA_TOKEN"])
                if values.get("V7_DATA_URL")
                else None
            ),
            execution=(
                ServiceEndpoint(values["V7_EXECUTION_URL"], values["V7_PLATFORM_EXECUTION_TOKEN"])
                if values.get("V7_EXECUTION_URL")
                else None
            ),
            heartbeat_file=(
                Path(values["V7_WORKER_HEARTBEAT_FILE"])
                if values.get("V7_WORKER_HEARTBEAT_FILE")
                else None
            ),
        )


@dataclass(frozen=True)
class AgentAdminComposition:
    database_url: str

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> AgentAdminComposition:
        values = os.environ if values is None else values
        return cls(database_url=_required(values, "V7_DATABASE_URL"))


@dataclass(frozen=True)
class AgentBackupComposition:
    """Only the two Agent stores required by the backup CLI."""

    database_url: str
    checkpoint_database_url: str

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> AgentBackupComposition:
        values = os.environ if values is None else values
        return cls(
            database_url=_required(values, "V7_DATABASE_URL"),
            checkpoint_database_url=_required(values, "V7_AGENT_CHECKPOINT_DATABASE_URL"),
        )


@dataclass(frozen=True)
class AgentCheckpointComposition:
    """Checkpoint administration has no need to assemble the worker."""

    checkpoint_database_url: str

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> AgentCheckpointComposition:
        values = os.environ if values is None else values
        return cls(checkpoint_database_url=_required(values, "V7_AGENT_CHECKPOINT_DATABASE_URL"))


@dataclass(frozen=True)
class AgentToolCatalogComposition:
    """Explicit output location for the tool-catalogue maintenance command."""

    tool_catalog_path: Path | None

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> AgentToolCatalogComposition:
        values = os.environ if values is None else values
        raw = values.get("V7_AGENT_TOOL_CATALOG")
        return cls(tool_catalog_path=Path(raw) if raw else None)
