"""Pure, validated deployment values for V7 composition roots.

Contracts deliberately do not inspect environment variables, release files, or
the filesystem.  An executable application's composition module performs that
I/O once, then passes these immutable values to its application and adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_DIGEST_PREFIX = "sha256:"


def _optional_text(value: str | None, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name}_INVALID")
    return value


def _digest(value: str | None, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name}_REQUIRED")
        return None
    if (
        not isinstance(value, str)
        or len(value) != len(_DIGEST_PREFIX) + 64
        or not value.startswith(_DIGEST_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name}_INVALID")
    return value


@dataclass(frozen=True)
class DeploymentContext:
    """Identity supplied by a direct Compose deployment.

    A deployment identifier is useful for request tracing and dynamic-container
    ownership, but it is not a release lock and it never authorizes a startup.
    File identity, image selection and recovery evidence are checked by the
    explicit maintenance procedure rather than imported by application code.
    """

    deployment_mode: str = "development"
    deployment_id: str | None = None
    deployment_revision: str | None = None

    def __post_init__(self) -> None:
        if self.deployment_mode not in {"development", "direct"}:
            raise ValueError("V7_DEPLOYMENT_MODE_INVALID")
        _optional_text(self.deployment_id, "V7_DEPLOYMENT_ID")
        _optional_text(self.deployment_revision, "V7_DEPLOYMENT_REVISION")

    @property
    def is_direct(self) -> bool:
        return self.deployment_mode == "direct"


@dataclass(frozen=True)
class CatalogContext:
    """Catalog locations and immutable image identities, without file I/O."""

    operator_catalog_path: Path | None = None
    checker_catalog_path: Path | None = None
    images: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        paths = (self.operator_catalog_path, self.checker_catalog_path)
        if any(not isinstance(path, Path) for path in paths if path is not None):
            raise ValueError("V7_CATALOG_PATH_INVALID")
        for name, value in self.images.items():
            if not isinstance(name, str) or not name:
                raise ValueError("V7_CATALOG_IMAGE_INVALID")
            _digest(value, "V7_CATALOG_IMAGE", required=True)

    def image(self, name: str) -> str | None:
        return self.images.get(name)


@dataclass(frozen=True)
class ServiceEndpoint:
    """One authenticated internal endpoint.  The token is never repr'd."""

    url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("V7_SERVICE_URL_INVALID")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("V7_SERVICE_TOKEN_INVALID")


@dataclass(frozen=True)
class ContainerIdentity:
    """Labels that identify a container as belonging to this deployment."""

    deployment_id: str | None = None
    deployment_revision: str | None = None
    test_run_id: str | None = None

    def __post_init__(self) -> None:
        _optional_text(self.deployment_id, "V7_DEPLOYMENT_ID")
        _optional_text(self.deployment_revision, "V7_DEPLOYMENT_REVISION")
        _optional_text(self.test_run_id, "V7_TEST_RUN_ID")

    def labels(self) -> dict[str, str]:
        values = {
            "v7.deployment": self.deployment_id,
            "v7.deployment-revision": self.deployment_revision,
            "v7.test-run": self.test_run_id,
        }
        return {name: value for name, value in values.items() if value is not None}


@dataclass(frozen=True)
class ExecutionRuntimeConfig:
    """All deployment values consumed by Execution application components."""

    work_root: Path
    workflow_engine_callback_url: str | None
    park_manifest_path: Path | None
    container_identity: ContainerIdentity
    deployment: DeploymentContext
    workload_network: str | None = None
    remote_credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    remote_credential_dir: Path = Path("/run/secrets/v7-remote")
    remote_ca_dir: Path = Path("/etc/v7/remote-ca")
    worker_heartbeat_file: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.work_root, Path) or not self.work_root.is_absolute():
            raise ValueError("V7_EXECUTION_WORKDIR_INVALID")
        if self.workflow_engine_callback_url is not None:
            parsed = urlsplit(self.workflow_engine_callback_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("V7_EXECUTION_CALLBACK_URL_INVALID")
        if self.workload_network is not None and (
            not self.workload_network
            or any(character.isspace() for character in self.workload_network)
        ):
            raise ValueError("V7_EXECUTION_WORKLOAD_NETWORK_INVALID")
        if self.park_manifest_path is not None and not isinstance(self.park_manifest_path, Path):
            raise ValueError("V7_PARK_MANIFEST_INVALID")
        if not isinstance(self.remote_credential_dir, Path) or not isinstance(
            self.remote_ca_dir, Path
        ):
            raise ValueError("V7_REMOTE_CREDENTIAL_PATH_INVALID")
        if self.worker_heartbeat_file is not None and not isinstance(
            self.worker_heartbeat_file, Path
        ):
            raise ValueError("V7_WORKER_HEARTBEAT_PATH_INVALID")
        for reference, token in self.remote_credentials.items():
            if (
                not isinstance(reference, str)
                or not reference
                or not isinstance(token, str)
                or len(token) < 32
            ):
                raise ValueError("V7_REMOTE_CREDENTIALS_INVALID")
