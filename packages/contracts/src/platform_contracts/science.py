"""P3 data, computation and delivery contracts; no framework or persistence imports."""

import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints, field_validator, model_validator

from platform_contracts.research import ResearchExecutionContext
from platform_contracts.v1 import (
    AssetRef,
    Contract,
    Identifier,
    OperatorTarget,
    PositiveRevision,
    Sha256Digest,
)
from platform_contracts.workflows import WorkflowOutputSelection

Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
OperatorId = Literal[
    "timeseries-completeness",
    "statistics-linear-trend",
    "qgis-buffer",
    "spatial-vector-summary",
    "qgis-static-map",
    "table-profile",
    "table-validate",
    "table-project",
    "table-filter",
    "table-cast",
    "table-aggregate",
]


class FieldBindings(Contract):
    groupField: Identifier
    timeField: Identifier
    valueField: Identifier

    @model_validator(mode="after")
    def distinct(self):
        if len({self.groupField, self.timeField, self.valueField}) != 3:
            raise ValueError("Field roles must be distinct")
        return self


class InputSelection(Contract):
    asset: AssetRef
    representation_id: Identifier
    representation_digest: Sha256Digest
    semantic_revision: PositiveRevision
    field_bindings: FieldBindings


class TableSelection(Contract):
    kind: Literal["TABLE"] = "TABLE"
    asset: AssetRef
    representation_id: Identifier
    representation_digest: Sha256Digest


class VectorLayerSelector(Contract):
    kind: Literal["VECTOR_LAYER"]
    layer_id: Identifier


class RasterSelector(Contract):
    kind: Literal["RASTER"]
    band_ids: Annotated[list[Identifier], Field(min_length=1, max_length=64)]

    @field_validator("band_ids")
    @classmethod
    def distinct_bands(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Raster band identifiers must be unique")
        return values


SpatialSelector = Annotated[VectorLayerSelector | RasterSelector, Field(discriminator="kind")]


class SpatialSelection(Contract):
    asset: AssetRef
    representation_id: Identifier
    representation_digest: Sha256Digest
    selector: SpatialSelector
    semantic_revision: PositiveRevision | None = None


class AnalysisParameters(Contract):
    groups: Annotated[list[Text], Field(min_length=1, max_length=1000)]
    start: str
    end: str
    frequency: Literal["day", "month", "quarter", "year"]
    min_samples: Annotated[int, Field(ge=3, le=100000)] = 3

    @field_validator("start", "end")
    @classmethod
    def instant(cls, value):
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("Range boundaries require UTC offsets")
        return moment.isoformat()

    @model_validator(mode="after")
    def scope(self):
        if datetime.fromisoformat(self.start) >= datetime.fromisoformat(self.end):
            raise ValueError("Range must be nonempty")
        if len(set(self.groups)) != len(self.groups):
            raise ValueError("Duplicate group")
        return self


class AcceptanceSpec(Contract):
    profile: Literal["p3-timeseries-v1"] = "p3-timeseries-v1"
    input: InputSelection
    parameters: AnalysisParameters
    required_outputs: Annotated[list[OperatorId], Field(min_length=1, max_length=2)]
    allow_not_evaluable: bool = True
    minimum_evaluable: Annotated[int, Field(ge=1, le=1000)] = 1

    @model_validator(mode="after")
    def coverage(self):
        if len(set(self.required_outputs)) != len(self.required_outputs):
            raise ValueError("Duplicate output requirement")
        if self.minimum_evaluable > len(self.parameters.groups):
            raise ValueError("Minimum coverage exceeds scope")
        return self


CheckType = Literal[
    "RUNTIME_INTEGRITY",
    "DATA_VALIDITY",
    "METHOD_PRECONDITION",
    "RESULT_REPRODUCTION",
    "CLAIM_SUPPORT",
    "HUMAN_REVIEW",
]
CheckResult = Literal[
    "PASS",
    "FAIL",
    "INDETERMINATE",
    "REVIEW_REQUIRED",
    "NOT_APPLICABLE_WITH_REASON",
    "ERROR",
    "STALE",
]


class OpenRequirement(Contract):
    requirement_id: Identifier
    description: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    necessary: bool = True
    checks: Annotated[list[CheckType], Field(min_length=1, max_length=6)]

    @field_validator("checks")
    @classmethod
    def unique_checks(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Requirement check types must be unique")
        return values


class ExpectedOutput(Contract):
    result_kind: Literal[
        "OPERATOR_RESULT", "SPATIAL_VECTOR", "SPATIAL_SUMMARY", "CARTOGRAPHIC_PRODUCT"
    ]
    operator_id: Identifier
    operator_version: str
    output_port: Identifier
    schema_id: Identifier


class OutputRequirement(Contract):
    role_id: Identifier
    necessary: bool = True
    expected: ExpectedOutput


class OpenScienceAcceptanceSpec(Contract):
    profile: Literal["open-science-v1"] = "open-science-v1"
    delivery_class: Literal["EXPLORATORY", "VERIFIED"]
    requirements: Annotated[list[OpenRequirement], Field(max_length=64)] = Field(
        default_factory=list
    )
    required_outputs: Annotated[list[OutputRequirement], Field(max_length=64)] = Field(
        default_factory=list
    )
    allow_manual_review: bool = False
    max_check_executions: Annotated[int, Field(ge=1, le=4)] = 1

    @model_validator(mode="after")
    def nonempty_and_unique(self):
        if not self.requirements and not self.required_outputs:
            raise ValueError("Open acceptance requires a clause or required output")
        requirement_ids = [item.requirement_id for item in self.requirements]
        output_ids = [item.role_id for item in self.required_outputs]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("Requirement identifiers must be unique")
        if len(set(output_ids)) != len(output_ids):
            raise ValueError("Required output identifiers must be unique")
        if any("HUMAN_REVIEW" in item.checks for item in self.requirements):
            if not self.allow_manual_review:
                raise ValueError("Human review clauses require allow_manual_review")
        return self


AcceptanceSpecification = Annotated[
    AcceptanceSpec | OpenScienceAcceptanceSpec, Field(discriminator="profile")
]


class UploadCreate(Contract):
    filename: Text
    format: Literal["csv", "parquet", "geopackage", "geotiff", "markdown", "text"]
    size: Annotated[int, Field(gt=0, le=20 * 1024 * 1024)]
    digest: Sha256Digest


class Semantics(Contract):
    timezone: Text
    time_format: Literal["ISO8601"] = "ISO8601"
    value_unit: Text
    quantity: Literal["water_level_elevation", "depth_to_water", "observation_value"]
    missing_values: Annotated[list[str], Field(max_length=16)] = Field(default_factory=lambda: [""])

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        return value


class SemanticRevision(Contract):
    expected_revision: Annotated[int, Field(ge=0)]
    version_id: Identifier
    semantics: Semantics


class AssetSemanticTarget(Contract):
    kind: Literal["ASSET"]


class LayerSemanticTarget(Contract):
    kind: Literal["LAYER"]
    layer_id: Identifier


class FieldSemanticTarget(Contract):
    kind: Literal["FIELD"]
    layer_id: Identifier
    field_id: Identifier


class BandSemanticTarget(Contract):
    kind: Literal["BAND"]
    band_id: Identifier


SpatialSemanticTarget = Annotated[
    AssetSemanticTarget | LayerSemanticTarget | FieldSemanticTarget | BandSemanticTarget,
    Field(discriminator="kind"),
]
SemanticScalar = str | bool | int | float


class SpatialAnnotation(Contract):
    namespace: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    term: Identifier
    target: SpatialSemanticTarget
    value: SemanticScalar | Annotated[list[SemanticScalar], Field(max_length=32)]

    @field_validator("namespace")
    @classmethod
    def absolute_namespace(cls, value):
        parsed = urlparse(value)
        if not parsed.scheme or parsed.scheme not in {"https", "http", "urn"}:
            raise ValueError("Semantic namespace must be an absolute HTTP(S) URI or URN")
        return value

    @field_validator("value")
    @classmethod
    def finite_and_bounded(cls, value):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and (not item or len(item) > 500):
                raise ValueError("Semantic string values must contain 1 to 500 characters")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Semantic numbers must be finite")
        return value


class SpatialSemantics(Contract):
    profile: Literal["spatial-generic-v1"] = "spatial-generic-v1"
    annotations: Annotated[list[SpatialAnnotation], Field(max_length=128)] = Field(
        default_factory=list
    )


class SpatialSemanticRevision(Contract):
    expected_revision: Annotated[int, Field(ge=0)]
    version_id: Identifier
    semantics: SpatialSemantics


class ExecutionInputs(Contract):
    observations: InputSelection


class ExecutionCreate(Contract):
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    target: OperatorTarget
    inputs: ExecutionInputs
    parameters: AnalysisParameters
    research_context: ResearchExecutionContext | None = None


class TableProfileParameters(Contract):
    fields: Annotated[list[Identifier], Field(min_length=1, max_length=128)]

    @field_validator("fields")
    @classmethod
    def distinct_fields(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Profile fields must be unique")
        return values


class TableRule(Contract):
    rule_id: Identifier
    kind: Literal["ROW_COUNT", "TYPE", "NOT_NULL", "UNIQUE", "RANGE", "ALLOWED_VALUES"]
    field_id: Identifier | None = None
    field_ids: Annotated[list[Identifier], Field(min_length=1, max_length=16)] | None = None
    expected_type: Literal["string", "int64", "double", "bool"] | None = None
    minimum: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    maximum: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    allowed_values: Annotated[list[str], Field(min_length=1, max_length=64)] | None = None

    @field_validator("minimum", "maximum")
    @classmethod
    def finite_json_number(cls, value):
        if value is None:
            return value
        import json

        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Numeric bounds must be JSON numbers") from exc
        if type(parsed) not in {int, float} or (
            isinstance(parsed, float) and not math.isfinite(parsed)
        ):
            raise ValueError("Numeric bounds must be finite JSON numbers")
        return value

    @model_validator(mode="after")
    def valid_shape(self):
        populated = {
            "field_id": self.field_id is not None,
            "field_ids": self.field_ids is not None,
            "expected_type": self.expected_type is not None,
            "minimum": self.minimum is not None,
            "maximum": self.maximum is not None,
            "allowed_values": self.allowed_values is not None,
        }
        expected = {
            "ROW_COUNT": {"minimum", "maximum"},
            "TYPE": {"field_id", "expected_type"},
            "NOT_NULL": {"field_id"},
            "UNIQUE": {"field_ids"},
            "RANGE": {"field_id", "minimum", "maximum"},
            "ALLOWED_VALUES": {"field_id", "allowed_values"},
        }[self.kind]
        present = {name for name, value in populated.items() if value}
        if not present <= expected:
            raise ValueError("Rule contains fields that do not apply to its kind")
        if self.kind in {"ROW_COUNT", "RANGE"} and not present.intersection({"minimum", "maximum"}):
            raise ValueError("Range rule requires at least one bound")
        if not (expected - {"minimum", "maximum"}) <= present:
            raise ValueError("Rule is missing a required field")
        if self.field_ids is not None and len(set(self.field_ids)) != len(self.field_ids):
            raise ValueError("Unique key fields must be distinct")
        if self.allowed_values is not None:
            if len(set(self.allowed_values)) != len(self.allowed_values):
                raise ValueError("Allowed values must be unique")
            if any(unicodedata.normalize("NFC", value) != value for value in self.allowed_values):
                raise ValueError("Allowed values must be NFC text")
        return self


class TableValidateParameters(Contract):
    rules: Annotated[list[TableRule], Field(min_length=1, max_length=64)]

    @field_validator("rules")
    @classmethod
    def distinct_rules(cls, values):
        if len({rule.rule_id for rule in values}) != len(values):
            raise ValueError("Rule identifiers must be unique")
        return values


class TableProjectField(Contract):
    source_field_id: Identifier
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value):
        if value is None:
            return value
        if value != value.strip() or unicodedata.normalize("NFC", value) != value:
            raise ValueError("Display names must be trimmed NFC text")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            raise ValueError("Display names cannot contain control or formatting characters")
        return value


class TableProjectParameters(Contract):
    fields: Annotated[list[TableProjectField], Field(min_length=1, max_length=128)]

    @field_validator("fields")
    @classmethod
    def distinct_source_fields(cls, values):
        if len({value.source_field_id for value in values}) != len(values):
            raise ValueError("Projected source fields must be unique")
        return values


FilterLiteral = Annotated[str, StringConstraints(max_length=1000)]
_FILTER_INTEGER = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
_FILTER_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


def _validate_filter_literal(kind: str, value: str):
    if kind == "string":
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("Filter string literals must use NFC")
        return value
    if kind == "bool":
        if value not in {"true", "false"}:
            raise ValueError("Filter boolean literals must be true or false")
        return value == "true"
    if kind == "int64":
        if not _FILTER_INTEGER.fullmatch(value):
            raise ValueError("Filter int64 literals must be canonical decimal integers")
        parsed = int(value)
        if parsed < -(2**63) or parsed > 2**63 - 1:
            raise ValueError("Filter int64 literal exceeds the supported range")
        return parsed
    if kind == "double":
        if not _FILTER_NUMBER.fullmatch(value):
            raise ValueError("Filter double literals must be JSON numbers")
        try:
            parsed = json.loads(value)
            finite = math.isfinite(float(parsed))
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("Filter double literal is invalid") from exc
        if type(parsed) not in {int, float} or not finite:
            raise ValueError("Filter double literals must be finite")
        return float(parsed)
    raise ValueError("Unknown filter literal type")


class TableFilterNode(Contract):
    """One node in a bounded, topologically ordered predicate AST."""

    node_id: Identifier
    kind: Literal[
        "COMPARE",
        "IN",
        "IS_NULL",
        "IS_NAN",
        "IS_FINITE",
        "NOT",
        "AND",
        "OR",
    ]
    field_id: Identifier | None = None
    operator: Literal["EQ", "NE", "LT", "LTE", "GT", "GTE"] | None = None
    value_type: Literal["string", "int64", "double", "bool"] | None = None
    value: FilterLiteral | None = None
    values: Annotated[list[FilterLiteral], Field(min_length=1, max_length=64)] | None = None
    arguments: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_shape(self):
        present = {
            name
            for name, available in {
                "field_id": self.field_id is not None,
                "operator": self.operator is not None,
                "value_type": self.value_type is not None,
                "value": self.value is not None,
                "values": self.values is not None,
                "arguments": bool(self.arguments),
            }.items()
            if available
        }
        expected = {
            "COMPARE": {"field_id", "operator", "value_type", "value"},
            "IN": {"field_id", "value_type", "values"},
            "IS_NULL": {"field_id"},
            "IS_NAN": {"field_id"},
            "IS_FINITE": {"field_id"},
            "NOT": {"arguments"},
            "AND": {"arguments"},
            "OR": {"arguments"},
        }[self.kind]
        if present != expected:
            raise ValueError("Filter node fields differ from its kind")
        if self.kind == "NOT" and len(self.arguments) != 1:
            raise ValueError("NOT requires exactly one argument")
        if self.kind in {"AND", "OR"} and not 2 <= len(self.arguments) <= 16:
            raise ValueError("AND and OR require two to sixteen arguments")
        if self.kind == "COMPARE":
            _validate_filter_literal(self.value_type, self.value)
            if self.value_type == "bool" and self.operator not in {"EQ", "NE"}:
                raise ValueError("Boolean comparisons only support EQ and NE")
        if self.kind == "IN":
            if len(set(self.values)) != len(self.values):
                raise ValueError("Filter IN literals must be unique")
            parsed = [_validate_filter_literal(self.value_type, value) for value in self.values]
            if len(set(parsed)) != len(parsed):
                raise ValueError("Filter IN literals must be semantically unique")
        return self


class TableFilterPredicate(Contract):
    semantics: Literal["kleene-three-valued-v1"] = "kleene-three-valued-v1"
    root_id: Identifier
    nodes: Annotated[list[TableFilterNode], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def bounded_tree(self):
        identities = [node.node_id for node in self.nodes]
        if len(set(identities)) != len(identities):
            raise ValueError("Filter node identifiers must be unique")
        if self.root_id != identities[-1]:
            raise ValueError("Filter root must be the final topological node")
        available: set[str] = set()
        references = {identity: 0 for identity in identities}
        depths: dict[str, int] = {}
        for node in self.nodes:
            if not set(node.arguments) <= available:
                raise ValueError("Filter arguments must reference earlier nodes")
            if len(set(node.arguments)) != len(node.arguments):
                raise ValueError("Filter node arguments must be unique")
            for identity in node.arguments:
                references[identity] += 1
            depths[node.node_id] = (
                1 + max(depths[identity] for identity in node.arguments) if node.arguments else 1
            )
            if depths[node.node_id] > 8:
                raise ValueError("Filter predicate exceeds depth eight")
            available.add(node.node_id)
        if references[self.root_id] != 0 or any(
            count != 1 for identity, count in references.items() if identity != self.root_id
        ):
            raise ValueError("Filter nodes must form one tree rooted at root_id")
        return self

    def validate_fields(self, fields: list[dict]):
        types = {
            item.get("field_id"): item.get("type") for item in fields if isinstance(item, dict)
        }
        if len(types) != len(fields) or any(
            kind not in {"string", "int64", "double", "bool"} for kind in types.values()
        ):
            raise ValueError("Filter input fields are invalid")
        for node in self.nodes:
            if node.field_id is None:
                continue
            kind = types.get(node.field_id)
            if kind is None:
                raise ValueError("Filter references an unknown field")
            if node.kind in {"COMPARE", "IN"} and node.value_type != kind:
                raise ValueError("Filter literal type differs from its field")
            if node.kind in {"IS_NAN", "IS_FINITE"} and kind != "double":
                raise ValueError("NaN and finite predicates require a double field")
        return self


class TableFilterParameters(Contract):
    predicate: TableFilterPredicate


class TableCastField(Contract):
    field_id: Identifier
    target_type: Literal["string", "int64", "double", "bool"]
    on_error: Literal["ERROR", "SET_NULL"]


class TableCastParameters(Contract):
    semantics: Literal["strict-scalar-cast-v1"] = "strict-scalar-cast-v1"
    casts: Annotated[list[TableCastField], Field(min_length=1, max_length=128)]

    @field_validator("casts")
    @classmethod
    def distinct_fields(cls, values):
        if len({value.field_id for value in values}) != len(values):
            raise ValueError("Cast fields must be unique")
        return values

    def validate_fields(self, fields: list[dict]):
        types = {
            item.get("field_id"): item.get("type") for item in fields if isinstance(item, dict)
        }
        if len(types) != len(fields) or any(
            kind not in {"string", "int64", "double", "bool"} for kind in types.values()
        ):
            raise ValueError("Cast input fields are invalid")
        for cast in self.casts:
            source_type = types.get(cast.field_id)
            if source_type is None:
                raise ValueError("Cast references an unknown field")
            if source_type == cast.target_type:
                raise ValueError("Cast source and target types must differ")
        return self


class TableAggregateMeasure(Contract):
    measure_id: Identifier
    operation: Literal[
        "COUNT_ROWS",
        "COUNT_NON_NULL",
        "COUNT_DISTINCT",
        "MIN",
        "MAX",
        "SUM",
        "MEAN",
    ]
    field_id: Identifier | None = None
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value):
        if value is None:
            return value
        if value != value.strip() or unicodedata.normalize("NFC", value) != value:
            raise ValueError("Aggregate display names must be trimmed NFC text")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            raise ValueError("Aggregate display names cannot contain control characters")
        return value

    @model_validator(mode="after")
    def operation_shape(self):
        if (self.operation == "COUNT_ROWS") != (self.field_id is None):
            raise ValueError("COUNT_ROWS must omit field_id and other measures require it")
        return self


class TableAggregateParameters(Contract):
    semantics: Literal["strict-table-aggregate-v1"] = "strict-table-aggregate-v1"
    group_by: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    null_key_policy: Literal["GROUP", "DROP", "ERROR"]
    measures: Annotated[list[TableAggregateMeasure], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_identities(self):
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("Aggregate group fields must be unique")
        if len({value.measure_id for value in self.measures}) != len(self.measures):
            raise ValueError("Aggregate measure identifiers must be unique")
        return self

    def validate_fields(self, fields: list[dict]):
        types = {
            item.get("field_id"): item.get("type") for item in fields if isinstance(item, dict)
        }
        if len(types) != len(fields) or any(
            kind not in {"string", "int64", "double", "bool"} for kind in types.values()
        ):
            raise ValueError("Aggregate input fields are invalid")
        for identity in self.group_by:
            if types.get(identity) not in {"string", "int64", "bool"}:
                raise ValueError("Aggregate group fields must be string, int64 or bool")
        for measure in self.measures:
            if measure.operation == "COUNT_ROWS":
                continue
            source_type = types.get(measure.field_id)
            if source_type is None:
                raise ValueError("Aggregate measure references an unknown field")
            if measure.operation in {"SUM", "MEAN"} and source_type not in {"int64", "double"}:
                raise ValueError("SUM and MEAN require numeric fields")
        return self


class TableExecutionInputs(Contract):
    table: TableSelection


class TableExecutionCreate(Contract):
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    target: OperatorTarget
    inputs: TableExecutionInputs
    parameters: (
        TableProfileParameters
        | TableValidateParameters
        | TableProjectParameters
        | TableFilterParameters
        | TableCastParameters
        | TableAggregateParameters
    )
    research_context: ResearchExecutionContext | None = None

    @model_validator(mode="after")
    def operator_parameters_match(self):
        expected = {
            "table-profile": TableProfileParameters,
            "table-validate": TableValidateParameters,
            "table-project": TableProjectParameters,
            "table-filter": TableFilterParameters,
            "table-cast": TableCastParameters,
            "table-aggregate": TableAggregateParameters,
        }
        model = expected.get(self.target.operator_id)
        if model is None or not isinstance(self.parameters, model):
            raise ValueError("Table operator and parameter contract differ")
        return self


class BufferParameters(Contract):
    distance: Annotated[float, Field(gt=0)]
    unit: Literal["metre", "kilometre"]
    output_crs: Annotated[str, StringConstraints(pattern=r"^EPSG:[1-9][0-9]{0,5}$(?![\s\S])")]
    segments: Annotated[int, Field(ge=1, le=64)] = 8

    @model_validator(mode="after")
    def bounded_distance(self):
        metres = self.distance * (1000 if self.unit == "kilometre" else 1)
        if not math.isfinite(metres) or metres > 10_000_000:
            raise ValueError("Buffer distance exceeds 10,000,000 metres")
        return self


class SpatialSummaryParameters(Contract):
    layer_id: Identifier


class StaticMapParameters(Contract):
    layer_id: Identifier
    template_id: Literal["v7-spatial-basic-v1"] = "v7-spatial-basic-v1"
    output_crs: Annotated[str, StringConstraints(pattern=r"^EPSG:[1-9][0-9]{0,5}$(?![\s\S])")]
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    legend: bool = True
    palette: Literal["blue", "green", "orange"] = "blue"

    @field_validator("title")
    @classmethod
    def safe_title(cls, value):
        if value != value.strip() or unicodedata.normalize("NFC", value) != value:
            raise ValueError("Map title must be trimmed NFC text")
        forbidden = {"Cc", "Cf", "Cs"}
        if any(unicodedata.category(character) in forbidden for character in value):
            raise ValueError("Map title contains control or directional characters")
        return value


class SpatialExecutionInputs(Contract):
    vector: SpatialSelection


class SpatialExecutionCreate(Contract):
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    target: OperatorTarget
    inputs: SpatialExecutionInputs
    parameters: BufferParameters | SpatialSummaryParameters | StaticMapParameters
    research_context: ResearchExecutionContext | None = None

    @model_validator(mode="after")
    def operator_parameters_match(self):
        expected = {
            "qgis-buffer": BufferParameters,
            "spatial-vector-summary": SpatialSummaryParameters,
            "qgis-static-map": StaticMapParameters,
        }
        model = expected.get(self.target.operator_id)
        if model is None or not isinstance(self.parameters, model):
            raise ValueError("Spatial operator and parameter contract differ")
        if self.inputs.vector.selector.kind != "VECTOR_LAYER":
            raise ValueError("Spatial vector operators require a vector layer selection")
        if isinstance(self.parameters, (SpatialSummaryParameters, StaticMapParameters)):
            if self.parameters.layer_id != self.inputs.vector.selector.layer_id:
                raise ValueError("Spatial parameter layer must match the selected layer")
        return self


class OperatorOutputRef(Contract):
    kind: Literal["OPERATOR_OUTPUT"]
    action_id: Identifier
    execution_id: Identifier
    output_port: Identifier
    asset_ref: AssetRef
    representation_id: Identifier
    representation_digest: Sha256Digest
    schema_id: Identifier
    schema_source: Literal["MANIFEST"]


class WorkflowOutputRef(Contract):
    kind: Literal["WORKFLOW_OUTPUT"]
    parent_action_id: Identifier
    parent_execution_id: Identifier
    node_action_id: Identifier
    node_execution_id: Identifier
    call_path: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    scatter_path: Annotated[list[int], Field(max_length=32)] = Field(default_factory=list)
    published_output_port: Identifier
    published_path: Annotated[list[str | int], Field(max_length=32)] = Field(default_factory=list)
    output_port: Identifier
    asset_ref: AssetRef
    representation_id: Identifier
    representation_digest: Sha256Digest
    schema_id: Identifier
    schema_source: Literal["MANIFEST"]


BoundOutputRef = Annotated[OperatorOutputRef | WorkflowOutputRef, Field(discriminator="kind")]


class RoleBinding(Contract):
    role_id: Identifier
    output_ref: BoundOutputRef


class DeliverableCreate(Contract):
    expected_spec_revision: PositiveRevision
    executions: Annotated[list[Identifier], Field(max_length=2)] = Field(default_factory=list)
    workflow_outputs: Annotated[list[WorkflowOutputSelection], Field(max_length=32)] = Field(
        default_factory=list
    )
    role_bindings: Annotated[list[RoleBinding], Field(max_length=64)] = Field(default_factory=list)

    @model_validator(mode="after")
    def selections(self):
        if not self.executions and not self.workflow_outputs and not self.role_bindings:
            raise ValueError("At least one deliverable output is required")
        if self.role_bindings and (self.executions or self.workflow_outputs):
            raise ValueError("Exact role bindings cannot be mixed with legacy selections")
        if len(set(self.executions)) != len(self.executions):
            raise ValueError("Duplicate execution")
        identities = [
            (value.execution_id, value.output, tuple(value.path)) for value in self.workflow_outputs
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate workflow output")
        roles = [value.role_id for value in self.role_bindings]
        if len(set(roles)) != len(roles):
            raise ValueError("Duplicate output role")
        return self


class AcceptanceCreate(Contract):
    expected_spec_revision: PositiveRevision
    deliverable_id: Identifier
    retry_of: Identifier | None = None
    analysis_registration_id: Identifier | None = None


class HumanReviewCreate(Contract):
    expected_spec_revision: PositiveRevision
    coverage_plan_revision: PositiveRevision
    item_id: Identifier
    qualification: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    criteria: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    decision: Literal["PASS", "FAIL"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ExecutionCancel(Contract):
    expected_spec_revision: PositiveRevision


class ColumnType(Contract):
    name: Text
    type: Literal["string", "int64", "float64", "boolean"]


class UploadFinalize(Contract):
    columns: Annotated[list[ColumnType], Field(max_length=128)] = Field(default_factory=list)
    null_values: Annotated[list[str], Field(max_length=16)] = Field(default_factory=lambda: [""])

    @field_validator("columns")
    @classmethod
    def distinct_names(cls, values):
        if len({value.name for value in values}) != len(values):
            raise ValueError("Column names must be unique")
        return values


class SpatialUploadFinalize(Contract):
    kind: Literal["SPATIAL"]
    profile: Literal["strict-spatial-v1"] = "strict-spatial-v1"


class DocumentUploadFinalize(Contract):
    kind: Literal["DOCUMENT"]
    profile: Literal["utf8-text-v1"] = "utf8-text-v1"


class UploadReceipt(Contract):
    upload_id: Identifier
    status: Literal["QUEUED", "IMPORTING", "READY", "FAILED"]
    status_url: str


class AcceptanceReceipt(Contract):
    acceptance_id: Identifier
    status: Literal[
        "QUEUED",
        "PASS",
        "FAIL",
        "ERROR",
        "NOT_EVALUABLE",
        "INDETERMINATE",
        "REVIEW_REQUIRED",
        "CANCELLED",
        "STALE",
    ]
    status_url: str


class UploadView(UploadCreate):
    upload_id: Identifier
    project_id: Identifier
    status: Literal["AWAITING_CONTENT", "UPLOADED", "QUEUED", "IMPORTING", "READY", "FAILED"]
    actor_id: Identifier
    conversion: dict[str, Any] | None = None
    asset: AssetRef | None = None
    error: dict[str, Any] | None = None


class UploadList(Contract):
    items: list[UploadView]
    next_cursor: str | None


class AssetSummary(Contract):
    ref: AssetRef
    manifest: dict[str, Any]


class AssetView(AssetSummary):
    semantic_revisions: list[dict[str, Any]]


class AssetList(Contract):
    items: list[AssetSummary]
    next_cursor: str | None


class SemanticRevisionView(Contract):
    asset_id: Identifier
    version_id: Identifier
    revision: PositiveRevision
    semantics: Semantics | SpatialSemantics


class LocalOperatorImplementationView(Contract):
    implementation_id: Identifier
    binding_kind: Literal["LOCAL"]
    runtime: Literal["NATIVE", "QGIS"]
    implementation_digest: Sha256Digest
    image_digest: Sha256Digest
    protocol: str
    resource_profile: Identifier


class RemoteOperatorImplementationView(Contract):
    implementation_id: Identifier
    binding_kind: Literal["REMOTE"]
    runtime: Literal["REMOTE"]
    implementation_digest: Sha256Digest
    protocol: str
    resource_profile: Identifier
    service_binding_id: Identifier
    binding_revision: PositiveRevision


OperatorImplementationView = Annotated[
    LocalOperatorImplementationView | RemoteOperatorImplementationView,
    Field(discriminator="binding_kind"),
]


class OperatorView(Contract):
    operator_id: OperatorId
    version_id: Literal["1.0.0"]
    operator_digest: Sha256Digest
    ownership: Literal["PLATFORM", "CURATED_PACKAGE", "DOMAIN_PACKAGE", "REGISTERED_REMOTE"]
    trust_level: Literal["BUILTIN", "VERIFIED"]
    lifecycle: Literal["ACTIVE", "DISABLED", "REVOKED"]
    effects: list[Literal["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"]]
    supports_workflow: bool
    implementations: list[OperatorImplementationView]
    # Kept as a projection for 0.9.x clients. New clients use implementations.
    implementation_id: Identifier
    legacy: dict[str, str] | None = None
    runtime: Literal["NATIVE", "QGIS"] = "NATIVE"
    inputs: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


class OperatorList(Contract):
    items: list[OperatorView]


class ExecutionView(Contract):
    action_id: Identifier
    execution_id: Identifier
    project_id: Identifier
    task_id: Identifier
    state: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"]
    revision: PositiveRevision
    executed: bool
    attempt_id: Identifier
    container_id: str | None
    cancel_requested: bool
    request: dict[str, Any]
    runtime: dict[str, Any]


class DeliverableView(Contract):
    deliverable_id: Identifier
    project_id: Identifier
    task_id: Identifier
    spec_revision: PositiveRevision
    digest: str
    payload: dict[str, Any]
    identity_mode: Literal["EXACT"] = "EXACT"


class DeliverableList(Contract):
    items: list[DeliverableView]


class AcceptanceView(Contract):
    acceptance_id: Identifier
    project_id: Identifier
    task_id: Identifier
    spec_revision: PositiveRevision
    deliverable_id: Identifier
    action_id: Identifier | None
    analysis_registration_id: Identifier | None = None
    retry_of: Identifier | None
    status: Literal[
        "QUEUED",
        "PASS",
        "FAIL",
        "ERROR",
        "NOT_EVALUABLE",
        "INDETERMINATE",
        "REVIEW_REQUIRED",
        "CANCELLED",
        "STALE",
    ]
    report: dict[str, Any] | None
    report_digest: str | None
    stale: bool
    completion: dict[str, Any] | None
    coverage_plan: dict[str, Any] | None = None
    human_reviews: list[dict[str, Any]] = Field(default_factory=list)
    check_runs: list[dict[str, Any]] = Field(default_factory=list)


class AcceptanceList(Contract):
    items: list[AcceptanceView]
