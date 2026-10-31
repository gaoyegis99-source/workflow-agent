"""Explicit dependencies shared by the internal Execution HTTP route groups."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from platform_contracts.service import authenticate, ensure

from platform_execution.adapters.repository import Repository
from platform_execution.composition import ExecutionComposition


@dataclass(frozen=True)
class ExecutionApiContext:
    """Route dependencies assembled once by ``create_app``.

    It carries concrete ports and immutable deployment configuration rather
    than asking route handlers to load environment state or reach into the app
    façade.
    """

    repository: Repository
    composition: ExecutionComposition
    platform_token: str
    workflow_profile: Any
    operator_catalog: Any | None
    checker_catalog: Any | None

    def authenticate_platform(self, request: Request) -> None:
        authenticate(
            request.headers.get("authorization"),
            {"platform": self.platform_token},
            {"platform"},
        )

    @staticmethod
    def authenticate_workflow(request: Request, root: dict) -> None:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        expected = root["runtime"].get("bridge_token_digest", "")
        ensure(
            len(supplied) >= 32
            and len(expected) == 64
            and hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected),
            "AUTHENTICATION_REQUIRED",
            "Workflow engine credential required",
            401,
        )


__all__ = ("ExecutionApiContext",)
