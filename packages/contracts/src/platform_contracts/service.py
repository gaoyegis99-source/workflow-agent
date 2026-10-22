"""Small standard-library helpers for authenticated internal transports."""

import hashlib
import hmac
import json
import os


class ServiceFault(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def ensure(condition, code, message, status=409):
    if not condition:
        raise ServiceFault(code, message, status)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def authenticate(header, credentials, allowed):
    for name in allowed:
        token = credentials.get(name)
        if token and len(token) >= 32 and hmac.compare_digest(header or "", "Bearer " + token):
            return name
    raise ServiceFault("AUTHENTICATION_REQUIRED", "Valid service authentication required", 401)


RELEASE_HEADER = b"x-v7-release-digest"
LEGACY_WORKFLOW_BRIDGE_PREFIX = "/internal/workflows/"


def release_headers(headers=None):
    """Attach the pinned release identity to a local service request."""

    result = dict(headers or {})
    value = os.environ.get("V7_RELEASE_DIGEST")
    if value:
        result["X-V7-Release-Digest"] = value
    return result


class ReleaseIdentityMiddleware:
    """Reject mixed production releases on authenticated internal routes.

    This is deliberately a small ASGI middleware so the contracts package
    keeps its standard-library-only transport boundary.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        expected = os.environ.get("V7_RELEASE_DIGEST")
        production = os.environ.get("V7_DEPLOYMENT_MODE", "development") == "production"
        path = scope.get("path", "")
        if scope.get("type") == "http" and production and path.startswith("/internal/"):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(RELEASE_HEADER, b"").decode("ascii", "ignore")
            # N-1 Toil leaders are intentionally resumed with their immutable
            # original image.  The 0.15.0 bridge predates this transport
            # header, so an absent value is accepted only on its dedicated
            # workflow route.  That endpoint still requires the per-workflow
            # high-entropy token and exact epoch/Action identity.  A supplied
            # but wrong digest is always rejected, including on this route.
            legacy_bridge = path.startswith(LEGACY_WORKFLOW_BRIDGE_PREFIX) and not supplied
            if not expected or (not legacy_bridge and not hmac.compare_digest(supplied, expected)):
                body = json.dumps(
                    {
                        "code": "RELEASE_MISMATCH",
                        "message": "Internal service release identity differs",
                    },
                    separators=(",", ":"),
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        async def identified(message):
            if message.get("type") == "http.response.start" and expected:
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-v7-release-digest", expected.encode())
                ]
            await send(message)

        await self.app(scope, receive, identified)
