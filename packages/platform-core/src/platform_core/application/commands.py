"""Small transaction command primitive shared without service-to-service imports."""

from __future__ import annotations

from collections.abc import Callable

from platform_core.domain.canonical import digest_hex
from platform_core.domain.control import require


def validate_idempotency_key(key: str) -> None:
    require(
        0 < len(key) <= 128 and key.isascii() and all(33 <= ord(char) <= 126 for char in key),
        "MALFORMED_REQUEST",
        "A valid Idempotency-Key is required",
        400,
    )


def authorize(uow, actor, project: str | None, role: str) -> None:
    if actor.is_service_continuation:
        require(project is not None, "SCOPE_DENIED", "Agent continuation requires a project")
        uow.access.authorize_principal(actor.principal_id, project, role)
    else:
        uow.access.authorize(actor, project, role)


def write_command(
    store,
    actor,
    project: str | None,
    operation: str,
    key: str,
    payload: dict,
    role: str,
    action: Callable[[object], dict],
    *,
    replay_check: Callable[[object, dict], None] | None = None,
) -> dict:
    validate_idempotency_key(key)
    scope = project or "GLOBAL"
    with store.transaction() as uow:
        authorize(uow, actor, project, role)
        replay = uow.delivery.idempotency(actor, scope, operation, key, digest_hex(payload))
        if replay is not None:
            if replay_check is not None:
                replay_check(uow, replay)
            return replay
        result = action(uow)
        uow.delivery.remember(actor, scope, operation, key, result)
        return result
