import io
import json
import math
import urllib.error
import urllib.request

import pytest
from platform_agent_runtime.embedding_client import (
    EmbeddingServiceError,
    HttpEmbeddingClient,
    embedding_client_from_environment,
)
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.embeddings import EmbeddingRequest, cosine_similarity


class Response:
    def __init__(self, value, status=200):
        self.status = status
        self._stream = io.BytesIO(json.dumps(value).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read1(self, size):
        return self._stream.read(size)

    def getcode(self):
        return self.status


def response(model="qwen3-embedding-4b-q4", dimensions=3):
    return {
        "object": "list",
        "model": model,
        "data": [
            {"index": 1, "embedding": [0.0, 1.0, 0.0]},
            {"index": 0, "embedding": [1.0, 0.0, 0.0]},
        ],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
        "x_gateway": {
            "model_id": model,
            "dimension": dimensions,
            "quantization": "q4",
            "artifact_digest": "sha256:" + "a" * 64,
            "service_compute_ms": 1,
            "gpu_ids": [0],
        },
    }


def test_client_normalizes_input_preserves_order_and_validates_gateway():
    captured = {}

    def open_request(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response(response())

    client = HttpEmbeddingClient(
        "http://192.0.2.1:38173",
        dimensions=3,
        timeout_seconds=7,
        opener=open_request,
    )
    result = client.embed(
        EmbeddingRequest(
            "qwen3-embedding-4b-q4",
            ("地下水位", "e\u0301"),
            3,
        )
    )
    assert captured == {
        "body": {
            "model": "qwen3-embedding-4b-q4",
            "input": ["地下水位", "é"],
            "dimensions": 3,
        },
        "timeout": 7,
    }
    assert result.vectors == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert result.prompt_tokens == 4
    assert result.gateway["gpu_ids"] == [0]
    assert cosine_similarity(result.vectors[0], result.vectors[0]) == 1.0


def test_default_transport_explicitly_disables_proxy_inheritance(monkeypatch):
    seen = {}
    real_build_opener = urllib.request.build_opener

    def capture(*handlers):
        seen["handlers"] = handlers
        return real_build_opener(*handlers)

    monkeypatch.setattr(urllib.request, "build_opener", capture)
    HttpEmbeddingClient("https://embedding.example.test")
    proxy = next(
        value for value in seen["handlers"] if isinstance(value, urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(model="different"),
        lambda value: value["data"][0].update(embedding=[1.0]),
        lambda value: value["data"][0].update(index=0),
        lambda value: value["data"][0]["embedding"].__setitem__(0, math.inf),
        lambda value: value["x_gateway"].update(dimension=1024),
        lambda value: value["x_gateway"].update(artifact_digest="sha256:" + "b" * 64),
    ],
)
def test_contract_violations_are_rejected(mutate):
    value = response()
    mutate(value)
    client = HttpEmbeddingClient(
        "http://192.0.2.1:38173",
        dimensions=3,
        quantization="q4",
        artifact_digest="sha256:" + "a" * 64,
        opener=lambda *_args, **_kwargs: Response(value),
    )
    with pytest.raises(EmbeddingServiceError) as caught:
        client.embed(EmbeddingRequest("qwen3-embedding-4b-q4", ("a", "b"), 3))
    assert caught.value.code == "INVALID_EMBEDDING_RESPONSE"
    assert caught.value.retryable is False


def test_http_rate_limit_is_retryable_without_silent_retry():
    calls = []

    def rejected(*_args, **_kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("http://service", 429, "rate limited", {}, None)

    client = HttpEmbeddingClient("http://192.0.2.1:38173", dimensions=3, opener=rejected)
    with pytest.raises(EmbeddingServiceError) as caught:
        client.embed(EmbeddingRequest("qwen3-embedding-4b-q4", ("a",), 3))
    assert caught.value.retryable is True
    assert len(calls) == 1


def test_environment_uses_frozen_catalog_and_restricted_credential(monkeypatch, tmp_path):
    credential = tmp_path / "credential"
    credential.write_text("secret-token\n")
    credential.chmod(0o600)
    unsigned = {
        "schema_version": 1,
        "catalog_id": "p8-embedding-v1",
        "state": "ENABLED",
        "origin": "https://embedding.example.test",
        "model_id": "qwen3-embedding-4b-q4",
        "dimensions": 1024,
        "quantization": "q4",
        "artifact_digest": "sha256:" + "a" * 64,
        "credential_file": str(credential),
        "ca_file": "/etc/ssl/certs/ca-certificates.crt",
        "request_timeout_seconds": 15,
        "p95_limit_seconds": 10,
    }
    catalog = tmp_path / "embedding.json"
    catalog.write_text(json.dumps(unsigned | {"catalog_digest": canonical_digest(unsigned)}))
    monkeypatch.setenv("V7_EMBEDDING_CATALOG", str(catalog))
    client = embedding_client_from_environment()
    assert client.model == "qwen3-embedding-4b-q4"
    assert client.dimensions == 1024
    assert client.bearer_token == "secret-token"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "file:///tmp/model"},
        {"base_url": "http://user:password@example.test"},
        {"base_url": "http://example.test/base"},
        {"base_url": "http://example.test"},
    ],
)
def test_base_url_cannot_change_the_fixed_endpoint(kwargs):
    with pytest.raises(ValueError):
        HttpEmbeddingClient(**kwargs)


def test_request_limits_are_enforced_before_network_io():
    with pytest.raises(ValueError):
        EmbeddingRequest("qwen3-embedding-4b-q4", (), 1024)
    with pytest.raises(ValueError):
        EmbeddingRequest("qwen3-embedding-4b-q4", ("",), 1024)
    with pytest.raises(ValueError):
        EmbeddingRequest("qwen3-embedding-4b-q4", ("a",), 0)
