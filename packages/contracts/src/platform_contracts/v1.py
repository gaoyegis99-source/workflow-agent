"""Strict P0 contracts. Object existence and authorization require later use cases."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# The end guard is portable to JSON Schema's ECMA-style regexes: `$` alone can
# match immediately before a final newline in some validators.
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])")
]
VersionId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$(?![\s\S])"),
    Field(json_schema_extra={"not": {"pattern": "^[Ll][Aa][Tt][Ee][Ss][Tt]$"}}),
]
PositiveRevision = Annotated[int, Field(ge=1)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$(?![\s\S])")]
CommandStatus = Literal["RECORDED", "DISPATCHING", "RESOLVED", "REJECTED", "RECONCILING"]
RetryMode = Literal["NONE", "SAME_KEY", "AFTER_STATE_CHANGE"]
ErrorCode = Literal[
    "MALFORMED_REQUEST",
    "AUTHENTICATION_REQUIRED",
    "SCOPE_DENIED",
    "RESOURCE_NOT_FOUND",
    "METHOD_NOT_ALLOWED",
    "REVISION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "PRECONDITION_UNRESOLVED",
    "BUDGET_BLOCKED",
    "EVENT_CURSOR_EXPIRED",
    "INVALID_BINDING",
    "UNSUPPORTED_TARGET",
    "RATE_LIMITED",
    "DEPENDENCY_UNAVAILABLE",
    "INTERNAL_ERROR",
]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False, regex_engine="python-re"
    )


class TaskRef(Contract):
    """Project-scoped identity; project context is supplied by the enclosing resource."""

    task_id: Identifier
    spec_revision: PositiveRevision


class VersionedContract(Contract):
    version_id: VersionId

    @model_validator(mode="after")
    def reject_floating_version(self) -> Self:
        if self.version_id.lower() == "latest":
            raise ValueError("A fixed version_id is required")
        return self


class AssetRef(VersionedContract):
    project_id: Identifier
    asset_id: Identifier
    version_manifest_digest: Sha256Digest


class PackageRef(VersionedContract):
    package_id: Identifier
    manifest_digest: Sha256Digest


class ExecutionRef(Contract):
    """Only an identity for command results; no execution API is implemented in P0."""

    execution_id: Identifier


class LocalBinding(Contract):
    kind: Literal["LOCAL"]
    implementation_id: Identifier


class RemoteBinding(Contract):
    kind: Literal["REMOTE"]
    service_binding_id: Identifier
    binding_revision: PositiveRevision


ExecutionBinding = Annotated[LocalBinding | RemoteBinding, Field(discriminator="kind")]


class OperatorTarget(VersionedContract):
    kind: Literal["OPERATOR"]
    operator_id: Identifier
    execution_binding: ExecutionBinding


class WorkflowTarget(Contract):
    kind: Literal["WORKFLOW"]
    package_ref: PackageRef


class CodePackageTarget(Contract):
    kind: Literal["CODE_PACKAGE"]
    package_ref: PackageRef


ComputationTarget = Annotated[
    OperatorTarget | WorkflowTarget | CodePackageTarget, Field(discriminator="kind")
]


class ErrorLocation(Contract):
    """Schema field path only: never include submitted values or filesystem paths."""

    path: Annotated[list[Identifier | int], Field(max_length=16)]


class ErrorEnvelope(Contract):
    code: ErrorCode
    message: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    request_id: Identifier
    retry_mode: RetryMode
    locations: Annotated[list[ErrorLocation], Field(max_length=32)] = Field(default_factory=list)
    related_refs: Annotated[list[TaskRef | AssetRef], Field(max_length=16)] = Field(
        default_factory=list
    )


class CommandReceipt(Contract):
    """Transport outcome only. RESOLVED does not imply execution or task success."""

    command_id: Identifier
    status: CommandStatus
    action_id: Identifier | None = None
    task_ref: TaskRef
    result: ExecutionRef | None = None
    error: ErrorEnvelope | None = None
    status_url: Annotated[
        str,
        StringConstraints(
            pattern=(
                r"^/api/v1/projects/[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
                r"/commands/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])"
            )
        ),
    ]
    request_id: Identifier

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.status_url.rsplit("/", 1)[-1] != self.command_id:
            raise ValueError("status_url must identify this command")
        if self.status == "REJECTED":
            if self.error is None or self.result is not None:
                raise ValueError("REJECTED requires an error and no result")
        elif self.error is not None:
            raise ValueError("Only REJECTED carries an error")
        if self.result is not None and self.status != "RESOLVED":
            raise ValueError("Only RESOLVED carries a command result")
        return self


class HealthResponse(Contract):
    status: Literal["ok"]
    service: Literal["platform-api"]
    phase: Literal[
        "P0",
        "P2",
        "P3",
        "P4.1",
        "P4.2.1",
        "P4.2.2",
        "P4.3.1",
        "P4.3.1a",
        "P4.3.2",
        "P5.1",
        "P5.2",
        "P5.3",
        "P6.2",
        "P8.2",
    ]
    contract_version: Literal["1"]
