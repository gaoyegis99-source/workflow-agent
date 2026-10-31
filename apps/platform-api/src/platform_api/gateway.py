"""Small public gateway used to establish a verifiable cold-backup write barrier."""

from __future__ import annotations

import argparse
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from platform_api.composition import GatewayComposition

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PROXY_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")
_IDENTITY_HEADERS = {
    "authorization",
    "x-v7-trusted-proxy",
    "x-platform-subject",
    "x-platform-display-name",
    "x-platform-roles",
}


def _proxy_display_name(value: str) -> str:
    """Decode the conventional ASGI representation before validating it."""
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return value.strip()


def trusted_proxy_headers(
    request: Request, headers: dict[str, str], *, shared_secret: str
) -> dict[str, str] | None:
    """Normalize identity headers only after authenticating the edge proxy.

    The public gateway does not forward caller-supplied identity headers.  A
    trusted edge proxy proves its identity with a secret kept out of browsers;
    the gateway then reconstructs the minimal internal identity envelope for
    Platform API.  Project roles are deliberately not part of that envelope.
    """
    proof = request.headers.get("x-v7-trusted-proxy", "")
    subject = request.headers.get("x-platform-subject", "")
    display_name = _proxy_display_name(request.headers.get("x-platform-display-name", ""))
    if (
        len(shared_secret) < 32
        or not hmac.compare_digest(proof, shared_secret)
        or _PROXY_SUBJECT.fullmatch(subject) is None
        or not display_name
        or len(display_name.encode("utf-8")) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in display_name)
    ):
        return None
    forwarded = {
        key: value for key, value in headers.items() if key.lower() not in _IDENTITY_HEADERS
    }
    forwarded["X-V7-Trusted-Proxy"] = shared_secret
    forwarded["X-Platform-Subject"] = subject
    forwarded["X-Platform-Display-Name"] = display_name
    return forwarded


def create_gateway(
    upstream: str | None = None,
    maintenance_file: str | Path | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    composition: GatewayComposition | None = None,
) -> FastAPI:
    if composition is None:
        composition = GatewayComposition.for_test(
            upstream=upstream or "http://platform-api:8007",
            maintenance_file=Path(maintenance_file or "/var/lib/v7-control/maintenance"),
        )
    elif upstream is not None or maintenance_file is not None:
        raise ValueError("Gateway composition and direct overrides are mutually exclusive")
    target = composition.upstream.rstrip("/")
    barrier = composition.maintenance_file

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            base_url=target,
            transport=transport,
            timeout=httpx.Timeout(130.0, connect=5.0),
            trust_env=False,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="Groundwater Platform V7 public gateway", lifespan=lifespan)

    @app.middleware("http")
    async def observability_headers(request: Request, call_next):
        response = await call_next(request)
        # Health and maintenance responses do not traverse Platform's
        # RequestIdMiddleware.  Apply the same public observability contract
        # at the gateway boundary, while preserving an upstream identity when
        # a proxied response already carries one.
        response.headers.setdefault("X-Request-ID", uuid4().hex)
        response.headers.setdefault("Cache-Control", "no-store")
        if composition.deployment.deployment_id:
            response.headers.setdefault("X-V7-Deployment-ID", composition.deployment.deployment_id)
        if composition.deployment.deployment_revision:
            response.headers.setdefault(
                "X-V7-Deployment-Revision",
                composition.deployment.deployment_revision,
            )
        return response

    @app.get("/gateway/health")
    async def gateway_health():
        return {"status": "ok", "maintenance": barrier.is_file()}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy(path: str, request: Request):
        if request.method in MUTATING_METHODS and barrier.is_file():
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "30"},
                content={
                    "code": "MAINTENANCE_IN_PROGRESS",
                    "message": "The platform is in a consistent backup window",
                    "retry_mode": "AFTER_STATE_CHANGE",
                },
            )
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP | {"host", "content-length"}
        }
        # In trusted-proxy mode direct client identity headers are never
        # forwarded.  The gateway first authenticates the edge proxy and then
        # emits a fresh minimal envelope for the private Platform API.
        if composition.identity_mode == "trusted-proxy":
            headers = trusted_proxy_headers(
                request, headers, shared_secret=composition.trusted_proxy_token
            )
            if headers is None:
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "AUTHENTICATION_REQUIRED",
                        "message": "Valid trusted-proxy authentication is required",
                        "retry_mode": "NONE",
                    },
                )
        try:
            response = await request.app.state.client.request(
                request.method,
                "/" + path,
                params=request.query_params,
                headers=headers,
                content=await request.body(),
            )
        except httpx.TransportError:
            return JSONResponse(
                status_code=503,
                content={
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "The platform API is temporarily unavailable",
                    "retry_mode": "SAME_KEY",
                },
            )
        returned_headers = {
            key: value
            for key, value in response.headers.items()
            # Uvicorn adds Date and Server to the public response. Do not copy
            # the upstream values as well, otherwise both headers are emitted
            # twice on every proxied request.
            if key.lower() not in HOP_BY_HOP | {"content-length", "date", "server"}
        }
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=returned_headers,
        )

    return app


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="Groundwater Platform V7 maintenance gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=port_number, default=8080)
    args = parser.parse_args()
    composition = GatewayComposition.from_environment()
    uvicorn.run(create_gateway(composition=composition), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
