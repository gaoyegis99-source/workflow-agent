import json

import pytest
from platform_agent_runtime.embedding_client import embedding_client_from_environment
from platform_core.application.embedding_catalog import (
    load_embedding_catalog,
    validate_embedding_catalog,
)
from platform_core.domain.agent_runtime import canonical_digest


def enabled(tmp_path):
    unsigned = {
        "schema_version": 1,
        "catalog_id": "p8-embedding-v1",
        "state": "ENABLED",
        "origin": "https://embedding.example.test",
        "model_id": "fixed-model",
        "dimensions": 3,
        "quantization": "int8",
        "artifact_digest": "sha256:" + "a" * 64,
        "credential_file": str(tmp_path / "credential"),
        "ca_file": "/etc/ssl/certs/ca-certificates.crt",
        "request_timeout_seconds": 15,
        "p95_limit_seconds": 10,
    }
    return unsigned | {"catalog_digest": canonical_digest(unsigned)}


def test_disabled_release_catalog_is_valid_and_has_no_endpoint_or_credential():
    catalog = load_embedding_catalog("deploy/p8/embedding-catalog.disabled.json")
    assert catalog["state"] == "DISABLED"
    assert "origin" not in catalog and "credential_file" not in catalog


def test_enabled_catalog_rejects_http_alias_digest_and_latency_drift(tmp_path):
    catalog = enabled(tmp_path)
    assert validate_embedding_catalog(catalog) == catalog
    for changes in (
        {"origin": "http://192.168.1.2:8000"},
        {"request_timeout_seconds": 16},
        {"p95_limit_seconds": 11},
        {"artifact_digest": "fixed-model-worker-name"},
    ):
        tampered = catalog | changes
        unsigned = {key: value for key, value in tampered.items() if key != "catalog_digest"}
        tampered["catalog_digest"] = canonical_digest(unsigned)
        with pytest.raises(ValueError):
            validate_embedding_catalog(tampered)


def test_production_credential_permissions_fail_closed(tmp_path, monkeypatch):
    credential = tmp_path / "credential"
    credential.write_text("private-token")
    credential.chmod(0o640)
    catalog = enabled(tmp_path)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setenv("V7_EMBEDDING_CATALOG", str(path))
    with pytest.raises(ValueError, match="permissions"):
        embedding_client_from_environment()
