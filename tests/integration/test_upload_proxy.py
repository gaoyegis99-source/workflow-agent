import anyio
import pytest
from platform_api.adapters.http import science
from platform_core.application.errors import RuleViolation
from starlette.requests import Request


def request_with_body(chunks, *, content_length=None):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0)

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return Request({"type": "http", "method": "PUT", "headers": headers}, receive)


def read(request):
    return anyio.run(science.bounded_upload_body, request)


def test_upload_proxy_materializes_chunks_once():
    assert read(request_with_body([b"first", b"-second"], content_length=12)) == b"first-second"


def test_upload_proxy_rejects_oversized_declared_body_before_reading(monkeypatch):
    monkeypatch.setattr(science, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(RuleViolation, match="size limit") as raised:
        read(request_with_body([b"unused"], content_length=5))
    assert raised.value.status == 413


def test_upload_proxy_enforces_limit_when_length_is_unknown(monkeypatch):
    monkeypatch.setattr(science, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(RuleViolation, match="size limit") as raised:
        read(request_with_body([b"123", b"45"]))
    assert raised.value.status == 413
