"""Small standard-library helpers for authenticated internal transports."""

import hashlib
import hmac
import json

from .runtime_context import DeploymentContext


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


DEPLOYMENT_HEADER = b"x-v7-deployment-id"
DEPLOYMENT_REVISION_HEADER = b"x-v7-deployment-revision"


def deployment_headers(headers=None, *, deployment: DeploymentContext):
    """Attach a non-authorizing direct-deployment identity to a request."""

    if not isinstance(deployment, DeploymentContext):
        raise TypeError("deployment must be an explicit DeploymentContext")
    result = dict(headers or {})
    if deployment.deployment_id:
        result["X-V7-Deployment-ID"] = deployment.deployment_id
    if deployment.deployment_revision:
        result["X-V7-Deployment-Revision"] = deployment.deployment_revision
    return result


class DeploymentIdentityMiddleware:
    """Expose the configured deployment identity without creating a gate.

    Direct Compose deployments intentionally do not use an imported lock as a
    startup or request authorization condition.  Internal service credentials
    remain the authorization boundary; these response headers are tracing
    metadata only.
    """

    def __init__(self, app, deployment: DeploymentContext):
        if not isinstance(deployment, DeploymentContext):
            raise TypeError("deployment must be an explicit DeploymentContext")
        self.app = app
        self.deployment = deployment

    async def __call__(self, scope, receive, send):
        async def identified(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", []))
                if self.deployment.deployment_id:
                    headers.append((DEPLOYMENT_HEADER, self.deployment.deployment_id.encode()))
                if self.deployment.deployment_revision:
                    headers.append(
                        (DEPLOYMENT_REVISION_HEADER, self.deployment.deployment_revision.encode())
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, identified)
