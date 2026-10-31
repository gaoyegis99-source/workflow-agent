"""Data application's environment mapping; application code receives values only."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_contracts import ContainerIdentity, DeploymentContext


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise ValueError(f"{name}_REQUIRED")
    return value


@dataclass(frozen=True)
class DataComposition:
    database_url: str
    storage_root: str
    credentials: dict[str, str]
    deployment: DeploymentContext
    container_identity: ContainerIdentity
    host: str
    port: int
    spatial_image: str | None
    spatial_work_root: Path | None
    heartbeat_file: Path | None = None

    @classmethod
    def from_environment(
        cls,
        values: Mapping[str, str] | None = None,
        *,
        role: str = "api",
    ) -> DataComposition:
        """Create only the values required by one Data process role.

        The old shared P8 environment made every Data process capable of
        authenticating to sibling services and starting a spatial container.
        Worker processes do neither, so they must not require or receive those
        values merely because they reuse this composition value object.
        """

        if role not in {"api", "worker", "document", "spatial", "database"}:
            raise ValueError("V7_DATA_COMPOSITION_ROLE_INVALID")
        values = os.environ if values is None else values
        deployment = DeploymentContext(
            deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
            deployment_id=values.get("V7_DEPLOYMENT_ID"),
            deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
        )
        is_api = role == "api"
        is_spatial = role == "spatial"
        credentials = (
            {
                "platform": _required(values, "V7_PLATFORM_DATA_TOKEN"),
                "execution": _required(values, "V7_EXECUTION_DATA_TOKEN"),
                "knowledge": _required(values, "V7_KNOWLEDGE_DATA_TOKEN"),
            }
            if is_api
            else {}
        )
        return cls(
            database_url=_required(values, "V7_DATA_DATABASE_URL"),
            storage_root=(
                values.get("V7_DATA_STORAGE", "")
                if role == "database"
                else _required(values, "V7_DATA_STORAGE")
            ),
            credentials=credentials,
            deployment=deployment,
            container_identity=ContainerIdentity(
                deployment_id=values.get("V7_DEPLOYMENT_ID"),
                deployment_revision=deployment.deployment_revision,
                test_run_id=values.get("V7_TEST_RUN_ID"),
            ),
            host=values.get("V7_DATA_HOST", "127.0.0.1") if is_api else "127.0.0.1",
            port=int(values.get("V7_DATA_PORT", "8011")) if is_api else 8011,
            spatial_image=(_required(values, "V7_SPATIAL_QGIS_IMAGE_ID") if is_spatial else None),
            spatial_work_root=(
                Path(_required(values, "V7_DATA_SPATIAL_WORK_ROOT")) if is_spatial else None
            ),
            heartbeat_file=(
                Path(values["V7_WORKER_HEARTBEAT_FILE"])
                if values.get("V7_WORKER_HEARTBEAT_FILE")
                else None
            ),
        )

    @classmethod
    def for_test(cls) -> DataComposition:
        return cls(
            database_url="postgresql+psycopg://unused",
            storage_root="/tmp/v7-data-test",
            credentials={"platform": "p" * 32, "execution": "e" * 32, "knowledge": "k" * 32},
            deployment=DeploymentContext(),
            container_identity=ContainerIdentity(),
            host="127.0.0.1",
            port=8011,
            spatial_image=None,
            spatial_work_root=Path("/tmp/v7-spatial-test"),
            heartbeat_file=None,
        )
