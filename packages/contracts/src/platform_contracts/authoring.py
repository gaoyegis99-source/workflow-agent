"""Contracts for bounded workflow authoring and trusted validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from platform_contracts.research import ResearchExecutionContext
from platform_contracts.science import (
    AnalysisParameters,
    BufferParameters,
    InputSelection,
    SpatialSelection,
    SpatialSummaryParameters,
    StaticMapParameters,
    TableAggregateParameters,
    TableCastParameters,
    TableFilterParameters,
    TableProfileParameters,
    TableProjectParameters,
    TableSelection,
    TableValidateParameters,
)
from platform_contracts.v1 import (
    CommandReceipt,
    Contract,
    Identifier,
    OperatorTarget,
    PackageRef,
    PositiveRevision,
    Sha256Digest,
)

Title = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=4000)]


class WorkflowMetadata(Contract):
    title: Title
    description: Description = ""
    labels: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def unique_labels(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Workflow labels must be unique")
        return value


class LegacyBlankWorkflowSource(Contract):
    """A1's immutable blank source remains readable after the A2 upgrade."""

    cwl_version: Literal["v1.2"] = Field(alias="cwlVersion", serialization_alias="cwlVersion")
    document_class: Literal["Workflow"] = Field(alias="class", serialization_alias="class")
    inputs: dict[Identifier, Any] = Field(default_factory=dict, max_length=128)
    outputs: dict[Identifier, Any] = Field(default_factory=dict, max_length=128)
    steps: dict[Identifier, Any] = Field(default_factory=dict, max_length=1000)


class WorkflowPackageDraftSource(Contract):
    """Editable A2 package source; incomplete graph semantics are allowed in drafts."""

    profile: Literal["v7-cwl-p4-v1"] = "v7-cwl-p4-v1"
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    entrypoint: Identifier = "main"
    documents: Annotated[dict[Identifier, dict[str, Any]], Field(min_length=1, max_length=100)]
    implementations: Annotated[dict[Identifier, OperatorTarget], Field(max_length=32)] = Field(
        default_factory=dict
    )


WorkflowDraftSource = LegacyBlankWorkflowSource | WorkflowPackageDraftSource


class WorkflowDefinitionCreate(Contract):
    expected_revision: Literal[0]
    stable_key: Identifier
    metadata: WorkflowMetadata
    layout: Annotated[dict[str, Any], Field(max_length=1000)] = Field(default_factory=dict)


class WorkflowRevisionCreate(Contract):
    expected_revision: PositiveRevision
    source_digest: Sha256Digest
    metadata: WorkflowMetadata
    layout: Annotated[dict[str, Any], Field(max_length=1000)] = Field(default_factory=dict)
    source: WorkflowPackageDraftSource | None = None


class AuthoringSourceImportCreate(Contract):
    format: Literal["JSON", "YAML"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=1024 * 1024)]


class AuthoringSourceImportView(Contract):
    source: WorkflowPackageDraftSource
    source_digest: Sha256Digest
    format: Literal["JSON", "YAML"]
    editable: Literal[True] = True


class WorkflowRevisionView(Contract):
    project_id: Identifier
    definition_id: Identifier
    revision: PositiveRevision
    source: WorkflowDraftSource
    source_digest: Sha256Digest
    metadata: WorkflowMetadata
    layout: dict[str, Any]
    layout_digest: Sha256Digest
    created_by: Identifier
    created_at: str


class WorkflowDefinitionView(Contract):
    project_id: Identifier
    definition_id: Identifier
    stable_key: Identifier
    head_revision: PositiveRevision
    created_by: Identifier
    created_at: str
    head: WorkflowRevisionView


class WorkflowDefinitionPage(Contract):
    items: list[WorkflowDefinitionView]
    next_cursor: str | None = None


class WorkflowRevisionPage(Contract):
    items: list[WorkflowRevisionView]
    next_cursor: str | None = None


class OperatorAuthoringDescriptor(Contract):
    operator_id: Identifier
    version_id: str
    operator_digest: Sha256Digest
    ownership: str
    trust_level: str
    lifecycle: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    input_ports: dict[str, Any]
    output_ports: dict[str, Any]
    implementations: list[dict[str, Any]]
    parameter_schema: dict[str, Any]
    ui_hints: dict[str, Any]
    server_validation_required: bool
    semantic_contract_digest: Sha256Digest
    presentation_digest: Sha256Digest
    descriptor_digest: Sha256Digest


class AuthoringDescriptor(Contract):
    schema_version: Literal[1] = 1
    operator_catalog_digest: Sha256Digest
    operators: list[OperatorAuthoringDescriptor]
    semantic_contract_digest: Sha256Digest
    presentation_digest: Sha256Digest
    descriptor_digest: Sha256Digest


class WorkflowRevisionRef(Contract):
    definition_id: Identifier
    revision: PositiveRevision
    source_digest: Sha256Digest


PreviewSelection = InputSelection | TableSelection | SpatialSelection


class AuthoringPreviewCreate(Contract):
    revision_ref: WorkflowRevisionRef | None = None
    source: WorkflowPackageDraftSource | None = None
    sample_bindings: Annotated[dict[Identifier, PreviewSelection], Field(max_length=64)] = Field(
        default_factory=dict
    )
    semantic_contract_digest: Sha256Digest

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.revision_ref is None) == (self.source is None):
            raise ValueError("Exactly one revision_ref or source is required")
        return self


class AuthoringIssueLocation(Contract):
    document_id: Identifier | None = None
    step_key: Identifier | None = None
    port: Identifier | None = None
    pointer: Annotated[str, StringConstraints(max_length=512)] = ""


class AuthoringIssue(Contract):
    code: Identifier
    severity: Literal["ERROR", "WARNING", "INFO"]
    scope: Literal["SOURCE", "SAMPLE_BINDING"]
    location: AuthoringIssueLocation
    message: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class StructurePreview(Contract):
    representation_kind: Literal["TABLE", "VECTOR", "IMAGE", "REPORT", "METHOD", "UNKNOWN"]
    content_role: str
    fields: list[dict[str, Any]] | None = None
    spatial: dict[str, Any] | None = None
    source: str | None = None
    shape: Literal["SCALAR", "ARRAY"] | None = None
    nullable: bool = False


class NodePreview(Contract):
    step_key: Identifier
    document_id: Identifier = "main"
    call_path: list[Identifier] = Field(default_factory=list, max_length=8)
    node_kind: Literal["OPERATOR", "SUBWORKFLOW"] = "OPERATOR"
    status: Literal["COMPATIBLE", "INCOMPATIBLE", "REQUIRES_BINDING"]
    outputs: dict[Identifier, StructurePreview]


class AuthoringPreviewView(Contract):
    status: Literal["COMPATIBLE", "INCOMPATIBLE", "REQUIRES_BINDING"]
    source_digest: Sha256Digest
    bindings_digest: Sha256Digest
    semantic_contract_digest: Sha256Digest
    catalog_digest: Sha256Digest
    nodes: list[NodePreview]
    issues: list[AuthoringIssue]


class AuthoringValidationCreate(Contract):
    revision: PositiveRevision
    source_digest: Sha256Digest
    semantic_contract_digest: Sha256Digest


class AuthoringValidationReceipt(Contract):
    validation_id: Identifier
    status: Literal["QUEUED", "RUNNING", "PASSED", "REJECTED", "ERROR"]
    status_url: str


class AuthoringValidationView(AuthoringValidationReceipt):
    project_id: Identifier
    definition_id: Identifier
    revision: PositiveRevision
    source_digest: Sha256Digest
    context_digest: Sha256Digest
    generation: int
    issues: list[AuthoringIssue]
    created_by: Identifier
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    current_valid: bool
    stale_reasons: list[Identifier]


class WorkflowCandidateCreate(Contract):
    revision: PositiveRevision
    source_digest: Sha256Digest
    validation_id: Identifier


class WorkflowCandidateReceipt(Contract):
    candidate_id: Identifier
    status: Literal["QUEUED", "VALIDATING", "READY", "REJECTED"]
    status_url: str


class WorkflowCandidateView(WorkflowCandidateReceipt):
    project_id: Identifier
    definition_id: Identifier
    revision: PositiveRevision
    validation_id: Identifier
    source_digest: Sha256Digest
    context_digest: Sha256Digest
    package_id: Identifier
    package_ref: PackageRef | None = None
    error: dict[str, Any] | None = None
    created_by: Identifier
    created_at: str
    current_valid: bool
    stale_reasons: list[Identifier]


class WorkflowCandidatePage(Contract):
    items: list[WorkflowCandidateView]
    next_cursor: str | None = None


class TrialExpectation(Contract):
    output: Identifier
    path: Annotated[list[Identifier | int], Field(max_length=32)] = Field(default_factory=list)
    schema_id: Identifier
    representation_digest: Sha256Digest | None = None


class WorkflowTrialCreate(Contract):
    candidate_id: Identifier
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    inputs: Annotated[dict[Identifier, Any], Field(max_length=128)]
    max_node_executions: Annotated[int, Field(ge=1, le=1000)]
    trial_expectations: Annotated[list[TrialExpectation], Field(max_length=64)] = Field(
        default_factory=list
    )

    @field_validator("trial_expectations")
    @classmethod
    def unique_expectations(cls, value):
        identities = [(item.output, tuple(item.path)) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("Trial output expectations must be unique")
        return value


class WorkflowTrialReceipt(Contract):
    trial_id: Identifier
    trial_case_digest: Sha256Digest
    command: CommandReceipt


class WorkflowTrialView(Contract):
    project_id: Identifier
    definition_id: Identifier
    trial_id: Identifier
    candidate_id: Identifier
    task_id: Identifier
    action_id: Identifier
    execution_id: Identifier | None = None
    execution_outcome: str
    trial_case_digest: Sha256Digest
    case_verdict: Literal["PENDING", "PASS", "FAIL", "ERROR"]
    case_result: dict[str, Any] | None = None
    case_result_digest: Sha256Digest | None = None
    error_category: Identifier | None = None
    generation: int
    created_by: Identifier
    created_at: str
    finished_at: str | None = None


class WorkflowTrialPage(Contract):
    items: list[WorkflowTrialView]
    next_cursor: str | None = None


VersionLabel = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=64,
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
            r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?$"
        ),
    ),
]


class WorkflowReleaseCreate(Contract):
    candidate_id: Identifier
    version_label: VersionLabel
    trial_ids: Annotated[list[Identifier], Field(min_length=1, max_length=64)]

    @field_validator("trial_ids")
    @classmethod
    def unique_trials(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Release trial identifiers must be unique")
        return value


class WorkflowReleaseView(Contract):
    release_id: Identifier
    project_id: Identifier
    definition_id: Identifier
    stable_key: Identifier
    metadata: WorkflowMetadata
    version_label: VersionLabel
    candidate_id: Identifier
    revision: PositiveRevision
    source_digest: Sha256Digest
    source: WorkflowDraftSource
    package_ref: PackageRef
    trial_ids: list[Identifier]
    context_digest: Sha256Digest
    lifecycle: Literal["ACTIVE", "DISABLED", "REVOKED"]
    lifecycle_revision: PositiveRevision
    lifecycle_reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    current_executable: bool
    unavailable_reasons: list[Identifier]
    created_by: Identifier
    created_at: str
    qualification: Literal["SAMPLE_VERIFIED"] = "SAMPLE_VERIFIED"


class WorkflowReleasePage(Contract):
    items: list[WorkflowReleaseView]
    next_cursor: str | None = None


class WorkflowReleaseLifecycleCreate(Contract):
    expected_lifecycle_revision: PositiveRevision
    lifecycle: Literal["ACTIVE", "DISABLED", "REVOKED"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]

    @field_validator("reason")
    @classmethod
    def reason_is_meaningful(cls, value):
        if not value.strip():
            raise ValueError("Lifecycle reason must contain non-whitespace text")
        return value


class WorkflowReleaseLifecycleView(Contract):
    event_id: Identifier
    release_id: Identifier
    lifecycle_revision: PositiveRevision
    previous_lifecycle: Literal["ACTIVE", "DISABLED", "REVOKED"] | None = None
    lifecycle: Literal["ACTIVE", "DISABLED", "REVOKED"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    created_by: Identifier
    created_at: str


class WorkflowReleaseLifecyclePage(Contract):
    items: list[WorkflowReleaseLifecycleView]
    next_cursor: str | None = None


class WorkflowReleaseExecutionCreate(Contract):
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    inputs: Annotated[dict[Identifier, Any], Field(max_length=128)]
    max_node_executions: Annotated[int, Field(ge=1, le=1000)]
    research_context: ResearchExecutionContext | None = None


PARAMETER_MODELS = {
    "timeseries-completeness": AnalysisParameters,
    "statistics-linear-trend": AnalysisParameters,
    "qgis-buffer": BufferParameters,
    "spatial-vector-summary": SpatialSummaryParameters,
    "qgis-static-map": StaticMapParameters,
    "table-profile": TableProfileParameters,
    "table-validate": TableValidateParameters,
    "table-project": TableProjectParameters,
    "table-filter": TableFilterParameters,
    "table-cast": TableCastParameters,
    "table-aggregate": TableAggregateParameters,
}

TABLE_INPUT = {
    "transport_type": "File",
    "representation_kind": "TABLE",
    "media_types": ["application/vnd.apache.parquet"],
    "schema_ids": [],
    "content_role": "SOURCE_TABLE",
    "structure_rule": "RUNTIME",
    "constraints": {"physical_types": ["string", "int64", "double", "bool"]},
}
VECTOR_INPUT = {
    "transport_type": "File",
    "representation_kind": "VECTOR",
    "media_types": ["application/geopackage+sqlite3"],
    "schema_ids": [],
    "content_role": "SOURCE_VECTOR",
    "structure_rule": "RUNTIME",
    "constraints": {"geometry": ["POINT", "LINE", "POLYGON", "MULTI"]},
}


def _file_port(kind: str, role: str, rule: str, *schemas: str) -> dict:
    media = {
        "TABLE": ["application/vnd.apache.parquet"],
        "VECTOR": ["application/geopackage+sqlite3"],
        "IMAGE": ["image/png"],
        "REPORT": ["application/json"],
        "METHOD": ["application/json"],
    }[kind]
    return {
        "transport_type": "File",
        "representation_kind": kind,
        "media_types": media,
        "schema_ids": list(schemas),
        "content_role": role,
        "structure_rule": rule,
        "constraints": {},
    }


PORT_SEMANTICS = {
    "timeseries-completeness": {
        "inputs": {"observations": TABLE_INPUT},
        "outputs": {
            "result": _file_port("REPORT", "COMPLETENESS_RESULT", "FIXED", "p3-result-v1"),
            "table": _file_port("TABLE", "COMPLETENESS_TABLE", "FIXED"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "statistics-linear-trend": {
        "inputs": {"observations": TABLE_INPUT},
        "outputs": {
            "result": _file_port("REPORT", "TREND_RESULT", "FIXED", "p3-result-v1"),
            "table": _file_port("TABLE", "TREND_TABLE", "FIXED"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "qgis-buffer": {
        "inputs": {"vector": VECTOR_INPUT},
        "outputs": {
            "vector": _file_port("VECTOR", "DERIVED_VECTOR", "RUNTIME"),
            "manifest": _file_port("REPORT", "SPATIAL_MANIFEST", "FIXED"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "spatial-vector-summary": {
        "inputs": {"vector": VECTOR_INPUT},
        "outputs": {
            "summary": _file_port("REPORT", "SPATIAL_SUMMARY", "FIXED"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "qgis-static-map": {
        "inputs": {"vector": VECTOR_INPUT},
        "outputs": {
            "image": _file_port("IMAGE", "CARTOGRAPHIC_PRODUCT", "FIXED"),
            "manifest": _file_port("REPORT", "MAP_MANIFEST", "FIXED"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-profile": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port("REPORT", "PROFILE_REPORT", "FIXED", "table-profile-report-v1"),
            "table": _file_port("TABLE", "PROFILE_TABLE", "FIXED", "table-profile-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-validate": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port(
                "REPORT", "VALIDATION_REPORT", "FIXED", "table-validation-report-v1"
            ),
            "table": _file_port("TABLE", "VALIDATION_REPORT", "FIXED", "table-validation-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-project": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port("REPORT", "PROJECT_REPORT", "FIXED", "table-project-report-v1"),
            "table": _file_port("TABLE", "DERIVED_TABLE", "PROJECT", "table-project-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-filter": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port("REPORT", "FILTER_REPORT", "FIXED", "table-filter-report-v1"),
            "table": _file_port("TABLE", "DERIVED_TABLE", "PRESERVE", "table-filter-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-cast": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port("REPORT", "CAST_REPORT", "FIXED", "table-cast-report-v1"),
            "table": _file_port("TABLE", "DERIVED_TABLE", "CAST", "table-cast-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
    "table-aggregate": {
        "inputs": {"table": TABLE_INPUT},
        "outputs": {
            "report": _file_port(
                "REPORT", "AGGREGATE_REPORT", "FIXED", "table-aggregate-report-v1"
            ),
            "table": _file_port("TABLE", "DERIVED_TABLE", "AGGREGATE", "table-aggregate-table-v1"),
            "method": _file_port("METHOD", "METHOD", "FIXED"),
        },
    },
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _ui_hints(schema: dict) -> dict:
    properties = schema.get("properties", {})
    return {
        "order": list(properties),
        "fields": {
            name: {
                "widget": (
                    "checkbox"
                    if value.get("type") == "boolean"
                    else "select"
                    if "enum" in value
                    else "number"
                    if value.get("type") in {"integer", "number"}
                    else "text"
                )
            }
            for name, value in properties.items()
        },
    }


def build_authoring_descriptor(catalog: dict) -> dict:
    """Project already-validated compute definitions into stable authoring metadata."""

    operators = sorted(
        (
            item
            for item in catalog.get("operators", [])
            if item.get("operator_id") in PARAMETER_MODELS
        ),
        key=lambda item: (item["operator_id"], item["version_id"]),
    )
    if len(operators) != len(PARAMETER_MODELS) or not isinstance(
        catalog.get("catalog_digest"), str
    ):
        raise ValueError("Operator catalogue does not cover authoring contracts")
    descriptors = []
    for operator in operators:
        model = PARAMETER_MODELS[operator["operator_id"]]
        parameter_schema = model.model_json_schema(mode="validation")
        hints = _ui_hints(parameter_schema)
        decorators = getattr(model, "__pydantic_decorators__", None)
        semantic = {
            "operator_id": operator["operator_id"],
            "version_id": operator["version_id"],
            "operator_digest": operator["operator_digest"],
            "ownership": operator["ownership"],
            "trust_level": operator["trust_level"],
            "lifecycle": operator["lifecycle"],
            "inputs": deepcopy(operator["inputs"]),
            "outputs": deepcopy(operator["outputs"]),
            "input_ports": deepcopy(PORT_SEMANTICS[operator["operator_id"]]["inputs"]),
            "output_ports": deepcopy(PORT_SEMANTICS[operator["operator_id"]]["outputs"]),
            "implementations": deepcopy(operator["implementations"]),
            "parameter_schema": parameter_schema,
            "server_validation_required": bool(
                decorators
                and (
                    decorators.validators
                    or decorators.field_validators
                    or decorators.model_validators
                )
            ),
        }
        descriptor = semantic | {
            "ui_hints": hints,
            "semantic_contract_digest": _digest(semantic),
            "presentation_digest": _digest(hints),
        }
        descriptor["descriptor_digest"] = _digest(descriptor)
        descriptors.append(descriptor)
    result = {
        "schema_version": 1,
        "operator_catalog_digest": catalog["catalog_digest"],
        "operators": descriptors,
        "semantic_contract_digest": _digest(
            [item["semantic_contract_digest"] for item in descriptors]
        ),
        "presentation_digest": _digest([item["presentation_digest"] for item in descriptors]),
    }
    result["descriptor_digest"] = _digest(result)
    return result


def validate_operator_parameters(operator_id: str, parameters: Any) -> list[dict]:
    """Return stable, value-free field errors from the authoritative Pydantic DTO."""

    model = PARAMETER_MODELS.get(operator_id)
    if model is None:
        return [
            {
                "code": "UNKNOWN_OPERATOR",
                "pointer": "",
                "message": "Operator has no trusted parameter contract",
            }
        ]
    try:
        model.model_validate(parameters)
    except ValidationError as error:
        issues = []
        for item in error.errors(include_input=False, include_url=False):
            pointer = "".join(
                "/" + str(value).replace("~", "~0").replace("/", "~1") for value in item["loc"]
            )
            issues.append(
                {
                    "code": "PARAMETER_INVALID",
                    "pointer": pointer,
                    "message": str(item["msg"])[:512],
                }
            )
        return issues
    return []
