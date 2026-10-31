"""Platform API environment mapping and dependency composition inputs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_contracts import CatalogContext, DeploymentContext, ServiceEndpoint


def _optional_endpoint(
    values: Mapping[str, str], url_name: str, token_name: str
) -> ServiceEndpoint | None:
    url = values.get(url_name)
    if url is None:
        return None
    token = values.get(token_name)
    if token is None:
        raise ValueError(f"{token_name}_REQUIRED")
    return ServiceEndpoint(url, token)


@dataclass(frozen=True)
class InternalExecutionAuthorization:
    token: str
    maintenance_file: Path | None

    def __post_init__(self) -> None:
        if len(self.token) < 32:
            raise ValueError("V7_EXECUTION_PLATFORM_TOKEN_INVALID")


@dataclass(frozen=True)
class PlatformComposition:
    database_url: str | None
    deployment: DeploymentContext
    catalog: CatalogContext
    operator_catalog: Mapping[str, object]
    checker_catalog: Mapping[str, object]
    data: ServiceEndpoint | None
    execution: ServiceEndpoint | None
    knowledge: ServiceEndpoint | None
    identity_mode: str
    trusted_proxy_token: str
    internal_execution: InternalExecutionAuthorization
    agent_runtime_profile_path: Path | None
    model_catalog_path: Path | None
    agent_tool_catalog_path: Path | None
    cwl_image: str | None
    studio_root: Path

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> PlatformComposition:
        values = os.environ if values is None else values
        deployment = DeploymentContext(
            deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
            deployment_id=values.get("V7_DEPLOYMENT_ID"),
            deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
        )
        images = {
            key: value
            for key, value in {
                "operator": values.get("V7_OPERATOR_IMAGE"),
                "qgis": values.get("V7_SPATIAL_QGIS_IMAGE_ID"),
                "spatial_native": values.get("V7_SPATIAL_NATIVE_IMAGE_ID"),
                "checker": values.get("V7_CHECKER_IMAGE"),
                "gdal_checker": values.get("V7_GDAL_CHECKER_IMAGE"),
                "tabular": values.get("V7_TABULAR_OPERATOR_IMAGE"),
                "cwl": values.get("V7_CWL_IMAGE"),
            }.items()
            if value is not None
        }
        operator_path = values.get("V7_OPERATOR_CATALOG")
        checker_path = values.get("V7_CHECKER_CATALOG")
        if not operator_path:
            raise ValueError("V7_OPERATOR_CATALOG_REQUIRED")
        if not checker_path:
            raise ValueError("V7_CHECKER_CATALOG_REQUIRED")
        catalog = CatalogContext(
            operator_catalog_path=Path(operator_path),
            checker_catalog_path=Path(checker_path),
            images=images,
        )
        operator_catalog, checker_catalog = _load_catalogs(catalog)
        return cls(
            database_url=values.get("V7_DATABASE_URL"),
            deployment=deployment,
            catalog=catalog,
            operator_catalog=operator_catalog,
            checker_catalog=checker_catalog,
            data=_optional_endpoint(values, "V7_DATA_URL", "V7_PLATFORM_DATA_TOKEN"),
            execution=_optional_endpoint(values, "V7_EXECUTION_URL", "V7_PLATFORM_EXECUTION_TOKEN"),
            knowledge=_optional_endpoint(values, "V7_KNOWLEDGE_URL", "V7_PLATFORM_KNOWLEDGE_TOKEN"),
            identity_mode=values.get("V7_IDENTITY_MODE", "token"),
            trusted_proxy_token=values.get("V7_TRUSTED_PROXY_TOKEN", ""),
            internal_execution=InternalExecutionAuthorization(
                values.get("V7_EXECUTION_PLATFORM_TOKEN", ""),
                Path(values["V7_MAINTENANCE_FILE"]) if values.get("V7_MAINTENANCE_FILE") else None,
            ),
            agent_runtime_profile_path=(
                Path(values["V7_AGENT_RUNTIME_PROFILE"])
                if values.get("V7_AGENT_RUNTIME_PROFILE")
                else None
            ),
            model_catalog_path=(
                Path(values["V7_MODEL_CATALOG"]) if values.get("V7_MODEL_CATALOG") else None
            ),
            agent_tool_catalog_path=(
                Path(values["V7_AGENT_TOOL_CATALOG"])
                if values.get("V7_AGENT_TOOL_CATALOG")
                else None
            ),
            cwl_image=values.get("V7_CWL_IMAGE"),
            studio_root=(
                Path(values["V7_STUDIO_ROOT"])
                if values.get("V7_STUDIO_ROOT")
                else Path(__file__).parent / "studio_static"
            ),
        )

    @classmethod
    def for_test(cls, *, studio_root: Path | None = None) -> PlatformComposition:
        # Tests deliberately inject deterministic, pure catalog values.  This
        # is not a runtime fallback: deployed applications must provide the
        # two pinned catalog files above.
        from platform_core.application.catalog import build_catalog
        from platform_core.application.checker_catalog import build_catalog as build_checker_catalog

        return cls(
            database_url=None,
            deployment=DeploymentContext(),
            catalog=CatalogContext(),
            operator_catalog=build_catalog(),
            checker_catalog=build_checker_catalog(),
            data=None,
            execution=None,
            knowledge=None,
            identity_mode="token",
            trusted_proxy_token="",
            internal_execution=InternalExecutionAuthorization("x" * 32, None),
            agent_runtime_profile_path=None,
            model_catalog_path=None,
            agent_tool_catalog_path=None,
            cwl_image=None,
            studio_root=studio_root or Path(__file__).parent / "studio_static",
        )


def _load_catalogs(catalog: CatalogContext) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read pinned catalogs once at the Platform composition boundary."""

    operator_path = catalog.operator_catalog_path
    checker_path = catalog.checker_catalog_path
    if operator_path is None:
        raise ValueError("V7_OPERATOR_CATALOG_REQUIRED")
    if checker_path is None:
        raise ValueError("V7_CHECKER_CATALOG_REQUIRED")
    checker_images = {
        "CHECKER": catalog.image("checker"),
        "GDAL_CHECKER": catalog.image("gdal_checker"),
    }
    if any(value is None for value in checker_images.values()):
        raise ValueError("V7_CHECKER_CATALOG_IMAGES_REQUIRED")
    from platform_core.application.catalog import load_catalog as load_operator_catalog
    from platform_core.application.checker_catalog import load_catalog as load_checker_catalog

    return (
        load_operator_catalog(operator_path),
        load_checker_catalog(checker_path, checker_images),
    )


@dataclass(frozen=True)
class GatewayComposition:
    upstream: str
    maintenance_file: Path
    identity_mode: str
    trusted_proxy_token: str
    deployment: DeploymentContext

    def __post_init__(self) -> None:
        if not self.upstream.startswith(("http://", "https://")):
            raise ValueError("V7_PLATFORM_UPSTREAM_INVALID")
        if self.identity_mode not in {"token", "trusted-proxy"}:
            raise ValueError("V7_IDENTITY_MODE_INVALID")
        if self.identity_mode == "trusted-proxy" and len(self.trusted_proxy_token) < 32:
            raise ValueError("V7_TRUSTED_PROXY_TOKEN_INVALID")

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> GatewayComposition:
        values = os.environ if values is None else values
        return cls(
            upstream=values.get("V7_PLATFORM_UPSTREAM", "http://platform-api:8007"),
            maintenance_file=Path(
                values.get("V7_MAINTENANCE_FILE", "/var/lib/v7-control/maintenance")
            ),
            identity_mode=values.get("V7_IDENTITY_MODE", "token"),
            trusted_proxy_token=values.get("V7_TRUSTED_PROXY_TOKEN", ""),
            deployment=DeploymentContext(
                deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
                deployment_id=values.get("V7_DEPLOYMENT_ID"),
                deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
            ),
        )

    @classmethod
    def for_test(
        cls, *, upstream: str = "http://platform.test", maintenance_file: Path
    ) -> GatewayComposition:
        return cls(
            upstream=upstream,
            maintenance_file=maintenance_file,
            identity_mode="token",
            trusted_proxy_token="",
            deployment=DeploymentContext(),
        )
