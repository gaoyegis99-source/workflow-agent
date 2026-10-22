"""Durable single-attempt execution, then recoverable publication. No background API tasks."""

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from platform_contracts.science import (
    TableAggregateParameters,
    TableCastParameters,
    TableFilterParameters,
)
from platform_contracts.service import ServiceFault, canonical, ensure

from platform_execution.cartography import verify_png
from platform_execution.trace import record as trace

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


class Runner:
    def __init__(
        self,
        repository,
        root,
        docker,
        data,
        platform,
        workflow=None,
        work_classes=None,
        remote_factory=None,
        table_inspector=None,
    ):
        self.repo, self.root, self.docker = repository, Path(root).resolve(), docker
        self.data, self.platform, self.owner = data, platform, uuid4().hex
        self.workflow = workflow
        self.work_classes = work_classes
        self.remote_factory = remote_factory
        self.table_inspector = table_inspector
        self.lease_seconds, self.heartbeat_seconds = 30, 10
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def save(path, content):
        if path.exists():
            ensure(path.read_bytes() == content, "INVALID_BINDING", "Frozen input changed")
            return
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o444)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def runtime(self, row, **changes):
        runtime = row["runtime"] | changes
        runtime.pop("uncertain", None)
        runtime.pop("failures", None)
        return self.repo.change(row, runtime=runtime)

    @staticmethod
    def workflow_execution(row):
        if row.get("parent_execution_id"):
            return row["parent_execution_id"]
        if row.get("payload", {}).get("kind") == "WORKFLOW":
            return row["execution_id"]
        return None

    def trace(self, row, event_type, discriminator, *, duration_ms=None, attributes=None):
        workflow = self.workflow_execution(row)
        if workflow is None:
            return None
        epoch = (
            row["payload"].get("node_request", {}).get("epoch")
            if row.get("parent_execution_id")
            else row["runtime"].get("engine_epoch")
        )
        return trace(
            self.repo,
            workflow_execution_id=workflow,
            node_action_id=row["action_id"] if row.get("parent_execution_id") else None,
            engine_epoch=epoch,
            worker_generation=row.get("generation"),
            source="EXECUTION_WORKER",
            event_type=event_type,
            discriminator=discriminator,
            source_monotonic_ns=time.monotonic_ns(),
            duration_ms=duration_ms,
            attributes=attributes,
        )

    def permit(self, row):
        started = time.perf_counter_ns()
        self.trace(
            row,
            "PERMIT_REQUESTED",
            [row["generation"], row["runtime"]["phase"]],
            attributes={"phase": row["runtime"]["phase"]},
        )
        if row.get("parent_execution_id"):
            payload = row["payload"]
            result = self.platform.json(
                "POST",
                f"/internal/workflows/{payload['parent_action_id']}/"
                f"{payload['parent_execution_id']}/nodes/{row['action_id']}/permit",
                json=payload["node_request"]
                | {"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
            )
        else:
            result = self.platform.json(
                "POST",
                "/internal/actions/" + row["action_id"] + "/permit",
                json={"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
            )
        event = {
            "ALLOW": "PERMIT_ALLOWED",
            "WAIT": "PERMIT_WAIT",
        }.get(result["decision"], "PERMIT_DENIED")
        self.trace(
            row,
            event,
            [row["generation"], row["runtime"]["phase"], result["decision"]],
            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
            attributes={"decision": result["decision"]},
        )
        return result

    def prepare_input(self, row, root):
        payload = row["payload"]
        profile = row["runtime"].get("implementation_profile", {})
        protocol = profile.get("protocol", "P3_TABLE")
        if protocol == "CAPABILITY_CHECK":
            plan = payload.get("coverage_plan", {})
            run = payload.get("checker_run", {})
            ensure(
                plan.get("schema_version") == 3
                and run.get("run_id")
                and run.get("capability_id") == profile.get("implementation_id")
                and run.get("implementation_id") == profile.get("implementation_id")
                and run.get("image_digest") == profile.get("image_id")
                and run.get("catalog_digest") == plan.get("checker_catalog"),
                "INVALID_BINDING",
                "Checker capability input differs from the frozen plan",
            )
            artifacts = payload.get("artifacts", [])
            bindings = {
                value["role_id"]: value["output_ref"] for value in payload.get("role_bindings", [])
            }
            index = {"roles": {}}
            for role in run.get("input_roles", []):
                ref = bindings.get(role)
                ensure(ref is not None, "INVALID_BINDING", "Checker input role is unbound")
                artifact = next(
                    (value for value in artifacts if value.get("ref") == ref.get("asset_ref")), None
                )
                ensure(artifact is not None, "INVALID_BINDING", "Checker artifact is absent")
                manifest = artifact.get("manifest", {})
                representations = manifest.get("representations", [])
                selected = next(
                    (
                        value
                        for value in representations
                        if value.get("representation_id") == ref.get("representation_id")
                    ),
                    None,
                )
                ensure(
                    selected is not None
                    and selected.get("digest") == ref.get("representation_digest")
                    and selected.get("schema_id") == ref.get("schema_id"),
                    "INVALID_BINDING",
                    "Checker role representation differs",
                )
                role_root = root / "input" / "roles" / role
                role_root.mkdir(parents=True, exist_ok=True)
                files = {}
                for representation in representations:
                    identity = representation.get("representation_id")
                    ensure(
                        isinstance(identity, str)
                        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity),
                        "INVALID_BINDING",
                        "Invalid checker representation identity",
                    )
                    asset = ref["asset_ref"]
                    path = (
                        f"/internal/projects/{row['project_id']}/assets/{asset['asset_id']}"
                        f"/versions/{asset['version_id']}/representations/{identity}/content"
                    )
                    content = self.data.request("GET", path).content
                    ensure(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                        == representation.get("digest"),
                        "INVALID_BINDING",
                        "Checker artifact content digest mismatch",
                    )
                    filename = identity + ".bin"
                    self.save(role_root / filename, content)
                    files[identity] = {
                        "path": f"roles/{role}/{filename}",
                        "digest": representation["digest"],
                        "format": representation.get("format"),
                        "schema_id": representation.get("schema_id"),
                    }
                role_index = {"output_ref": ref, "manifest": manifest, "files": files}
                if run["capability_id"] == "p3-timeseries-reproduction-v1":
                    source = manifest.get("source", {}).get("input")
                    ensure(
                        isinstance(source, dict)
                        and isinstance(source.get("asset"), dict)
                        and source.get("representation_id"),
                        "INVALID_BINDING",
                        "P3 result lacks its exact normalized input",
                    )
                    source_asset = source["asset"]
                    source_path = (
                        f"/internal/projects/{row['project_id']}/assets/"
                        f"{source_asset['asset_id']}/versions/{source_asset['version_id']}"
                        f"/representations/{source['representation_id']}/content"
                    )
                    content = self.data.request("GET", source_path).content
                    ensure(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                        == source.get("representation_digest"),
                        "INVALID_BINDING",
                        "P3 checker source digest mismatch",
                    )
                    self.save(role_root / "observations.parquet", content)
                    role_index["source"] = {
                        "path": f"roles/{role}/observations.parquet",
                        "selection": source,
                    }
                if run["capability_id"] in {
                    "tabular-profile-reproduction-v1",
                    "tabular-validation-reproduction-v1",
                    "tabular-project-reproduction-v1",
                    "tabular-filter-reproduction-v1",
                    "tabular-cast-reproduction-v1",
                    "tabular-aggregate-reproduction-v1",
                }:
                    selection = manifest.get("source", {}).get("inputs", {}).get("table")
                    expected_digest = (
                        manifest.get("source", {})
                        .get("provenance", {})
                        .get("input_digests", {})
                        .get("table")
                    )
                    ensure(
                        isinstance(selection, dict)
                        and isinstance(expected_digest, str)
                        and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest),
                        "INVALID_BINDING",
                        "Tabular result lacks its exact source identity",
                    )
                    source_asset = None
                    if isinstance(selection.get("asset"), dict):
                        asset = selection["asset"]
                        source_asset = self.data.json(
                            "GET",
                            f"/internal/projects/{row['project_id']}/assets/{asset['asset_id']}"
                            f"/versions/{asset['version_id']}",
                        )
                        source_representation = next(
                            (
                                value
                                for value in source_asset.get("manifest", {}).get(
                                    "representations", []
                                )
                                if value.get("representation_id")
                                == selection.get("representation_id")
                            ),
                            None,
                        )
                        ensure(
                            source_asset.get("ref") == asset
                            and source_representation is not None
                            and source_representation.get("format") == "parquet"
                            and source_representation.get("digest") == expected_digest
                            and selection.get("representation_digest") == expected_digest,
                            "INVALID_BINDING",
                            "Tabular checker source asset differs",
                        )
                        content = self.data.request(
                            "GET",
                            f"/internal/projects/{row['project_id']}/assets/{asset['asset_id']}"
                            f"/versions/{asset['version_id']}/representations/"
                            f"{selection['representation_id']}/content",
                        ).content
                    else:
                        derived = selection.get("derived_from")
                        output_ref = ref
                        ensure(
                            isinstance(derived, dict)
                            and output_ref.get("kind") == "WORKFLOW_OUTPUT"
                            and derived.get("execution_id")
                            and derived.get("node_id")
                            and derived.get("representation_id"),
                            "INVALID_BINDING",
                            "Tabular checker derived source is incomplete",
                        )
                        source_row = self.repo.get(
                            execution=derived["execution_id"], project=row["project_id"]
                        )
                        source_runtime = source_row.get("runtime", {})
                        source_file = next(
                            (
                                value
                                for value in source_runtime.get("files", [])
                                if value.get("representation_id") == derived["representation_id"]
                            ),
                            None,
                        )
                        ensure(
                            source_row.get("action_id") == derived["node_id"]
                            and source_row.get("parent_execution_id")
                            == output_ref.get("parent_execution_id")
                            and source_row.get("state") == "SUCCEEDED"
                            and source_runtime.get("publication") == "PRIVATE"
                            and source_file is not None
                            and source_file.get("format") == "parquet"
                            and source_file.get("digest") == expected_digest,
                            "INVALID_BINDING",
                            "Tabular checker private source ledger differs",
                        )
                        source_path = (
                            self.root
                            / source_row["execution_id"]
                            / "output"
                            / source_file["filename"]
                        ).resolve()
                        ensure(
                            source_path.is_relative_to(self.root)
                            and source_path.is_file()
                            and not source_path.is_symlink(),
                            "INVALID_BINDING",
                            "Tabular checker private source is unavailable",
                        )
                        content = source_path.read_bytes()
                        source_asset = {
                            "manifest": source_runtime.get("manifest", {}).get(
                                "asset_descriptor", {}
                            )
                        }
                    ensure(
                        "sha256:" + hashlib.sha256(content).hexdigest() == expected_digest,
                        "INVALID_BINDING",
                        "Tabular checker source content differs",
                    )
                    self.save(role_root / "source-table.parquet", content)
                    role_index["source_table"] = {
                        "path": f"roles/{role}/source-table.parquet",
                        "digest": expected_digest,
                        "selection": selection,
                        "manifest": source_asset["manifest"],
                    }
                if run["capability_id"] == "cartographic-product-integrity-v1":
                    selection = manifest.get("source", {}).get("inputs", {}).get("vector")
                    ensure(
                        isinstance(selection, dict)
                        and isinstance(selection.get("asset"), dict)
                        and selection.get("representation_id")
                        and selection.get("representation_digest"),
                        "INVALID_BINDING",
                        "Cartographic result lacks its exact spatial source",
                    )
                    asset = selection["asset"]
                    source_asset = self.data.json(
                        "GET",
                        f"/internal/projects/{row['project_id']}/assets/{asset['asset_id']}"
                        f"/versions/{asset['version_id']}",
                    )
                    source_representation = next(
                        (
                            value
                            for value in source_asset.get("manifest", {}).get("representations", [])
                            if value.get("representation_id") == selection["representation_id"]
                        ),
                        None,
                    )
                    ensure(
                        source_asset.get("ref") == asset
                        and source_representation is not None
                        and source_representation.get("digest")
                        == selection["representation_digest"],
                        "INVALID_BINDING",
                        "Cartographic source asset differs",
                    )
                    role_index["source_spatial"] = {
                        "selection": selection,
                        "manifest": source_asset["manifest"],
                    }
                index["roles"][role] = role_index
            self.save(root / "input/input-index.json", canonical(index))
            self.save(root / "input/request.json", canonical(payload))
            return self.runtime(row, phase="PREPARED")
        if protocol == "OPEN_SCIENCE_CHECK":
            ensure(
                payload.get("acceptance_spec", {}).get("profile") == "open-science-v1"
                and isinstance(payload.get("coverage_plan"), dict),
                "INVALID_BINDING",
                "Open checker requires a frozen coverage plan",
            )
            self.save(root / "input/request.json", canonical(payload))
            return self.runtime(row, phase="PREPARED")
        bindings = payload.get("bindings") or {"observations": payload["binding"]}
        expected_ports = (
            {"observations"}
            if protocol.startswith("P3_")
            else ({"table"} if protocol == "TABULAR_V1" else {"vector"})
        )
        ensure(
            set(bindings) == expected_ports,
            "INVALID_BINDING",
            "Implementation input ports differ",
        )
        if payload.get("target", {}).get("operator_id") == "table-filter":
            try:
                table_description = bindings["table"].get(
                    "table", bindings["table"].get("manifest", {})
                )
                if isinstance(table_description.get("tabular"), dict):
                    table_description = table_description["tabular"]
                TableFilterParameters.model_validate(
                    payload["parameters"]
                ).predicate.validate_fields(table_description["fields"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ServiceFault(
                    "INVALID_BINDING",
                    "Filter predicate differs from the staged table schema",
                ) from exc
        if payload.get("target", {}).get("operator_id") == "table-cast":
            try:
                table_description = bindings["table"].get(
                    "table", bindings["table"].get("manifest", {})
                )
                if isinstance(table_description.get("tabular"), dict):
                    table_description = table_description["tabular"]
                TableCastParameters.model_validate(payload["parameters"]).validate_fields(
                    table_description["fields"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ServiceFault(
                    "INVALID_BINDING",
                    "Cast request differs from the staged table schema",
                ) from exc
        if payload.get("target", {}).get("operator_id") == "table-aggregate":
            try:
                table_description = bindings["table"].get(
                    "table", bindings["table"].get("manifest", {})
                )
                if isinstance(table_description.get("tabular"), dict):
                    table_description = table_description["tabular"]
                TableAggregateParameters.model_validate(payload["parameters"]).validate_fields(
                    table_description["fields"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ServiceFault(
                    "INVALID_BINDING",
                    "Aggregate request differs from the staged table schema",
                ) from exc
        for port, binding in bindings.items():
            if source := binding.get("private_source"):
                ensure(
                    row.get("parent_execution_id")
                    and source["node_id"] != row["action_id"]
                    and source["execution_id"] != row["execution_id"],
                    "INVALID_BINDING",
                    "Invalid private workflow input source",
                )
                path = (
                    self.root / source["execution_id"] / "output" / source["filename"]
                ).resolve()
                ensure(
                    path.is_relative_to(self.root) and path.is_file() and not path.is_symlink(),
                    "INVALID_BINDING",
                    "Private workflow input is unavailable",
                )
                content = path.read_bytes()
            else:
                selection = binding["selection"]
                ref = selection["asset"]
                path = (
                    f"/internal/projects/{row['project_id']}/assets/{ref['asset_id']}/versions/"
                    f"{ref['version_id']}/representations/{selection['representation_id']}/content"
                )
                content = self.data.request("GET", path).content
            ensure(
                "sha256:" + hashlib.sha256(content).hexdigest()
                == binding["representation"]["digest"],
                "INVALID_BINDING",
                "Input content digest mismatch",
            )
            filename = {
                "observations": "observations.parquet",
                "vector": "vector.gpkg",
                "table": "table.parquet",
            }[port]
            self.save(root / "input" / filename, content)
        if (
            payload.get("kind") == "CHECK"
            and payload.get("acceptance_spec", {}).get("profile") == "p3-timeseries-v1"
        ):
            for index, artifact in enumerate(payload["artifacts"]):
                ref = artifact["ref"]
                base = (
                    f"/internal/projects/{row['project_id']}/assets/{ref['asset_id']}"
                    f"/versions/{ref['version_id']}/representations/"
                )
                for rep in artifact["manifest"]["representations"]:
                    data = self.data.request(
                        "GET", base + rep["representation_id"] + "/content"
                    ).content
                    ensure(
                        "sha256:" + hashlib.sha256(data).hexdigest() == rep["digest"],
                        "INVALID_BINDING",
                        "Candidate content digest mismatch",
                    )
                    extension = {"result": "json", "table": "parquet", "method": "method.json"}[
                        rep["representation_id"]
                    ]
                    self.save(root / "input" / f"candidate-{index}.{extension}", data)
        self.save(root / "input/request.json", canonical(payload))
        return self.runtime(row, phase="PREPARED")

    def collect_tabular(self, row, root):
        output = root / "output"
        profile = row["runtime"]["implementation_profile"]
        expected = profile["outputs"]
        names = {path.name for path in output.iterdir() if path.name != "started.marker"}
        ensure(
            names == set(expected.values())
            and all(
                (output / name).is_file() and not (output / name).is_symlink() for name in names
            ),
            "INVALID_BINDING",
            "Tabular output set differs",
        )
        report = json.loads((output / expected["report"]).read_text())
        method = json.loads((output / expected["method"]).read_text())
        ensure(
            self.table_inspector is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Parquet output inspector is unavailable",
            503,
        )
        payload = row["payload"]
        operator = payload["target"]["operator_id"]
        table = self.table_inspector.inspect(
            output / expected["table"],
            rows=operator not in {"table-project", "table-filter", "table-cast"},
        )
        binding = payload["bindings"]["table"]
        file_schema = table["schema"]
        table_description = binding.get("table", binding.get("manifest", {}))
        if isinstance(table_description.get("tabular"), dict):
            table_description = table_description["tabular"]
        source_fields = {item["field_id"]: item for item in table_description.get("fields", [])}
        expected_method = {
            "schema": report.get("schema"),
            "operator_id": operator,
            "version_id": "1.0.0",
            "source_digest": binding["representation"]["digest"],
            "method": "deterministic-pyarrow-v1",
        }
        if operator == "table-filter":
            expected_method["predicate_semantics"] = "kleene-three-valued-v1"
        elif operator == "table-cast":
            expected_method["cast_semantics"] = "strict-scalar-cast-v1"
        elif operator == "table-aggregate":
            expected_method |= {
                "aggregation_semantics": "strict-table-aggregate-v1",
                "numeric_algorithm": "int64-checked-decimal-mean-and-fsum-v1",
                "group_order": "first-source-occurrence-v1",
                "null_measure_policy": "ignore-v1",
            }
        common_valid = bool(
            report.get("operator_id") == operator
            and report.get("version_id") == "1.0.0"
            and report.get("source_digest") == binding["representation"]["digest"]
            and report.get("parameters") == payload["parameters"]
            and method == expected_method
        )
        if operator == "table-project":
            requested = payload["parameters"]["fields"]
            raw_mappings = report.get("fields", [])
            mappings = raw_mappings if isinstance(raw_mappings, list) else []
            mapping_shape_valid = bool(
                isinstance(raw_mappings, list)
                and all(
                    isinstance(item, dict)
                    and {
                        "ordinal",
                        "output_field_id",
                        "source_field_id",
                        "source_name",
                        "output_name",
                        "physical_type",
                    }
                    == set(item)
                    for item in mappings
                )
            )
            fields = [
                {
                    "field_id": item["output_field_id"],
                    "name": item["output_name"],
                    "type": item["physical_type"],
                    "source_field_id": item["source_field_id"],
                }
                for item in mappings
                if isinstance(item, dict)
                and {
                    "ordinal",
                    "output_field_id",
                    "source_field_id",
                    "source_name",
                    "output_name",
                    "physical_type",
                }
                == set(item)
            ]
            project_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "field_count",
                    "fields",
                }
                and report.get("schema") == "table-project-v1"
                and report.get("row_count") == table["row_count"]
                and report.get("field_count") == len(requested) == len(mappings)
                and mapping_shape_valid
                and file_schema
                == [(item["output_field_id"], item["physical_type"]) for item in mappings]
                and all(
                    item["ordinal"] == ordinal
                    and item["output_field_id"] == f"f{ordinal}"
                    and item["source_field_id"] == selected["source_field_id"]
                    and item["source_field_id"] in source_fields
                    and item["source_name"]
                    == source_fields[item["source_field_id"]].get("name", item["source_field_id"])
                    and item["output_name"] == (selected.get("display_name") or item["source_name"])
                    and item["physical_type"] == source_fields[item["source_field_id"]]["type"]
                    for ordinal, (selected, item) in enumerate(
                        zip(requested, mappings, strict=True)
                    )
                )
            )
            ensure(
                common_valid and project_valid,
                "INVALID_BINDING",
                "Projected table differs from its frozen request",
            )
        elif operator == "table-filter":
            source_field_list = table_description.get("fields", [])
            predicate_digest = (
                "sha256:"
                + hashlib.sha256(canonical(payload["parameters"]["predicate"])).hexdigest()
            )
            counts = [
                report.get("true_count"),
                report.get("false_count"),
                report.get("unknown_count"),
            ]
            input_rows = table_description.get("row_count")
            filter_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "predicate_digest",
                    "input_row_count",
                    "output_row_count",
                    "dropped_row_count",
                    "true_count",
                    "false_count",
                    "unknown_count",
                }
                and report.get("schema") == "table-filter-v1"
                and report.get("predicate_digest") == predicate_digest
                and type(input_rows) is int
                and report.get("input_row_count") == input_rows
                and report.get("output_row_count") == table["row_count"]
                and report.get("dropped_row_count") == input_rows - table["row_count"]
                and all(type(value) is int and value >= 0 for value in counts)
                and sum(counts) == input_rows
                and report.get("true_count") == table["row_count"]
                and file_schema
                == [(item.get("field_id"), item.get("type")) for item in source_field_list]
            )
            ensure(
                common_valid and filter_valid,
                "INVALID_BINDING",
                "Filtered table differs from its frozen request",
            )
            fields = [dict(item) for item in source_field_list]
        elif operator == "table-cast":
            source_field_list = table_description.get("fields", [])
            requested = payload["parameters"]["casts"]
            expected_fields = [dict(item) for item in source_field_list]
            expected_by_id = {item["field_id"]: item for item in expected_fields}
            for item in requested:
                expected_by_id[item["field_id"]]["type"] = item["target_type"]
            raw_summaries = report.get("casts")
            summaries = raw_summaries if isinstance(raw_summaries, list) else []
            cast_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "cast_count",
                    "casts",
                }
                and report.get("schema") == "table-cast-v1"
                and report.get("row_count")
                == table_description.get("row_count")
                == table["row_count"]
                and report.get("cast_count") == len(requested) == len(summaries)
                and file_schema
                == [(item.get("field_id"), item.get("type")) for item in expected_fields]
                and all(
                    isinstance(summary, dict)
                    and set(summary)
                    == {
                        "field_id",
                        "source_type",
                        "target_type",
                        "on_error",
                        "input_null_count",
                        "converted_count",
                        "failure_count",
                        "output_null_count",
                    }
                    and summary["field_id"] == selected["field_id"]
                    and summary["source_type"] == source_fields[selected["field_id"]]["type"]
                    and summary["target_type"] == selected["target_type"]
                    and summary["on_error"] == selected["on_error"]
                    and all(
                        type(summary[key]) is int and summary[key] >= 0
                        for key in (
                            "input_null_count",
                            "converted_count",
                            "failure_count",
                            "output_null_count",
                        )
                    )
                    and summary["input_null_count"]
                    + summary["converted_count"]
                    + summary["failure_count"]
                    == table["row_count"]
                    and summary["output_null_count"]
                    == summary["input_null_count"] + summary["failure_count"]
                    and (selected["on_error"] != "ERROR" or summary["failure_count"] == 0)
                    and table["null_counts"].get(selected["field_id"])
                    == summary["output_null_count"]
                    for selected, summary in zip(requested, summaries, strict=True)
                )
            )
            ensure(
                common_valid and cast_valid,
                "INVALID_BINDING",
                "Cast table differs from its frozen request",
            )
            fields = expected_fields
        elif operator == "table-aggregate":
            parameters = payload["parameters"]
            group_by, requested = parameters["group_by"], parameters["measures"]
            raw_fields, raw_measures = report.get("fields"), report.get("measures")
            fields = raw_fields if isinstance(raw_fields, list) else []
            summaries = raw_measures if isinstance(raw_measures, list) else []
            output_types = []
            expected_fields = []
            for ordinal, identity in enumerate(group_by):
                source = source_fields.get(identity, {})
                expected_fields.append(
                    {
                        "field_id": f"f{ordinal}",
                        "name": source.get("name", identity),
                        "type": source.get("type"),
                        "source_kind": "GROUP_KEY",
                        "source_field_id": identity,
                    }
                )
                output_types.append(source.get("type"))
            expected_summaries = []
            for index, item in enumerate(requested):
                ordinal = len(group_by) + index
                field_id, operation = item.get("field_id"), item["operation"]
                source_type = source_fields.get(field_id, {}).get("type")
                output_type = (
                    "int64"
                    if operation in {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT"}
                    else "double"
                    if operation == "MEAN"
                    else source_type
                )
                expected_fields.append(
                    {
                        "field_id": f"f{ordinal}",
                        "name": item.get("display_name") or item["measure_id"],
                        "type": output_type,
                        "source_kind": "MEASURE",
                        "source_field_id": field_id,
                        "measure_id": item["measure_id"],
                        "operation": operation,
                    }
                )
                output_types.append(output_type)
                expected_summaries.append(
                    {
                        "measure_id": item["measure_id"],
                        "operation": operation,
                        "source_field_id": field_id,
                        "source_type": source_type,
                        "output_field_id": f"f{ordinal}",
                        "output_type": output_type,
                    }
                )
            input_rows = table_description.get("row_count")
            counts = [
                report.get("input_row_count"),
                report.get("participating_row_count"),
                report.get("dropped_key_row_count"),
                report.get("output_row_count"),
                report.get("group_count"),
                report.get("field_count"),
                report.get("measure_count"),
            ]
            aggregate_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "input_row_count",
                    "participating_row_count",
                    "dropped_key_row_count",
                    "group_count",
                    "output_row_count",
                    "field_count",
                    "fields",
                    "measure_count",
                    "measures",
                }
                and report.get("schema") == "table-aggregate-v1"
                and all(type(value) is int and value >= 0 for value in counts)
                and report.get("input_row_count") == input_rows
                and report.get("participating_row_count") + report.get("dropped_key_row_count")
                == input_rows
                and report.get("group_count")
                == report.get("output_row_count")
                == table["row_count"]
                and table["row_count"] <= max(1, input_rows)
                and report.get("field_count")
                == len(fields)
                == len(file_schema)
                == len(group_by) + len(requested)
                and report.get("measure_count") == len(summaries) == len(requested)
                and fields == expected_fields
                and file_schema
                == [
                    (f"f{index}", physical_type) for index, physical_type in enumerate(output_types)
                ]
                and all(
                    isinstance(summary, dict)
                    and set(summary)
                    == set(expected) | {"input_value_count", "null_value_count", "nonfinite_count"}
                    and all(summary[key] == value for key, value in expected.items())
                    and all(
                        type(summary[key]) is int and summary[key] >= 0
                        for key in ("input_value_count", "null_value_count", "nonfinite_count")
                    )
                    and summary["input_value_count"] + summary["null_value_count"]
                    == report["participating_row_count"]
                    and summary["nonfinite_count"] == 0
                    for summary, expected in zip(summaries, expected_summaries, strict=True)
                )
                and all(
                    not isinstance(value, float) or math.isfinite(value)
                    for result_row in table["rows"]
                    for value in result_row.values()
                    if value is not None
                )
            )
            ensure(
                common_valid and aggregate_valid,
                "INVALID_BINDING",
                "Aggregate table differs from its frozen request",
            )
        else:
            expected_rows = (
                payload["parameters"]["fields"]
                if operator == "table-profile"
                else [item["rule_id"] for item in payload["parameters"]["rules"]]
            )
            rules = {item["rule_id"]: item for item in payload["parameters"].get("rules", [])}
            ensure(
                common_valid
                and set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "rows",
                }
                and file_schema == TABULAR_FILE_SCHEMAS.get(operator)
                and report.get("row_count") == table["row_count"]
                and report.get("rows") == table["rows"]
                and table["row_count"] == len(expected_rows)
                and [
                    item["field_id"] if operator == "table-profile" else item["rule_id"]
                    for item in report["rows"]
                ]
                == expected_rows
                and (
                    operator != "table-validate"
                    or all(
                        item["status"] == "PASS"
                        and item["failed_count"] == 0
                        and rules[item["rule_id"]]["kind"] == item["kind"]
                        for item in report["rows"]
                    )
                )
                and (
                    operator != "table-profile"
                    or all(
                        item["field_id"] in source_fields
                        and item["source_name"]
                        == source_fields[item["field_id"]].get("name", item["field_id"])
                        and item["physical_type"] == source_fields[item["field_id"]]["type"]
                        for item in report["rows"]
                    )
                )
                and report.get("schema")
                == ("table-profile-v1" if operator == "table-profile" else "table-validation-v1"),
                "INVALID_BINDING",
                "Tabular output differs from its frozen request",
            )
            fields = [
                {"field_id": name, "name": name, "type": physical_type}
                for name, physical_type in file_schema
            ]
        ensure(
            0 < len(fields) <= 128
            and all(field["type"] in {"string", "int64", "double", "bool"} for field in fields),
            "INVALID_BINDING",
            "Tabular output schema is unsupported",
        )
        formats = {"report": "json", "table": "parquet", "method": "json"}
        files = []
        for identity, filename in expected.items():
            content = (output / filename).read_bytes()
            files.append(
                {
                    "representation_id": identity,
                    "filename": filename,
                    "format": formats[identity],
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "schema_id": output_schema(payload, identity),
                }
            )
        ensure(
            sum(item["size"] for item in files) <= 100 * 1024 * 1024,
            "INVALID_BINDING",
            "Tabular output exceeds limit",
        )
        manifest = {
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": payload["expected_spec_revision"],
            },
            "target": payload["target"],
            "inputs": {"table": binding["selection"]},
            "parameters": payload["parameters"],
            "semantics": {"table": None},
            "files": files,
            "asset_descriptor": {
                "schema_version": 1,
                "kind": "OPERATOR_RESULT",
                "tabular": {"fields": fields, "row_count": table["row_count"]},
            },
            "provenance": {
                "implementation_id": profile["implementation_id"],
                "image_id": profile["image_id"],
                **{
                    key: profile[key]
                    for key in (
                        "catalog_digest",
                        "operator_digest",
                        "implementation_digest",
                        "ownership",
                        "trust_level",
                    )
                    if key in profile
                },
                "input_digests": {"table": binding["representation"]["digest"]},
            },
        }
        if row.get("parent_execution_id"):
            return self.runtime(
                row,
                phase="REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=files,
                manifest=manifest,
            )
        return self.runtime(row, phase="PUBLISHING", manifest=manifest)

    @staticmethod
    def _spatial_descriptor(value):
        ensure(
            isinstance(value, dict)
            and value.get("schema_version") == 1
            and value.get("kind") == "SPATIAL_VECTOR"
            and value.get("driver") == "GPKG"
            and isinstance(value.get("spatial", {}).get("layers"), list)
            and 0 < len(value["spatial"]["layers"]) <= 64,
            "INVALID_BINDING",
            "Invalid spatial output descriptor",
        )
        for layer in value["spatial"]["layers"]:
            ensure(
                isinstance(layer.get("layer_id"), str)
                and isinstance(layer.get("name"), str)
                and type(layer.get("feature_count")) is int
                and layer["feature_count"] >= 0
                and layer.get("geometry_valid") is True
                and isinstance(layer.get("crs", {}).get("wkt2"), str),
                "INVALID_BINDING",
                "Invalid spatial layer descriptor",
            )
        return value

    def collect_spatial(self, row, root):
        output = root / "output"
        profile = row["runtime"]["implementation_profile"]
        expected = profile["outputs"]
        names = {path.name for path in output.iterdir() if path.name != "started.marker"}
        ensure(
            names == set(expected.values())
            and all(
                (output / name).is_file() and not (output / name).is_symlink() for name in names
            ),
            "INVALID_BINDING",
            "Spatial output set differs",
        )
        formats = {
            "vector": "geopackage",
            "manifest": "json",
            "method": "json",
            "summary": "json",
            "image": "png",
        }
        files = []
        payload = row["payload"]
        for identity, filename in expected.items():
            content = (output / filename).read_bytes()
            files.append(
                {
                    "representation_id": identity,
                    "filename": filename,
                    "format": formats[identity],
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "schema_id": output_schema(payload, identity),
                }
            )
        ensure(
            sum(item["size"] for item in files) <= 100 * 1024 * 1024,
            "INVALID_BINDING",
            "Spatial output exceeds limit",
        )
        file_by_id = {item["representation_id"]: item for item in files}
        bindings = payload["bindings"]
        if profile["protocol"] == "SPATIAL_VECTOR":
            descriptor = self._spatial_descriptor(
                json.loads((output / expected["manifest"]).read_text())
            )
            vector = file_by_id["vector"]
            layer = descriptor["spatial"]["layers"][0]
            ensure(
                descriptor.get("source", {}).get("digest") == vector["digest"]
                and descriptor["source"].get("size") == vector["size"]
                and len(descriptor["spatial"]["layers"]) == 1
                and layer["name"] == "buffer"
                and layer["crs"].get("projected") is True
                and layer["crs"].get("linear_unit") == "metre",
                "INVALID_BINDING",
                "Spatial output does not match its checked descriptor",
            )
        elif profile["protocol"] == "SPATIAL_SUMMARY":
            summary = json.loads((output / expected["summary"]).read_text())
            method = json.loads((output / expected["method"]).read_text())
            vector_binding = bindings["vector"]
            layer_id = payload["parameters"]["layer_id"]
            expected_layer = next(
                (
                    item
                    for item in vector_binding["manifest"].get("spatial", {}).get("layers", [])
                    if item.get("layer_id") == layer_id
                ),
                None,
            )
            ensure(
                summary.get("schema_version") == 1
                and summary.get("operator_id") == "spatial-vector-summary"
                and expected_layer is not None
                and summary.get("layer") == expected_layer
                and summary.get("source_digest") == vector_binding["representation"]["digest"]
                and method
                == {
                    "schema_version": 1,
                    "operator_id": "spatial-vector-summary",
                    "version_id": "1.0.0",
                    "method": "GDAL/OGR full geometry inspection",
                },
                "INVALID_BINDING",
                "Invalid spatial summary",
            )
            descriptor = {"kind": "SPATIAL_SUMMARY", "summary": summary}
        elif profile["protocol"] == "CARTOGRAPHIC_PNG":
            map_manifest = json.loads((output / expected["manifest"]).read_text())
            method = json.loads((output / expected["method"]).read_text())
            image_content = (output / expected["image"]).read_bytes()
            facts = verify_png(
                image_content,
                payload["parameters"]["palette"],
                payload["parameters"]["legend"],
            )
            vector_binding = bindings["vector"]
            parameters = payload["parameters"]
            template = map_manifest.get("template", {})
            cartography = map_manifest.get("cartography", {})
            ensure(
                map_manifest.get("schema_version") == 1
                and map_manifest.get("kind") == "CARTOGRAPHIC_PRODUCT"
                and map_manifest.get("format") == "PNG"
                and map_manifest.get("source")
                == {
                    "digest": vector_binding["representation"]["digest"],
                    "layer_id": parameters["layer_id"],
                }
                and template.get("template_id") == parameters["template_id"]
                and isinstance(template.get("template_digest"), str)
                and template["template_digest"].startswith("sha256:")
                and len(template["template_digest"]) == 71
                and isinstance(template.get("resources", {}).get("fonts"), dict)
                and cartography.get("output_crs") == parameters["output_crs"]
                and cartography.get("coordinate_order") == "xy"
                and cartography.get("title") == parameters["title"]
                and cartography.get("legend") is parameters["legend"]
                and cartography.get("palette") == parameters["palette"]
                and cartography.get("page")
                == {
                    "width_mm": 297,
                    "height_mm": 210,
                    "dpi": 150,
                    "pixel_width": 1754,
                    "pixel_height": 1240,
                }
                and map_manifest.get("image")
                == {
                    "digest": facts["digest"],
                    "size": facts["size"],
                    "media_type": "image/png",
                    "width": facts["width"],
                    "height": facts["height"],
                }
                and method.get("schema_version") == 1
                and method.get("operator_id") == "qgis-static-map"
                and method.get("version_id") == "1.0.0"
                and method.get("template_id") == parameters["template_id"]
                and method.get("template_digest") == template["template_digest"]
                and method.get("parameters") == parameters
                and method.get("source_digest") == vector_binding["representation"]["digest"],
                "INVALID_BINDING",
                "Cartographic product does not match its frozen request",
            )
            extent = cartography.get("extent", {})
            ensure(
                set(extent) == {"min_x", "min_y", "max_x", "max_y"}
                and all(type(value) in {int, float} for value in extent.values())
                and all(math.isfinite(value) for value in extent.values())
                and extent["min_x"] < extent["max_x"]
                and extent["min_y"] < extent["max_y"],
                "INVALID_BINDING",
                "Cartographic extent is invalid",
            )
            descriptor = {
                "kind": "CARTOGRAPHIC_PRODUCT",
                "cartography": cartography,
                "template": template,
                "image": map_manifest["image"],
            }
        else:
            ensure(False, "INVALID_BINDING", "Unknown spatial output protocol")
        manifest = {
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": payload["expected_spec_revision"],
            },
            "target": payload["target"],
            "inputs": {name: value["selection"] for name, value in bindings.items()},
            "parameters": payload["parameters"],
            "semantics": {name: value.get("semantics") for name, value in bindings.items()},
            "files": files,
            "asset_descriptor": descriptor,
            "provenance": {
                "implementation_id": profile["implementation_id"],
                "image_id": profile["image_id"],
                **{
                    key: profile[key]
                    for key in (
                        "catalog_digest",
                        "operator_digest",
                        "implementation_digest",
                        "ownership",
                        "trust_level",
                    )
                    if key in profile
                },
                "input_digests": {
                    name: value["representation"]["digest"] for name, value in bindings.items()
                },
            },
        }
        if row.get("parent_execution_id"):
            return self.runtime(
                row,
                phase="REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=files,
                manifest=manifest,
            )
        return self.runtime(row, phase="PUBLISHING", manifest=manifest)

    def abort(self, row, state, error=None, **details):
        if row.get("parent_execution_id"):
            return self.runtime(
                row,
                phase="REPORTING",
                pending_state=state,
                node_revision=row["revision"] + 1,
                error=error,
                publication="PRIVATE",
                **details,
            )
        if row["payload"].get("kind") == "CHECK":
            return self.repo.change(
                row,
                state=state,
                runtime=row["runtime"]
                | {"publication": "NOT_REQUIRED", "phase": "FINAL", "error": error, **details},
            )
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        receipt = self.data.json("POST", prefix + "/abort", json={"generation": row["generation"]})
        runtime = row["runtime"] | {
            "publication": receipt["state"],
            "publication_receipt": receipt,
            "phase": "FINAL",
            "error": error,
            **details,
        }
        if state == "CANCELLED":
            if receipt["state"] == "COMMITTED":
                state = "SUCCEEDED"
                runtime["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
            else:
                runtime["cancellation"] = {"state": "ABORTED_BEFORE_COMMIT"}
        runtime.pop("uncertain", None)
        return self.repo.change(row, state=state, runtime=runtime)

    def cancel(self, row, info):
        if info is None and row["runtime"]["phase"] == "START_INTENT" and not row["executed"]:
            raise OSError("Cannot prove whether the missing container executed after start intent")
        if info:
            self.docker.check(row, info)
            started = not info["State"]["StartedAt"].startswith("0001-")
            if started and not row["executed"]:
                if not self.repo.change(row, executed=True, state="RUNNING"):
                    return
            if info["State"]["Running"] or not started:
                self.docker.stop(info["Id"], never_started=not started)
                return
        self.abort(row, "CANCELLED")

    @staticmethod
    def remote_execution(row):
        return (
            row.get("payload", {}).get("target", {}).get("execution_binding", {}).get("kind")
            == "REMOTE"
        )

    def remote_client(self, row):
        ensure(
            self.remote_factory is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Remote runtime unavailable",
            503,
        )
        return self.remote_factory(row["payload"]["_remote_binding"])

    def apply_remote_observation(self, row, call, observation):
        ensure(isinstance(observation, dict), "INVALID_BINDING", "Invalid remote observation")
        ensure(
            observation.get("action_id") == row["action_id"],
            "INVALID_BINDING",
            "Remote action identity changed",
        )
        revision = observation.get("revision")
        state = observation.get("state")
        executed = observation.get("executed")
        remote_id = observation.get("remote_execution_id")
        event_id = observation.get("event_id")
        ensure(
            type(revision) is int
            and revision >= 1
            and state
            in {
                "ACCEPTED",
                "QUEUED",
                "STARTING",
                "RUNNING",
                "COLLECTING",
                "SUCCEEDED",
                "FAILED",
                "REJECTED",
                "CANCELLED",
            }
            and type(executed) is bool
            and isinstance(remote_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", remote_id)
            and isinstance(event_id, str)
            and 0 < len(event_id) <= 256,
            "INVALID_BINDING",
            "Invalid remote fact",
        )
        if revision < call["remote_revision"]:
            return call
        ensure(
            (state not in {"RUNNING", "COLLECTING", "SUCCEEDED", "FAILED"} or executed)
            and (state not in {"ACCEPTED", "QUEUED", "STARTING"} or not executed)
            and (state != "REJECTED" or not executed),
            "INVALID_BINDING",
            "Remote state and executed fact conflict",
        )
        ensure(
            not call["remote_execution_id"] or call["remote_execution_id"] == remote_id,
            "INVALID_BINDING",
            "Remote execution identity changed",
        )
        ensure(
            not call["executed"] or executed, "INVALID_BINDING", "Remote executed fact regressed"
        )
        protocol = call.get("binding", {}).get("protocol", "v7-remote-execution-v1")
        if protocol == "v7-remote-execution-v2":
            ensure(
                observation.get("implementation_digest")
                == call["binding"]["target"].get("implementation_digest")
                and observation.get("cleanup_state")
                in {"NOT_REQUIRED", "PENDING", "CONFIRMED", "UNKNOWN"}
                and (
                    observation.get("started_at") is None
                    or isinstance(observation.get("started_at"), str)
                )
                and (
                    observation.get("finished_at") is None
                    or isinstance(observation.get("finished_at"), str)
                ),
                "INVALID_BINDING",
                "Remote v2 execution fact differs",
            )
        if revision == call["remote_revision"] and call["observation"] is not None:
            ensure(
                call["observation"] == observation,
                "INVALID_BINDING",
                "Conflicting remote observation revision",
            )
        elif revision > call["remote_revision"]:
            ensure(
                self.repo.observe_remote(
                    row,
                    observation,
                    remote_execution_id=remote_id,
                    remote_revision=revision,
                    remote_state=state,
                    executed=executed,
                    observation=observation,
                    phase="TRACKING",
                ),
                "DEPENDENCY_UNAVAILABLE",
                "Remote lease expired",
                503,
            )
            call = self.repo.remote(row["action_id"])
        if executed and not row["executed"]:
            self.repo.change(row, executed=True, state="RUNNING")
            row["executed"], row["state"] = True, "RUNNING"
        remote = row["runtime"].get("remote", {}) | {
            "state": state,
            "revision": revision,
            "remote_execution_id": remote_id,
            "executed": executed,
        }
        self.runtime(row, remote=remote, phase="REMOTE_TRACKING")
        row["runtime"] = row["runtime"] | {"remote": remote, "phase": "REMOTE_TRACKING"}
        return call

    def cancel_remote(self, row):
        client = self.remote_client(row)
        try:
            observation = client.cancel(row["action_id"])
            if observation is None:
                raise OSError("Remote cancellation is unknown")
            call = self.apply_remote_observation(
                row, self.repo.remote(row["action_id"]), observation
            )
            state = call["remote_state"]
            if state in {"CANCELLED", "REJECTED", "FAILED", "SUCCEEDED"}:
                return self.abort(row, "CANCELLED", "REMOTE_" + state)
        finally:
            client.close()

    def remote_diagnostic(self, row, category, code):
        remote = row["runtime"].get("remote", {}) | {
            "failure_category": category,
            "failure_code": code,
        }
        self.repo.change(row, runtime=row["runtime"] | {"remote": remote})
        row["runtime"] = row["runtime"] | {"remote": remote}

    @staticmethod
    def _remote_output_matches(directory, expected, outputs):
        if not directory.is_dir() or directory.is_symlink():
            return False
        paths = list(directory.iterdir())
        if {path.name for path in paths} != set(expected.values()):
            return False
        for port, filename in expected.items():
            path, meta = directory / filename, outputs[port]
            if not path.is_file() or path.is_symlink():
                return False
            content = path.read_bytes()
            if len(content) != meta["size"]:
                return False
            if "sha256:" + hashlib.sha256(content).hexdigest() != meta["digest"]:
                return False
        return True

    def collect_remote_outputs(self, row, root, call, client):
        outputs = call["observation"].get("outputs")
        expected = row["runtime"]["implementation_profile"]["outputs"]
        ensure(
            isinstance(outputs, dict) and set(outputs) == set(expected),
            "INVALID_BINDING",
            "Remote output set differs",
        )
        for port in expected:
            meta = outputs[port]
            ensure(
                isinstance(meta, dict)
                and isinstance(meta.get("path"), str)
                and isinstance(meta.get("digest"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", meta["digest"])
                and type(meta.get("size")) is int,
                "INVALID_BINDING",
                "Invalid remote output metadata",
            )

        output = root / "output"
        if self._remote_output_matches(output, expected, outputs):
            self.repo.change_remote(row, phase="OUTPUT_VERIFIED")
            return self.collect(row, root)

        incoming_root = root / "remote-incoming"
        incoming = incoming_root / str(call["remote_revision"])
        incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
        if output.is_dir() and any(output.iterdir()):
            quarantine = incoming_root / "legacy-output"
            ensure(
                not quarantine.exists(),
                "INVALID_BINDING",
                "Multiple unverified remote output sets exist",
            )
            os.replace(output, quarantine)
            output.mkdir(mode=0o700)
        total = 0
        for port, filename in expected.items():
            meta = outputs[port]
            remaining = call["binding"]["limits"]["output_bytes"] - total
            ensure(
                0 <= meta["size"] <= remaining,
                "INVALID_BINDING",
                "Remote output exceeds declared limit",
            )
            content_type = client.output(
                row["action_id"],
                meta["path"],
                incoming / filename,
                remaining,
                meta["digest"],
                meta["size"],
            )
            if content_type is None:
                self.remote_diagnostic(row, "COMMUNICATION", "REMOTE_OUTPUT_NOT_VISIBLE")
                raise OSError("Remote output is temporarily not visible")
            ensure(
                meta.get("media_type") == "application/json"
                and content_type.split(";", 1)[0] == "application/json",
                "INVALID_BINDING",
                "Remote output media type differs",
            )
            total += meta["size"]
        ensure(
            total <= call["binding"]["limits"]["output_bytes"]
            and self._remote_output_matches(incoming, expected, outputs),
            "INVALID_BINDING",
            "Remote output set is incomplete",
        )
        directory = os.open(incoming, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        ensure(
            output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
            "INVALID_BINDING",
            "Private output directory is not empty",
        )
        output.rmdir()
        os.replace(incoming, output)
        parent = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        shutil.rmtree(incoming_root, ignore_errors=True)
        self.repo.change_remote(row, phase="OUTPUT_VERIFIED")
        return self.collect(row, root)

    def step_remote(self, row, root):
        phase = row["runtime"]["phase"]
        if phase == "PUBLISHING":
            return self.publish(row, root)
        if row["cancel_requested"]:
            return self.cancel_remote(row)
        if phase == "ACCEPTED":
            return self.prepare_input(row, root)
        call = self.repo.remote(row["action_id"])
        client = self.remote_client(row)
        try:
            if phase == "PREPARED":
                permit = self.permit(row)
                if permit["decision"] == "WAIT":
                    return
                if permit["decision"] != "ALLOW":
                    # Platform can reject a workflow node before assigning a compute
                    # slot (for example when its frozen service binding was disabled).
                    # In that case the node already has a terminal Platform fact and
                    # must not enter REPORTING: observations for nodes without a slot
                    # are deliberately rejected by Platform.
                    if row.get("parent_execution_id") and not permit.get("reportable", True):
                        return self.repo.change(
                            row,
                            state="REJECTED",
                            runtime=row["runtime"]
                            | {
                                "phase": "FINAL",
                                "publication": "PRIVATE",
                                "error": permit.get("reason", "PERMIT_DENIED"),
                            },
                        )
                    return self.abort(row, "REJECTED", "PERMIT_DENIED")
                content = (root / "input/vector.gpkg").read_bytes()
                digest_value = row["payload"]["bindings"]["vector"]["representation"]["digest"]
                ensure(
                    len(content) <= call["binding"]["limits"]["input_bytes"],
                    "INVALID_BINDING",
                    "Remote input exceeds binding limit",
                )
                response = client.upload(row["action_id"], "vector", content, digest_value)
                ensure(
                    response is not None,
                    "DEPENDENCY_UNAVAILABLE",
                    "Remote input status unknown",
                    503,
                )
                ensure(
                    self.repo.change_remote(row, phase="SUBMITTING"),
                    "DEPENDENCY_UNAVAILABLE",
                    "Remote lease expired",
                    503,
                )
                return self.runtime(row, phase="REMOTE_SUBMITTING")
            if phase == "REMOTE_SUBMITTING":
                permit = self.permit(row)
                if permit["decision"] == "WAIT":
                    return
                if permit["decision"] != "ALLOW":
                    if row.get("parent_execution_id") and not permit.get("reportable", True):
                        return self.repo.change(
                            row,
                            state="REJECTED",
                            runtime=row["runtime"]
                            | {
                                "phase": "FINAL",
                                "publication": "PRIVATE",
                                "error": permit.get("reason", "PERMIT_DENIED_BEFORE_REMOTE_SEND"),
                            },
                        )
                    return self.abort(row, "REJECTED", "PERMIT_DENIED_BEFORE_REMOTE_SEND")
                request = {
                    "schema_version": (
                        2 if call["binding"]["protocol"] == "v7-remote-execution-v2" else 1
                    ),
                    "action_id": row["action_id"],
                    "request_digest": call["request_digest"],
                    "target": call["binding"]["target"],
                    "parameters": row["payload"]["parameters"],
                    "inputs": {
                        "vector": {
                            "digest": row["payload"]["bindings"]["vector"]["representation"][
                                "digest"
                            ]
                        }
                    },
                    "limits": call["binding"]["limits"],
                }
                observation = client.submit(row["action_id"], request)
                if observation is None:
                    raise OSError("Remote submit response is unknown")
                self.apply_remote_observation(row, call, observation)
                return
            if phase == "REMOTE_TRACKING":
                observation = client.query(row["action_id"])
                if observation is None:
                    self.remote_diagnostic(row, "COMMUNICATION", "REMOTE_NOT_VISIBLE")
                    raise OSError("Remote action is temporarily not found")
                call = self.apply_remote_observation(row, call, observation)
                state = call["remote_state"]
                if state == "SUCCEEDED":
                    try:
                        return self.collect_remote_outputs(row, root, call, client)
                    except ServiceFault as problem:
                        if problem.status < 500:
                            self.remote_diagnostic(
                                row, "REMOTE_PROTOCOL", "REMOTE_PROTOCOL_VIOLATION"
                            )
                        raise
                if state in {"FAILED", "REJECTED", "CANCELLED"}:
                    self.remote_diagnostic(
                        row,
                        "REMOTE_REJECTION"
                        if state in {"REJECTED", "CANCELLED"} and not call["executed"]
                        else "REMOTE_EXECUTION",
                        "REMOTE_" + state,
                    )
                    terminal = (
                        "REJECTED"
                        if state == "REJECTED" and not call["executed"]
                        else ("CANCELLED" if state == "CANCELLED" else "FAILED")
                    )
                    return self.abort(row, terminal, "REMOTE_" + state)
                return
        finally:
            client.close()

    def collect(self, row, root):
        output = root / "output"
        files = list(output.iterdir())
        ensure(
            all(f.is_file() and not f.is_symlink() for f in files)
            and sum(f.stat().st_size for f in files) <= 100 * 1024 * 1024,
            "INVALID_BINDING",
            "Output limit or format violated",
        )
        self.trace(
            row,
            "OUTPUT_VERIFIED",
            [row["attempt_id"], len(files)],
            attributes={"file_count": len(files)},
        )
        if row["runtime"].get("implementation_profile", {}).get("protocol", "") in {
            "SPATIAL_VECTOR",
            "SPATIAL_SUMMARY",
            "CARTOGRAPHIC_PNG",
        }:
            return self.collect_spatial(row, root)
        if row["runtime"].get("implementation_profile", {}).get("protocol") == "TABULAR_V1":
            return self.collect_tabular(row, root)
        if row["payload"].get("kind") == "CHECK":
            result = json.loads((output / "check.json").read_text())
            ensure(
                result["status"]
                in ("PASS", "FAIL", "NOT_EVALUABLE", "INDETERMINATE", "REVIEW_REQUIRED", "ERROR"),
                "INVALID_BINDING",
                "Invalid checker result",
            )
            return self.repo.change(
                row,
                state="SUCCEEDED",
                runtime=row["runtime"]
                | {"phase": "FINAL", "publication": "NOT_REQUIRED", "check_result": result},
            )
        result = json.loads((output / "result.json").read_text())
        representations = []
        for name, identity, format in [
            ("result.json", "result", "json"),
            ("result.parquet", "table", "parquet"),
            ("method.json", "method", "json"),
        ]:
            content = (output / name).read_bytes()
            representations.append(
                {
                    "representation_id": identity,
                    "format": format,
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "schema_id": output_schema(row["payload"], identity),
                }
            )
        payload = row["payload"]
        ensure(
            self.table_inspector is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Parquet output inspector is unavailable",
            503,
        )
        manifest = {
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": payload["expected_spec_revision"],
            },
            "target": payload["target"],
            "input": payload["binding"]["selection"],
            "parameters": payload["parameters"],
            "semantics": payload["binding"]["semantics"],
            "files": representations,
            "fields": [
                {"field_id": name, "name": name, "type": physical_type}
                for name, physical_type in self.table_inspector.inspect(output / "result.parquet")[
                    "schema"
                ]
            ],
            "row_count": len(result["rows"]),
            "provenance": {
                "implementation_id": row["runtime"]["implementation_profile"]["implementation_id"],
                "image_id": row["runtime"]["implementation_profile"]["image_id"],
                **{
                    key: row["runtime"]["implementation_profile"][key]
                    for key in (
                        "catalog_digest",
                        "operator_digest",
                        "implementation_digest",
                        "ownership",
                        "trust_level",
                    )
                    if key in row["runtime"]["implementation_profile"]
                },
            },
        }
        if row.get("parent_execution_id"):
            private_files = [
                item
                | {
                    "filename": {
                        "result": "result.json",
                        "table": "result.parquet",
                        "method": "method.json",
                    }[item["representation_id"]]
                }
                for item in representations
            ]
            return self.runtime(
                row,
                phase="REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=private_files,
                manifest=manifest,
            )
        return self.runtime(row, phase="PUBLISHING", manifest=manifest)

    def report_node(self, row, *, stopped=False):
        payload = row["payload"]
        revision = row["runtime"].get("node_revision", row["revision"])
        self.platform.json(
            "POST",
            f"/internal/workflows/{payload['parent_action_id']}/"
            f"{payload['parent_execution_id']}/nodes/{row['action_id']}/observations",
            json={
                "revision": revision,
                "state": row["runtime"].get("pending_state", row["state"]),
                "executed": row["executed"],
                "stopped": stopped,
                "execution_id": row["execution_id"],
                "attempt_id": row["attempt_id"],
            },
        )
        self.trace(
            row,
            "NODE_OBSERVATION_REPORTED",
            [revision, stopped],
            attributes={
                "revision": revision,
                "state": row["runtime"].get("pending_state", row["state"]),
            },
        )

    def publish(self, row, root):
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        manifest = row["runtime"]["manifest"]
        previous = self.data.json(
            "POST",
            prefix + "/prepare",
            json={"generation": row["generation"], "manifest": manifest},
        )
        if previous["state"] == "COMMITTED":
            receipt = previous["receipt"]
        else:
            if hasattr(self.platform, "json"):
                publication_permit = self.platform.json(
                    "POST", f"/internal/actions/{row['action_id']}/publication-permit"
                )
                if publication_permit.get("decision") != "ALLOW":
                    return self.abort(row, "REJECTED", "OPERATOR_REVOKED")
            for item in manifest["files"]:
                rep, filename = item["representation_id"], item.get("filename")
                if filename is None:
                    filename = {
                        "result": "result.json",
                        "table": "result.parquet",
                        "method": "method.json",
                    }[rep]
                self.data.request(
                    "PUT",
                    prefix + "/files/" + rep,
                    params={"generation": row["generation"]},
                    content=(root / "output" / filename).read_bytes(),
                )
            receipt = self.data.json(
                "POST", prefix + "/commit", json={"generation": row["generation"]}
            )
        return self.repo.change(
            row,
            state="SUCCEEDED",
            runtime=row["runtime"]
            | {"phase": "FINAL", "publication": "COMMITTED", "publication_receipt": receipt},
        )

    def step(self, row, root):
        # The process lock serializes Docker commands; the database generation fences writes.
        latest = self.repo.get(action=row["action_id"])
        if latest["cancel_requested"]:
            row["cancel_requested"] = True
        if row["payload"].get("kind") == "WORKFLOW":
            ensure(
                self.workflow is not None, "DEPENDENCY_UNAVAILABLE", "Workflow runtime unavailable"
            )
            return self.workflow.step(row, root)
        if not self.repo.change(row):
            return
        if row.get("parent_execution_id"):
            if row["runtime"]["phase"] == "REPORTING":
                self.report_node(row, stopped=True)
                runtime = row["runtime"] | {"phase": "FINAL"}
                runtime.pop("pending_state", None)
                runtime.pop("node_revision", None)
                return self.repo.change(row, state=row["runtime"]["pending_state"], runtime=runtime)
            if (
                row["executed"]
                and row["runtime"].get("node_reported_revision", 0) < row["revision"]
            ):
                self.report_node(row)
                return self.runtime(row, node_reported_revision=row["revision"])
        if self.remote_execution(row):
            return self.step_remote(row, root)
        info = self.docker.inspect(self.docker.name(row))
        if row["cancel_requested"]:
            return self.cancel(row, info)
        phase = row["runtime"]["phase"]
        if phase == "ACCEPTED":
            if "image_id" not in row["runtime"]:
                profile = self.docker.profile(row)
                if not self.runtime(
                    row, image_id=profile["image_id"], implementation_profile=profile
                ):
                    return
            return self.prepare_input(row, root)
        if phase in ("PREPARED", "CREATE_INTENT", "CREATED"):
            permit = self.permit(row)
            if permit["decision"] == "WAIT":
                return
            if permit["decision"] != "ALLOW":
                if row.get("parent_execution_id") and not permit.get("reportable", True):
                    return self.repo.change(
                        row,
                        state="REJECTED",
                        runtime=row["runtime"]
                        | {
                            "phase": "FINAL",
                            "publication": "PRIVATE",
                            "error": permit.get("reason", "PERMIT_DENIED"),
                        },
                    )
                return (
                    self.cancel(row, info) if info else self.abort(row, "REJECTED", "PERMIT_DENIED")
                )
            if phase != "CREATED":
                if not self.runtime(row, phase="CREATE_INTENT"):
                    return
                if info is None:
                    info = self.docker.create(row, root)
                self.docker.check(row, info)
                self.trace(
                    row,
                    "CONTAINER_CREATED",
                    info["Id"],
                    attributes={"container_id": info["Id"]},
                )
                return self.repo.change(
                    row, container_id=info["Id"], runtime=row["runtime"] | {"phase": "CREATED"}
                )
            ensure(info is not None, "INVALID_BINDING", "Created container disappeared")
            self.docker.check(row, info)
            if not self.runtime(row, phase="START_INTENT"):
                return
            self.docker.start(info["Id"])
            info = self.docker.inspect(info["Id"])
            if info is not None and not info["State"]["StartedAt"].startswith("0001-"):
                self.trace(
                    row,
                    "CONTAINER_STARTED",
                    info["Id"],
                    attributes={"container_id": info["Id"]},
                )
                return self.repo.change(
                    row,
                    executed=True,
                    state="RUNNING",
                    runtime=row["runtime"] | {"phase": "RUNNING"},
                )
            return
        if phase in ("START_INTENT", "RUNNING"):
            if info is None:
                # A vanished container after start intent is unknown, never "not executed".
                raise OSError("Container identity unavailable after start intent")
            self.docker.check(row, info)
            state = info["State"]
            if state["StartedAt"].startswith("0001-"):
                # Do not issue another start for an uncertain start intent. Remove the original
                # never-started container before a definite unexecuted failure can be recorded.
                # Persist the stop intent before deletion, so a crash or Data outage after
                # removing a never-started container cannot strand recovery on a missing ID.
                return self.runtime(row, phase="STOPPING_UNSTARTED")
            if not row["executed"]:
                if not self.repo.change(row, executed=True, state="RUNNING"):
                    return
            if row["runtime"].get("uncertain") and not self.runtime(row, phase="RUNNING"):
                return
            if state["Running"]:
                started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
                size = sum(f.stat().st_size for f in (root / "output").iterdir() if f.is_file())
                timeout = (
                    row["runtime"].get("implementation_profile", {}).get("timeout_seconds", 120)
                )
                if (
                    datetime.now(UTC) - started
                ).total_seconds() > timeout or size > 100 * 1024 * 1024:
                    if self.runtime(row, error="RESOURCE_LIMIT"):
                        self.docker.stop(info["Id"])
                return
            self.trace(
                row,
                "CONTAINER_EXITED",
                info["Id"],
                attributes={"container_id": info["Id"], "exit_code": state["ExitCode"]},
            )
            if state["ExitCode"] != 0 or row["runtime"].get("error"):
                error = row["runtime"].get("error") or "CONTAINER_EXIT"
                if state.get("OOMKilled"):
                    error = "MEMORY_LIMIT"
                elif state["ExitCode"] == 142:
                    error = "TIME_LIMIT"
                elif state["ExitCode"] == 153:
                    error = "OUTPUT_LIMIT"
                error_file = root / "output/error.json"
                if (
                    state["ExitCode"] == 10
                    and row["runtime"].get("implementation_profile", {}).get("implementation_id")
                    == "table-validate-native-v1"
                    and error_file.is_file()
                    and not error_file.is_symlink()
                    and error_file.stat().st_size < 16384
                ):
                    try:
                        diagnostic = json.loads(error_file.read_text())
                        failures = diagnostic.get("failures", [])
                        valid = (
                            set(diagnostic)
                            == {
                                "code",
                                "status",
                                "rule_count",
                                "failed_rule_count",
                                "violation_count",
                                "failures",
                            }
                            and diagnostic["code"] == "VALIDATION_FAILED"
                            and diagnostic["status"] == "INVALID"
                            and type(diagnostic["rule_count"]) is int
                            and 1 <= diagnostic["rule_count"] <= 64
                            and type(diagnostic["failed_rule_count"]) is int
                            and diagnostic["failed_rule_count"] == len(failures)
                            and 1 <= len(failures) <= diagnostic["rule_count"]
                            and type(diagnostic["violation_count"]) is int
                            and diagnostic["violation_count"]
                            == sum(item.get("failed_count", -1) for item in failures)
                            and all(
                                set(item) == {"rule_id", "kind", "failed_count"}
                                and isinstance(item["rule_id"], str)
                                and re.fullmatch(
                                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["rule_id"]
                                )
                                and item["kind"]
                                in {
                                    "ROW_COUNT",
                                    "TYPE",
                                    "NOT_NULL",
                                    "UNIQUE",
                                    "RANGE",
                                    "ALLOWED_VALUES",
                                }
                                and type(item["failed_count"]) is int
                                and item["failed_count"] > 0
                                for item in failures
                            )
                            and {
                                path.name
                                for path in (root / "output").iterdir()
                                if path.name != "started.marker"
                            }
                            == {"error.json"}
                        )
                    except (ValueError, TypeError, AttributeError):
                        valid = False
                    if valid:
                        return self.abort(
                            row,
                            "FAILED",
                            "VALIDATION_FAILED",
                            validation=diagnostic,
                        )
                if (
                    state["ExitCode"] == 11
                    and row["runtime"].get("implementation_profile", {}).get("implementation_id")
                    == "table-cast-native-v1"
                    and error_file.is_file()
                    and not error_file.is_symlink()
                    and error_file.stat().st_size < 16384
                ):
                    try:
                        diagnostic = json.loads(error_file.read_text())
                        failures = diagnostic.get("failures", [])
                        valid = (
                            set(diagnostic)
                            == {
                                "code",
                                "status",
                                "cast_count",
                                "failed_cast_count",
                                "violation_count",
                                "failures",
                            }
                            and diagnostic["code"] == "CAST_FAILED"
                            and diagnostic["status"] == "INVALID"
                            and type(diagnostic["cast_count"]) is int
                            and 1 <= diagnostic["cast_count"] <= 128
                            and type(diagnostic["failed_cast_count"]) is int
                            and diagnostic["failed_cast_count"] == len(failures)
                            and 1 <= len(failures) <= diagnostic["cast_count"]
                            and type(diagnostic["violation_count"]) is int
                            and diagnostic["violation_count"]
                            == sum(item.get("failure_count", -1) for item in failures)
                            and all(
                                set(item)
                                == {"field_id", "source_type", "target_type", "failure_count"}
                                and isinstance(item["field_id"], str)
                                and re.fullmatch(
                                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["field_id"]
                                )
                                and item["source_type"] in {"string", "int64", "double", "bool"}
                                and item["target_type"] in {"string", "int64", "double", "bool"}
                                and item["source_type"] != item["target_type"]
                                and type(item["failure_count"]) is int
                                and item["failure_count"] > 0
                                for item in failures
                            )
                            and {
                                path.name
                                for path in (root / "output").iterdir()
                                if path.name != "started.marker"
                            }
                            == {"error.json"}
                        )
                    except (ValueError, TypeError, AttributeError):
                        valid = False
                    if valid:
                        return self.abort(row, "FAILED", "CAST_FAILED", cast=diagnostic)
                if (
                    state["ExitCode"] == 12
                    and row["runtime"].get("implementation_profile", {}).get("implementation_id")
                    == "table-aggregate-native-v1"
                    and error_file.is_file()
                    and not error_file.is_symlink()
                    and error_file.stat().st_size < 16384
                ):
                    try:
                        diagnostic = json.loads(error_file.read_text())
                        failures = diagnostic.get("failures", [])
                        valid = (
                            set(diagnostic)
                            == {
                                "code",
                                "status",
                                "failure_count",
                                "violation_count",
                                "failures",
                            }
                            and diagnostic["code"] == "AGGREGATION_FAILED"
                            and diagnostic["status"] == "INVALID"
                            and type(diagnostic["failure_count"]) is int
                            and diagnostic["failure_count"] == len(failures)
                            and 1 <= len(failures) <= 64
                            and type(diagnostic["violation_count"]) is int
                            and diagnostic["violation_count"]
                            == sum(item.get("count", -1) for item in failures)
                            and all(
                                set(item)
                                == {
                                    "scope",
                                    "measure_id",
                                    "field_id",
                                    "category",
                                    "count",
                                }
                                and item["scope"] in {"GROUP_KEY", "MEASURE"}
                                and (
                                    item["measure_id"] is None
                                    or re.fullmatch(
                                        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",
                                        item["measure_id"],
                                    )
                                )
                                and re.fullmatch(
                                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["field_id"]
                                )
                                and item["category"]
                                in {"NULL_GROUP_KEY", "NONFINITE_VALUE", "NUMERIC_OVERFLOW"}
                                and type(item["count"]) is int
                                and item["count"] > 0
                                for item in failures
                            )
                            and {
                                path.name
                                for path in (root / "output").iterdir()
                                if path.name != "started.marker"
                            }
                            == {"error.json"}
                        )
                    except (ValueError, TypeError, AttributeError):
                        valid = False
                    if valid:
                        return self.abort(
                            row,
                            "FAILED",
                            "AGGREGATION_FAILED",
                            aggregation=diagnostic,
                        )
                if (
                    error_file.is_file()
                    and not error_file.is_symlink()
                    and error_file.stat().st_size < 4096
                ):
                    try:
                        if json.loads(error_file.read_text()).get("code") == "INPUT_ERROR":
                            error = "INPUT_ERROR"
                    except ValueError:
                        pass
                return self.abort(row, "FAILED", error)
            try:
                return self.collect(row, root)
            except (ValueError, KeyError, FileNotFoundError, ServiceFault) as exc:
                return self.abort(row, "FAILED", "INVALID_OUTPUT_" + type(exc).__name__)
        if phase == "STOPPING_UNSTARTED":
            if info:
                self.docker.check(row, info)
                if not info["State"]["StartedAt"].startswith("0001-"):
                    return self.repo.change(
                        row,
                        executed=True,
                        state="RUNNING",
                        runtime=row["runtime"] | {"phase": "RUNNING"},
                    )
                self.docker.stop(info["Id"], never_started=True)
                return
            return self.abort(row, "FAILED", "START_INTERRUPTED")
        if phase == "PUBLISHING":
            return self.publish(row, root)
        raise ValueError("Unknown execution phase")

    def tick(self):
        row = (
            self.repo.claim(self.owner, self.lease_seconds)
            if self.work_classes is None
            else self.repo.claim(self.owner, self.lease_seconds, self.work_classes)
        )
        if row is None:
            return False
        root = self.root / row["execution_id"]
        for name in ("input", "output"):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (root / "runtime.lock").open("a")
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.repo.renew(row, self.lease_seconds):
                        return
                except Exception:
                    # Keep the application layer independent of the database adapter.
                    # An unrenewed claim is fenced at every authoritative write.
                    logging.warning(
                        "Execution lease renewal unavailable action=%s generation=%s",
                        row["action_id"],
                        row["generation"],
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        delay = 0.2
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            if row.get("parent_execution_id") and row["runtime"]["phase"] == "ACCEPTED":
                self.trace(
                    row,
                    "EXECUTION_CLAIMED",
                    "first",
                    attributes={"phase": row["runtime"]["phase"]},
                )
            self.step(row, root)
        except (ServiceFault, ValueError, KeyError) as exc:
            if isinstance(exc, ServiceFault) and exc.status >= 500:
                failures = row["runtime"].get("failures", 0) + 1
                delay = min(30, 2 ** min(failures - 1, 5))
                self.repo.change(
                    row, runtime=row["runtime"] | {"uncertain": True, "failures": failures}
                )
                logging.warning(
                    "Execution recovery action=%s generation=%s "
                    "category=SERVICE_UNAVAILABLE code=%s detail=%s",
                    row["action_id"],
                    row["generation"],
                    exc.code,
                    str(exc),
                )
                return True
            if row["runtime"]["phase"] in (
                "CREATE_INTENT",
                "CREATED",
                "START_INTENT",
                "RUNNING",
                "STOPPING_UNSTARTED",
            ):
                # Runtime identity failure cannot prove that computation has stopped.
                self.repo.change(row, runtime=row["runtime"] | {"uncertain": True})
                return True
            # Invalid input is a failed execution only after Data has sealed publication.
            try:
                error = (
                    row["runtime"].get("remote", {}).get("failure_code")
                    if self.remote_execution(row)
                    else None
                ) or (exc.code if isinstance(exc, ServiceFault) else type(exc).__name__)
                terminal = "FAILED" if row["executed"] else "REJECTED"
                if row.get("payload", {}).get("kind") == "WORKFLOW" and self.workflow:
                    latest = self.repo.get(action=row["action_id"])
                    cancelled = latest["cancel_requested"]
                    self.workflow.abort(
                        row,
                        "CANCELLED" if cancelled else terminal,
                        "CANCELLED" if cancelled else error,
                    )
                else:
                    self.abort(row, terminal, error)
            except (OSError, ServiceFault):
                self.repo.change(row, runtime=row["runtime"] | {"uncertain": True})
        except (OSError, subprocess.TimeoutExpired):
            failures = row["runtime"].get("failures", 0) + 1
            delay = min(30, 2 ** min(failures - 1, 5))
            self.repo.change(
                row, runtime=row["runtime"] | {"uncertain": True, "failures": failures}
            )
            logging.warning(
                "Execution recovery action=%s generation=%s category=COMMUNICATION",
                row["action_id"],
                row["generation"],
            )
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
            lock.close()
            self.repo.release(row, delay)
        return True
