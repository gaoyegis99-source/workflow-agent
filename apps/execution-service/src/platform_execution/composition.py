"""Execution application's only environment-to-configuration mapping."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_contracts import (
    CatalogContext,
    ContainerIdentity,
    DeploymentContext,
    ExecutionRuntimeConfig,
    ServiceEndpoint,
)


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise ValueError(f"{name}_REQUIRED")
    return value


def _optional_path(values: Mapping[str, str], name: str) -> Path | None:
    value = values.get(name)
    return Path(value) if value else None


def deployment_context(values: Mapping[str, str]) -> DeploymentContext:
    return DeploymentContext(
        deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
        deployment_id=values.get("V7_DEPLOYMENT_ID"),
        deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
    )


@dataclass(frozen=True)
class ExecutionComposition:
    database_url: str
    platform_token: str
    data: ServiceEndpoint
    platform: ServiceEndpoint
    catalog: CatalogContext
    runtime: ExecutionRuntimeConfig
    workflow_image: str | None
    server_host: str = "127.0.0.1"
    server_port: int = 8012

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> ExecutionComposition:
        values = os.environ if values is None else values
        deployment = deployment_context(values)
        images = {
            key: value
            for key, value in {
                "operator": values.get("V7_OPERATOR_IMAGE"),
                "checker": values.get("V7_CHECKER_IMAGE"),
                "qgis": values.get("V7_SPATIAL_QGIS_IMAGE_ID"),
                "spatial_native": values.get("V7_SPATIAL_NATIVE_IMAGE_ID"),
                "gdal_checker": values.get("V7_GDAL_CHECKER_IMAGE"),
                "tabular": values.get("V7_TABULAR_OPERATOR_IMAGE"),
                "cwl": values.get("V7_CWL_IMAGE"),
            }.items()
            if value is not None
        }
        raw_remote = values.get("V7_REMOTE_CREDENTIALS_JSON", "{}")
        try:
            remote_credentials = json.loads(raw_remote)
        except json.JSONDecodeError as error:
            raise ValueError("V7_REMOTE_CREDENTIALS_JSON_INVALID") from error
        if not isinstance(remote_credentials, dict):
            raise ValueError("V7_REMOTE_CREDENTIALS_JSON_INVALID")
        identity = ContainerIdentity(
            deployment_id=values.get("V7_DEPLOYMENT_ID"),
            deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
            test_run_id=values.get("V7_TEST_RUN_ID"),
        )
        return cls(
            database_url=_required(values, "V7_EXECUTION_DATABASE_URL"),
            platform_token=_required(values, "V7_PLATFORM_EXECUTION_TOKEN"),
            data=ServiceEndpoint(
                _required(values, "V7_DATA_URL"),
                _required(values, "V7_EXECUTION_DATA_TOKEN"),
            ),
            platform=ServiceEndpoint(
                _required(values, "V7_PLATFORM_URL"),
                _required(values, "V7_EXECUTION_PLATFORM_TOKEN"),
            ),
            catalog=CatalogContext(
                operator_catalog_path=_optional_path(values, "V7_OPERATOR_CATALOG"),
                checker_catalog_path=_optional_path(values, "V7_CHECKER_CATALOG"),
                images=images,
            ),
            runtime=ExecutionRuntimeConfig(
                work_root=Path(_required(values, "V7_EXECUTION_WORKDIR")),
                workflow_engine_callback_url=values.get("V7_EXECUTION_CALLBACK_URL"),
                park_manifest_path=_optional_path(values, "V7_PARK_MANIFEST"),
                container_identity=identity,
                deployment=deployment,
                workload_network=values.get("V7_EXECUTION_WORKLOAD_NETWORK"),
                remote_credentials=remote_credentials,
                remote_credential_dir=Path(
                    values.get("V7_REMOTE_CREDENTIAL_DIR", "/run/secrets/v7-remote")
                ),
                remote_ca_dir=Path(values.get("V7_REMOTE_CA_DIR", "/etc/v7/remote-ca")),
                worker_heartbeat_file=(
                    Path(values["V7_WORKER_HEARTBEAT_FILE"])
                    if values.get("V7_WORKER_HEARTBEAT_FILE")
                    else None
                ),
            ),
            workflow_image=values.get("V7_CWL_IMAGE"),
            server_host=values.get("V7_EXECUTION_HOST", "127.0.0.1"),
            server_port=int(values.get("V7_EXECUTION_PORT", "8012")),
        )

    @classmethod
    def for_test(
        cls, *, database_url: str = "postgresql://unused", platform_token: str = "x" * 32
    ) -> ExecutionComposition:
        deployment = DeploymentContext()
        identity = ContainerIdentity()
        return cls(
            database_url=database_url,
            platform_token=platform_token,
            data=ServiceEndpoint("http://data.test", "d" * 32),
            platform=ServiceEndpoint("http://platform.test", "p" * 32),
            catalog=CatalogContext(),
            runtime=ExecutionRuntimeConfig(
                work_root=Path("/tmp/v7-execution-test"),
                workflow_engine_callback_url=None,
                park_manifest_path=None,
                container_identity=identity,
                deployment=deployment,
            ),
            workflow_image=None,
        )


@dataclass(frozen=True)
class ExecutionAdminComposition:
    """Database-only composition used by ``execution-admin``.

    Schema migration must not receive service tokens, catalog paths, runtime
    work directories, or a Docker-capable execution configuration.
    """

    database_url: str

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> ExecutionAdminComposition:
        values = os.environ if values is None else values
        return cls(database_url=_required(values, "V7_EXECUTION_DATABASE_URL"))
