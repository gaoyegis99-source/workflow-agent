"""P4.3 immutable remote-service binding and sanitized discovery contracts."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from platform_contracts.v1 import Contract, Identifier, PositiveRevision, Sha256Digest


class RemoteTarget(Contract):
    operator_id: Literal["spatial-vector-summary"]
    version_id: Literal["1.0.0"]
    remote_implementation_id: Identifier
    contract_digest: Sha256Digest
    implementation_digest: Sha256Digest | None = None


class RemoteLimits(Contract):
    input_bytes: Annotated[int, Field(gt=0, le=20 * 1024 * 1024)] = 20 * 1024 * 1024
    output_bytes: Annotated[int, Field(gt=0, le=100 * 1024 * 1024)] = 100 * 1024 * 1024
    timeout_seconds: Annotated[int, Field(ge=1, le=180)] = 180


class ServiceBindingCreate(Contract):
    service_binding_id: Identifier
    protocol: Literal["v7-remote-execution-v1", "v7-remote-execution-v2"]
    endpoint: Annotated[str, StringConstraints(min_length=8, max_length=512)]
    credential_ref: Identifier
    tls_ca_ref: Identifier | None = None
    targets: Annotated[list[RemoteTarget], Field(min_length=1, max_length=32)]
    limits: RemoteLimits = RemoteLimits()

    @field_validator("endpoint")
    @classmethod
    def endpoint_origin(cls, value):
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Remote endpoint must be an HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Remote endpoint must not contain paths, credentials or fragments")
        return value.rstrip("/")

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [(v.operator_id, v.version_id) for v in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Remote targets must be unique")
        if self.protocol == "v7-remote-execution-v2":
            if not self.endpoint.startswith("https://") or self.tls_ca_ref is None:
                raise ValueError("Remote v2 requires HTTPS and a CA reference")
            if any(value.implementation_digest is None for value in self.targets):
                raise ValueError("Remote v2 targets require an implementation digest")
        return self


class ServiceBindingView(Contract):
    service_binding_id: Identifier
    revision: PositiveRevision
    protocol: Literal["v7-remote-execution-v1", "v7-remote-execution-v2"]
    enabled: bool
    endpoint_origin: str
    tls_ca_ref: Identifier | None = None
    targets: list[RemoteTarget]
    limits: RemoteLimits
    binding_digest: Sha256Digest


class ServiceBindingList(Contract):
    items: list[ServiceBindingView]
