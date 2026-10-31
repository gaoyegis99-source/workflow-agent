"""Health and readiness routes for the Execution HTTP boundary."""

from __future__ import annotations

from fastapi import FastAPI
from platform_contracts.service import ensure


def register_health_routes(
    app: FastAPI,
    *,
    database,
    operator_catalog_path,
) -> None:
    """Register stable liveness/readiness endpoints without service algorithms."""

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "execution"}

    @app.get("/readyz")
    def ready():
        database.check_schema()
        ensure(
            operator_catalog_path is None or app.state.operator_catalog is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Operator Catalog is not loaded",
            503,
        )
        return {
            "status": "ready",
            "operator_catalog_digest": (
                app.state.operator_catalog["catalog_digest"]
                if app.state.operator_catalog is not None
                else None
            ),
            "release_digest": app.state.release_digest,
        }
