from __future__ import annotations

from pathlib import Path

import pytest
from platform_contracts import (
    CatalogContext,
    ContainerIdentity,
    DeploymentContext,
    ExecutionRuntimeConfig,
    ServiceEndpoint,
)


def test_deployment_context_has_no_release_lock_or_filesystem_gate():
    with pytest.raises(ValueError, match="V7_DEPLOYMENT_MODE_INVALID"):
        DeploymentContext(deployment_mode="production")
    with pytest.raises(ValueError, match="V7_DEPLOYMENT_ID_INVALID"):
        DeploymentContext(deployment_mode="direct", deployment_id=" ")

    deployment = DeploymentContext(
        deployment_mode="direct",
        deployment_id="p8-local",
        deployment_revision="sha256:" + "a" * 64,
    )
    assert deployment.is_direct


def test_runtime_contexts_validate_values_and_do_not_expose_tokens():
    with pytest.raises(ValueError, match="V7_CATALOG_IMAGE_INVALID"):
        CatalogContext(images={"operator": "not-a-digest"})
    with pytest.raises(ValueError, match="V7_SERVICE_TOKEN_INVALID"):
        ServiceEndpoint("https://data.example.test", "short")
    with pytest.raises(ValueError, match="V7_EXECUTION_CALLBACK_URL_INVALID"):
        ExecutionRuntimeConfig(
            work_root=Path("/work"),
            workflow_engine_callback_url="file:///callback",
            park_manifest_path=None,
            container_identity=ContainerIdentity(),
            deployment=DeploymentContext(),
        )

    endpoint = ServiceEndpoint("https://data.example.test", "t" * 32)
    assert "t" * 32 not in repr(endpoint)


def test_runtime_context_contract_module_is_pure():
    contracts = Path(__file__).parents[2] / "packages/contracts/src/platform_contracts"
    for name in ("runtime_context.py", "release.py", "service.py"):
        source = (contracts / name).read_text(encoding="utf-8")
        assert "import os" not in source
        assert "os.environ" not in source
        assert "os.getenv" not in source


def test_deployment_headers_require_an_explicit_deployment_context():
    from platform_contracts.service import deployment_headers

    with pytest.raises(TypeError):
        deployment_headers()
    with pytest.raises(TypeError, match="explicit DeploymentContext"):
        deployment_headers(deployment=None)
    assert deployment_headers(
        deployment=DeploymentContext(
            deployment_mode="direct", deployment_id="p8-local", deployment_revision="rev-1"
        )
    ) == {
        "X-V7-Deployment-ID": "p8-local",
        "X-V7-Deployment-Revision": "rev-1",
    }
