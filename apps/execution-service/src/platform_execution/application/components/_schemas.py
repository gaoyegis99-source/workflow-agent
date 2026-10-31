"""Frozen output schema identifiers shared by collection components."""

OUTPUT_SCHEMAS = {
    ("timeseries-completeness", "result"): "p3-result-v1",
    ("statistics-linear-trend", "result"): "p3-result-v1",
    ("timeseries-completeness", "table"): "p3-table-v1",
    ("statistics-linear-trend", "table"): "p3-table-v1",
    ("timeseries-completeness", "method"): "p3-method-v1",
    ("statistics-linear-trend", "method"): "p3-method-v1",
    ("qgis-buffer", "vector"): "spatial-vector-v1",
    ("qgis-buffer", "manifest"): "spatial-manifest-v1",
    ("qgis-buffer", "method"): "spatial-method-v1",
    ("spatial-vector-summary", "summary"): "spatial-summary-v1",
    ("spatial-vector-summary", "method"): "spatial-method-v1",
    ("qgis-static-map", "image"): "cartographic-png-v1",
    ("qgis-static-map", "manifest"): "cartographic-manifest-v1",
    ("qgis-static-map", "method"): "cartographic-method-v1",
    ("table-profile", "report"): "table-profile-report-v1",
    ("table-profile", "table"): "table-profile-table-v1",
    ("table-profile", "method"): "tabular-method-v1",
    ("table-validate", "report"): "table-validation-report-v1",
    ("table-validate", "table"): "table-validation-table-v1",
    ("table-validate", "method"): "tabular-method-v1",
    ("table-project", "report"): "table-project-report-v1",
    ("table-project", "table"): "table-project-table-v1",
    ("table-project", "method"): "tabular-method-v1",
    ("table-filter", "report"): "table-filter-report-v1",
    ("table-filter", "table"): "table-filter-table-v1",
    ("table-filter", "method"): "tabular-method-v1",
    ("table-cast", "report"): "table-cast-report-v1",
    ("table-cast", "table"): "table-cast-table-v1",
    ("table-cast", "method"): "tabular-method-v1",
    ("table-aggregate", "report"): "table-aggregate-report-v1",
    ("table-aggregate", "table"): "table-aggregate-table-v1",
    ("table-aggregate", "method"): "tabular-method-v1",
}

TABULAR_FILE_SCHEMAS = {
    "table-profile": [
        ("field_id", "string"),
        ("source_name", "string"),
        ("physical_type", "string"),
        ("row_count", "int64"),
        ("null_count", "int64"),
        ("non_null_count", "int64"),
        ("distinct_count", "int64"),
        ("usable_count", "int64"),
        ("nonfinite_count", "int64"),
        ("minimum", "string"),
        ("maximum", "string"),
        ("empty_count", "int64"),
        ("min_length", "int64"),
        ("max_length", "int64"),
        ("true_count", "int64"),
        ("false_count", "int64"),
    ],
    "table-validate": [
        ("rule_id", "string"),
        ("kind", "string"),
        ("status", "string"),
        ("checked_count", "int64"),
        ("failed_count", "int64"),
    ],
}


def output_schema(payload, representation_id):
    return OUTPUT_SCHEMAS[(payload["target"]["operator_id"], representation_id)]
