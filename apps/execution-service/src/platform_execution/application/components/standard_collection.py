"""Collection of non-spatial, non-tabular action outputs."""

from __future__ import annotations

import hashlib
import json

from platform_contracts.service import ensure

from ._schemas import output_schema
from .protocols import ActionComponent


class StandardOutputCollector(ActionComponent):
    """Build the immutable manifest for ordinary operator outputs."""

    def collect_standard(self, row, root):
        output = root / "output"
        if row["payload"].get("kind") == "CHECK":
            result = json.loads((output / "check.json").read_text())
            ensure(
                result["status"]
                in ("PASS", "FAIL", "NOT_EVALUABLE", "INDETERMINATE", "REVIEW_REQUIRED", "ERROR"),
                "INVALID_BINDING",
                "Invalid checker result",
            )
            return self.execution.transition(
                row,
                "FINAL",
                state="SUCCEEDED",
                publication="NOT_REQUIRED",
                check_result=result,
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
            return self.execution.transition(
                row,
                "REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=private_files,
                manifest=manifest,
            )
        return self.execution.transition(row, "PUBLISHING", manifest=manifest)
