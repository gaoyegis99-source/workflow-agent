"""Knowledge application's environment mapping and explicit dependencies."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_contracts import DeploymentContext, ServiceEndpoint


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise ValueError(f"{name}_REQUIRED")
    return value


@dataclass(frozen=True)
class KnowledgeComposition:
    database_url: str
    storage_root: str
    platform_token: str
    data: ServiceEndpoint
    deployment: DeploymentContext
    host: str
    port: int
    heartbeat_file: Path | None = None

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> KnowledgeComposition:
        values = os.environ if values is None else values
        return cls(
            database_url=_required(values, "V7_KNOWLEDGE_DATABASE_URL"),
            storage_root=_required(values, "V7_KNOWLEDGE_STORAGE"),
            platform_token=_required(values, "V7_PLATFORM_KNOWLEDGE_TOKEN"),
            data=ServiceEndpoint(
                _required(values, "V7_DATA_URL"), _required(values, "V7_KNOWLEDGE_DATA_TOKEN")
            ),
            deployment=DeploymentContext(
                deployment_mode=values.get("V7_DEPLOYMENT_MODE", "development"),
                deployment_id=values.get("V7_DEPLOYMENT_ID"),
                deployment_revision=values.get("V7_DEPLOYMENT_REVISION"),
            ),
            host=values.get("V7_KNOWLEDGE_HOST", "127.0.0.1"),
            port=int(values.get("V7_KNOWLEDGE_PORT", "8013")),
            heartbeat_file=(
                Path(values["V7_WORKER_HEARTBEAT_FILE"])
                if values.get("V7_WORKER_HEARTBEAT_FILE")
                else None
            ),
        )

    @classmethod
    def for_test(cls) -> KnowledgeComposition:
        return cls(
            database_url="postgresql+psycopg://unused",
            storage_root="/tmp/v7-knowledge-test",
            platform_token="p" * 32,
            data=ServiceEndpoint("http://data.test", "d" * 32),
            deployment=DeploymentContext(),
            host="127.0.0.1",
            port=8013,
            heartbeat_file=None,
        )


@dataclass(frozen=True)
class KnowledgeAdminComposition:
    """Database-only composition used by ``knowledge-admin`` migrations."""

    database_url: str

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> KnowledgeAdminComposition:
        values = os.environ if values is None else values
        return cls(database_url=_required(values, "V7_KNOWLEDGE_DATABASE_URL"))
