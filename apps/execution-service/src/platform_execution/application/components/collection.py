"""Top-level action and workflow output collectors."""

import hashlib
import json
from urllib.parse import unquote, urlparse

from platform_contracts.service import ensure

from platform_execution.engine_package import validate as validate_engine_package

from .protocols import WorkflowComponent
from .spatial_collection import SpatialOutputCollector
from .standard_collection import StandardOutputCollector
from .tabular_collection import TabularOutputCollector


class OutputCollector(TabularOutputCollector, SpatialOutputCollector, StandardOutputCollector):
    def run(self, row, root):
        output = root / "output"
        files = list(output.iterdir())
        ensure(
            all(f.is_file() and not f.is_symlink() for f in files)
            and sum(f.stat().st_size for f in files) <= 100 * 1024 * 1024,
            "INVALID_BINDING",
            "Output limit or format violated",
        )
        self.execution.trace(
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
        return self.collect_standard(row, root)


class WorkflowOutputCollector(WorkflowComponent):
    def _result_path(self, root, value):
        location = value.get("location") or value.get("path")
        ensure(isinstance(location, str), "INVALID_BINDING", "Workflow File has no location")
        if location.startswith("file://"):
            location = unquote(urlparse(location).path)
        prefix = "/run/work/"
        ensure(location.startswith(prefix), "INVALID_BINDING", "Workflow output escaped work area")
        path = (root / location[len(prefix) :]).resolve()
        ensure(
            path.is_relative_to(root) and path.is_file() and not path.is_symlink(),
            "INVALID_BINDING",
            "Workflow output is unavailable",
        )
        return path

    def run(self, row, root):
        if "payload" in row:
            validate_engine_package(row["payload"])
        epoch = row["runtime"]["engine_epoch"]
        result = json.loads((root / f"result-{epoch}.json").read_text())
        children = self.repo.children(row["execution_id"])
        matches = {}
        for child in children:
            for item in child["runtime"].get("files", []):
                name = child["action_id"] + "--" + item["representation_id"]
                matches.setdefault(name, []).append((child, item))
        members = {}

        def freeze(value, path):
            if isinstance(value, dict) and value.get("class") == "File":
                source = self._result_path(root, value)
                content = source.read_bytes()
                fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
                candidates = matches.get(source.name, [])
                ensure(
                    len(candidates) == 1 and candidates[0][1]["digest"] == fingerprint,
                    "INVALID_BINDING",
                    "Output provenance is ambiguous or changed",
                )
                child, item = candidates[0]
                member = members.setdefault(
                    child["action_id"],
                    {
                        "member_id": child["action_id"],
                        "child_execution_id": child["execution_id"],
                        "manifest": child["runtime"]["manifest"],
                    },
                )
                return {
                    "member_id": member["member_id"],
                    "representation_id": item["representation_id"],
                }
            if isinstance(value, dict):
                return {key: freeze(item, path + [key]) for key, item in value.items()}
            if isinstance(value, list):
                return [freeze(item, path + [index]) for index, item in enumerate(value)]
            return value

        outputs = freeze(result, [])
        files = []
        total = 0
        for member in members.values():
            child = next(item for item in children if item["action_id"] == member["member_id"])
            for item in child["runtime"]["files"]:
                total += item["size"]
                files.append(
                    {
                        "representation_id": member["member_id"] + "--" + item["representation_id"],
                        "member_id": member["member_id"],
                        "original_representation_id": item["representation_id"],
                        "format": item["format"],
                        "digest": item["digest"],
                        "size": item["size"],
                        **(
                            {"schema_id": item["schema_id"]}
                            if item.get("schema_id") is not None
                            else {}
                        ),
                        "child_execution_id": child["execution_id"],
                        "filename": item["filename"],
                    }
                )
        ensure(total <= 100 * 1024 * 1024, "INVALID_BINDING", "Workflow output limit exceeded")
        manifest = {
            "kind": "WORKFLOW_RESULT",
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": row["payload"]["expected_spec_revision"],
            },
            "target": row["payload"]["target"],
            "outputs": outputs,
            "members": list(members.values()),
            "files": files,
        }
        self.execution.trace(
            row,
            "GATHER_FINISHED",
            [row["runtime"]["engine_epoch"], len(members)],
            attributes={"member_count": len(members)},
        )
        return self.execution.transition(row, "PUBLISHING", manifest=manifest)
