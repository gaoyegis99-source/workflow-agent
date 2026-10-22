"""Small public gateway used to establish a verifiable cold-backup write barrier."""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

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


def create_gateway(
    upstream: str | None = None,
    maintenance_file: str | Path | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    target = (
        upstream or os.environ.get("V7_PLATFORM_UPSTREAM", "http://platform-api:8007")
    ).rstrip("/")
    barrier = Path(
        maintenance_file or os.environ.get("V7_MAINTENANCE_FILE", "/var/lib/v7-control/maintenance")
    )

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
        release_digest = os.environ.get("V7_RELEASE_DIGEST")
        if release_digest:
            response.headers.setdefault("X-V7-Release-Digest", release_digest)
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
    uvicorn.run(create_gateway(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
