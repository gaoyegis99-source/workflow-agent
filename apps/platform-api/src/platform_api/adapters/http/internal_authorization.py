"""Injected authorization policy for Execution's internal Platform routes."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path

from platform_core.application.errors import RuleViolation


@dataclass(frozen=True)
class InternalExecutionPolicy:
    token: str
    maintenance_file: Path | None

    def authorize(self, authorization: str | None) -> None:
        if not hmac.compare_digest(authorization or "", "Bearer " + self.token):
            raise RuleViolation("AUTHENTICATION_REQUIRED", "Service credential required", 401)

    def maintenance_active(self) -> bool:
        return self.maintenance_file is not None and self.maintenance_file.is_file()
