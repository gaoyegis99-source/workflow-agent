import pytest
from platform_contracts.remote import ServiceBindingCreate
from pydantic import ValidationError


def valid():
    return {
        "service_binding_id": "p43-peer",
        "protocol": "v7-remote-execution-v1",
        "endpoint": "http://192.168.167.89:19080",
        "credential_ref": "p43-peer-token",
        "targets": [
            {
                "operator_id": "spatial-vector-summary",
                "version_id": "1.0.0",
                "remote_implementation_id": "spatial-summary-remote-v1",
                "contract_digest": (
                    "sha256:8a49ec81ca22f0ab595ea30cc8f1f9641433ee9820e9a76e095d26889b0754ee"
                ),
            }
        ],
        "limits": {
            "input_bytes": 20 * 1024 * 1024,
            "output_bytes": 100 * 1024 * 1024,
            "timeout_seconds": 180,
        },
    }


def test_binding_contract_is_strict_and_origin_only():
    assert ServiceBindingCreate.model_validate(valid()).endpoint.endswith("19080")
    for endpoint in ("file:///tmp/peer", "https://user@example.test", "https://example.test/path"):
        value = valid() | {"endpoint": endpoint}
        with pytest.raises(ValidationError):
            ServiceBindingCreate.model_validate(value)


def test_target_and_limits_are_closed():
    value = valid()
    value["targets"][0]["operator_id"] = "qgis-buffer"
    with pytest.raises(ValidationError):
        ServiceBindingCreate.model_validate(value)


def test_v2_requires_https_ca_and_implementation_digest():
    value = valid()
    value["protocol"] = "v7-remote-execution-v2"
    with pytest.raises(ValidationError):
        ServiceBindingCreate.model_validate(value)
    value["endpoint"] = "https://192.168.167.89:19080"
    value["tls_ca_ref"] = "p43-test-ca"
    value["targets"][0]["implementation_digest"] = "sha256:" + "2" * 64
    assert ServiceBindingCreate.model_validate(value).protocol.endswith("v2")


def test_origin_rejects_query_and_embedded_password():
    for endpoint in ("http://example.test?x=1", "http://user:password@example.test"):
        with pytest.raises(ValidationError):
            ServiceBindingCreate.model_validate(valid() | {"endpoint": endpoint})
    value = valid()
    value["limits"]["output_bytes"] = 100 * 1024 * 1024 + 1
    with pytest.raises(ValidationError):
        ServiceBindingCreate.model_validate(value)
