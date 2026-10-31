import pytest
from platform_api.adapters.http.identity import IdentityResolver
from platform_core.domain.control import Identity, RuleViolation


class Database:
    def __init__(self):
        self.subjects: list[str] = []

    def authenticate(self, raw):
        if raw != "valid-token":
            raise RuleViolation("AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)
        return Identity("principal-token", "token-id")

    def principal(self, principal_id):
        return {"principal_id": principal_id, "name": "Stored principal"}

    def authenticate_subject(self, subject):
        self.subjects.append(subject)
        if subject != "idp:alice":
            raise RuleViolation("AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)
        return Identity("principal-proxy", None, "trusted-proxy")


def test_token_mode_keeps_bearer_compatibility():
    resolved = IdentityResolver(Database(), mode="token").resolve({}, "Bearer valid-token")

    assert resolved.actor == Identity("principal-token", "token-id")
    assert resolved.mode == "token"
    assert resolved.display_name == "Stored principal"


def test_trusted_proxy_requires_proof_and_uses_stable_subject_only():
    database = Database()
    resolver = IdentityResolver(database, mode="trusted-proxy", proxy_token="p" * 32)

    resolved = resolver.resolve(
        {
            "x-v7-trusted-proxy": "p" * 32,
            "x-platform-subject": "idp:alice",
            "x-platform-display-name": "Alice from IdP",
            # A forged role header is intentionally ignored by identity
            # resolution; project membership remains the authorization fact.
            "x-platform-roles": "OWNER",
        },
        "Bearer ignored-in-proxy-mode",
    )

    assert resolved.actor.principal_id == "principal-proxy"
    assert resolved.actor.is_trusted_proxy
    assert resolved.subject == "idp:alice"
    assert database.subjects == ["idp:alice"]


def test_trusted_proxy_rejects_bad_proof_before_subject_lookup():
    database = Database()
    resolver = IdentityResolver(database, mode="trusted-proxy", proxy_token="p" * 32)

    with pytest.raises(RuleViolation) as error:
        resolver.resolve(
            {
                "x-v7-trusted-proxy": "wrong",
                "x-platform-subject": "idp:alice",
                "x-platform-display-name": "Alice",
            },
            None,
        )

    assert error.value.status == 401
    assert database.subjects == []


def test_trusted_proxy_requires_a_high_entropy_process_secret():
    with pytest.raises(RuntimeError, match="V7_TRUSTED_PROXY_TOKEN"):
        IdentityResolver(Database(), mode="trusted-proxy", proxy_token="short")


def test_service_continuation_requires_an_explicit_credential_kind():
    assert not Identity("principal", None).is_service_continuation
    assert Identity("principal", None, "service-continuation").is_service_continuation
