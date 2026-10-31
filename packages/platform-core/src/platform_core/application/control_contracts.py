"""Pure Control request and compatibility contracts."""

from __future__ import annotations

from platform_core.application.ports import Record
from platform_core.domain.canonical import digest_hex
from platform_core.domain.control import require


def digest(value: Record) -> str:
    """Bare digest retained for idempotency fields already stored in Platform."""

    return digest_hex(value)


def compatible_control_request(request: Record) -> Record:
    """New opt-in defaults must not change the digest of a delivered P2 request."""

    request = request.copy()
    if "spec" in request and request["spec"].get("acceptance_spec") is None:
        request["spec"] = {
            key: value for key, value in request["spec"].items() if key != "acceptance_spec"
        }
    if request.get("derived_outputs") is False:
        request.pop("derived_outputs")
    if request.get("acceptance_profiles") == []:
        request.pop("acceptance_profiles")
    if request.get("model_budget") is None:
        request.pop("model_budget", None)
    return request


def require_current_acceptance_contract(spec: Record) -> None:
    acceptance = spec.get("acceptance_spec")
    if not acceptance or acceptance.get("profile") != "open-science-v1":
        return
    if acceptance.get("delivery_class") != "VERIFIED":
        return
    outputs = acceptance.get("required_outputs", [])
    require(
        bool(outputs) and all("role_id" in value and "expected" in value for value in outputs),
        "MALFORMED_REQUEST",
        "New VERIFIED task revisions require exact output role contracts",
        422,
    )
