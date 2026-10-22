"""Bounded OpenAI-compatible embedding adapter with proxy inheritance disabled."""

from __future__ import annotations

import json
import math
import os
import ssl
import stat
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import urlsplit

from platform_core.application.embedding_catalog import load_embedding_catalog
from platform_core.domain.embeddings import EmbeddingBatch, EmbeddingRequest

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class EmbeddingServiceError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


class HttpEmbeddingClient:
    """Strict client for one frozen model and dimension."""

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "qwen3-embedding-4b-q4",
        dimensions: int = 1024,
        timeout_seconds: float = 15.0,
        bearer_token: str | None = None,
        quantization: str | None = None,
        artifact_digest: str | None = None,
        ca_file: str | None = None,
        opener=None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Embedding base URL must be an HTTP origin without credentials")
        if parsed.scheme != "https" and opener is None:
            raise ValueError("Embedding production transport requires HTTPS")
        if not 0.1 <= timeout_seconds <= 15:
            raise ValueError("Embedding timeout must be between 0.1 and 15 seconds")
        # An empty ProxyHandler is deliberate: LAN inference traffic must not
        # inherit HTTP(S)_PROXY from the host process.
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if ca_file is not None:
            handlers.append(
                urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file))
            )
        self._transport = urllib.request.build_opener(*handlers)
        self._open = opener or self._transport.open
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.bearer_token = bearer_token
        self.quantization = quantization
        self.artifact_digest = artifact_digest

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        if request.model != self.model or request.dimensions != self.dimensions:
            raise ValueError("Embedding request differs from the frozen client profile")
        payload = json.dumps(
            {
                "model": request.model,
                "input": list(request.inputs),
                "dimensions": request.dimensions,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = "Bearer " + self.bearer_token
        http_request = urllib.request.Request(
            self.base_url + "/v1/embeddings",
            data=payload,
            method="POST",
            headers=headers,
        )
        started = time.monotonic()
        try:
            with self._open(http_request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status >= 400:
                    raise EmbeddingServiceError(
                        "EMBEDDING_REJECTED",
                        f"Embedding service returned HTTP {status}",
                        retryable=status == 429 or status >= 500,
                        status_code=status,
                    )
                raw = self._read_bounded(response)
        except EmbeddingServiceError:
            raise
        except urllib.error.HTTPError as error:
            raise EmbeddingServiceError(
                "EMBEDDING_REJECTED",
                f"Embedding service returned HTTP {error.code}",
                retryable=error.code == 429 or error.code >= 500,
                status_code=error.code,
            ) from error
        except (TimeoutError, urllib.error.URLError, OSError, IncompleteRead) as error:
            raise EmbeddingServiceError(
                "EMBEDDING_UNAVAILABLE",
                "Embedding service is temporarily unavailable",
                retryable=True,
            ) from error
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise EmbeddingServiceError(
                "INVALID_EMBEDDING_RESPONSE",
                "Embedding service returned invalid JSON",
                retryable=False,
            ) from error
        result = self._parse_response(value, request)
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms > self.timeout_seconds * 1000:
            raise EmbeddingServiceError(
                "EMBEDDING_TIMEOUT",
                "Embedding request exceeded the hard wall timeout",
                retryable=True,
            )
        gateway = dict(result.gateway)
        service_ms = gateway.get("service_compute_ms")
        gateway["client_wall_ms"] = elapsed_ms
        if type(service_ms) in {int, float} and math.isfinite(float(service_ms)):
            gateway["queue_and_transport_ms"] = max(0.0, elapsed_ms - float(service_ms))
        return EmbeddingBatch(
            model=result.model,
            dimensions=result.dimensions,
            vectors=result.vectors,
            prompt_tokens=result.prompt_tokens,
            gateway=gateway,
        )

    def _read_bounded(self, response) -> bytes:
        deadline = time.monotonic() + self.timeout_seconds
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Embedding response exceeded the wall timeout")
            reader = getattr(response, "read1", None) or response.read
            chunk = reader(65_536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise EmbeddingServiceError(
                    "EMBEDDING_RESPONSE_TOO_LARGE",
                    "Embedding response exceeds the fixed size limit",
                    retryable=False,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _parse_response(self, value: object, request: EmbeddingRequest) -> EmbeddingBatch:
        try:
            if not isinstance(value, dict) or value.get("model") != request.model:
                raise ValueError("model mismatch")
            data = value["data"]
            if not isinstance(data, list) or len(data) != len(request.inputs):
                raise ValueError("item count mismatch")
            by_index: dict[int, tuple[float, ...]] = {}
            for item in data:
                if not isinstance(item, dict) or type(item.get("index")) is not int:
                    raise ValueError("invalid item")
                index = item["index"]
                raw_vector = item.get("embedding")
                if index in by_index or not isinstance(raw_vector, list):
                    raise ValueError("duplicate index or invalid vector")
                if len(raw_vector) != request.dimensions:
                    raise ValueError("dimension mismatch")
                vector = tuple(
                    float(component) for component in raw_vector if type(component) in {int, float}
                )
                if len(vector) != request.dimensions or not all(map(math.isfinite, vector)):
                    raise ValueError("invalid vector component")
                by_index[index] = vector
            if set(by_index) != set(range(len(request.inputs))):
                raise ValueError("indices mismatch")
            gateway = value.get("x_gateway") or {}
            if not isinstance(gateway, dict):
                raise ValueError("invalid gateway metadata")
            if gateway.get("dimension") != request.dimensions:
                raise ValueError("gateway dimension mismatch")
            if gateway.get("model_id") != request.model:
                raise ValueError("gateway model mismatch")
            if self.quantization is not None and gateway.get("quantization") != self.quantization:
                raise ValueError("gateway quantization mismatch")
            if (
                self.artifact_digest is not None
                and gateway.get("artifact_digest") != self.artifact_digest
            ):
                raise ValueError("gateway artifact mismatch")
            usage = value.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            if prompt_tokens is not None and (type(prompt_tokens) is not int or prompt_tokens < 0):
                raise ValueError("invalid usage")
            return EmbeddingBatch(
                model=request.model,
                dimensions=request.dimensions,
                vectors=tuple(by_index[index] for index in range(len(request.inputs))),
                prompt_tokens=prompt_tokens,
                gateway=dict(gateway),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingServiceError(
                "INVALID_EMBEDDING_RESPONSE",
                "Embedding response violates the frozen contract",
                retryable=False,
            ) from error


def embedding_client_from_environment() -> HttpEmbeddingClient:
    path = os.environ.get("V7_EMBEDDING_CATALOG")
    if not path:
        raise ValueError("V7_EMBEDDING_CATALOG is not configured")
    catalog = load_embedding_catalog(path)
    if catalog["state"] != "ENABLED":
        raise ValueError("Embedding is disabled by the frozen catalog")
    credential = _read_credential(catalog["credential_file"])
    return embedding_client_from_catalog(catalog, credential)


def embedding_client_from_catalog(catalog: dict, bearer_token: str | None) -> HttpEmbeddingClient:
    if catalog.get("state") != "ENABLED":
        raise ValueError("Embedding is disabled by the frozen catalog")
    return HttpEmbeddingClient(
        catalog["origin"],
        model=catalog["model_id"],
        dimensions=catalog["dimensions"],
        timeout_seconds=catalog["request_timeout_seconds"],
        bearer_token=bearer_token,
        quantization=catalog["quantization"],
        artifact_digest=catalog["artifact_digest"],
        ca_file=catalog["ca_file"],
    )


def _read_credential(path: str) -> str:
    target = os.lstat(path)
    if stat.S_ISLNK(target.st_mode) or not stat.S_ISREG(target.st_mode):
        raise ValueError("Embedding credential must be a regular file")
    if stat.S_IMODE(target.st_mode) & 0o077:
        raise ValueError("Embedding credential permissions must not be wider than 0600")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Embedding credential file is invalid")
    return value
