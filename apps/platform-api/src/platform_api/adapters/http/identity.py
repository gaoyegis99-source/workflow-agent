"""Public-user identity resolution for the V7 HTTP boundary.

V7 deliberately separates the credential that establishes a caller from the
project membership that authorizes it.  A trusted proxy may establish an
existing Principal by stable subject, but cannot grant a project role through a
header.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

from platform_core.application.errors import RuleViolation
from platform_core.application.ports import Identity

IDENTITY_MODES = frozenset({"token", "trusted-proxy"})
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")


def _display_name(value: str) -> str:
    """Recover UTF-8 header bytes exposed through ASGI's Latin-1 mapping."""
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return value.strip()


def _invalid_identity() -> RuleViolation:
    return RuleViolation("AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)


@dataclass(frozen=True)
class AuthenticatedIdentity:
    actor: Identity
    mode: str
    subject: str | None
    display_name: str


class IdentityResolver:
    """Resolve exactly one user credential mode selected at process startup."""

    def __init__(self, database, *, mode: str, proxy_token: str = ""):
        self.database = database
        self.mode = mode
        if self.mode not in IDENTITY_MODES:
            raise RuntimeError("V7_IDENTITY_MODE must be token or trusted-proxy")
        self.proxy_token = proxy_token
        if self.mode == "trusted-proxy" and len(self.proxy_token) < 32:
            raise RuntimeError("trusted-proxy mode requires a high-entropy V7_TRUSTED_PROXY_TOKEN")

    def resolve(
        self, headers: Mapping[str, str], authorization: str | None
    ) -> AuthenticatedIdentity:
        if self.database is None:
            raise RuleViolation(
                "DEPENDENCY_UNAVAILABLE",
                "Database is not configured",
                503,
                "SAME_KEY",
            )
        if self.mode == "token":
            if (
                not authorization
                or not authorization.startswith("Bearer ")
                or len(authorization) > 256
            ):
                raise _invalid_identity()
            actor = self.database.authenticate(authorization[7:])
            principal = self.database.principal(actor.principal_id)
            return AuthenticatedIdentity(actor, "token", None, principal["name"])

        supplied_proof = headers.get("x-v7-trusted-proxy", "")
        subject = headers.get("x-platform-subject", "")
        display_name = _display_name(headers.get("x-platform-display-name", ""))
        if (
            not hmac.compare_digest(supplied_proof, self.proxy_token)
            or _SUBJECT.fullmatch(subject) is None
            or not display_name
            or len(display_name.encode("utf-8")) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in display_name)
        ):
            raise _invalid_identity()
        actor = self.database.authenticate_subject(subject)
        return AuthenticatedIdentity(actor, "trusted-proxy", subject, display_name)
