"""Input materialization algorithms for actions and workflows."""

import hashlib
import json
import re
import secrets

from platform_contracts.science import (
    TableAggregateParameters,
    TableCastParameters,
    TableFilterParameters,
)
from platform_contracts.service import ServiceFault, canonical, ensure

from platform_execution.engine_package import validate as validate_engine_package

from .protocols import ActionComponent, WorkflowComponent


class InputMaterializer(ActionComponent):
    def run(self, row, root):
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
                    self.execution.save(role_root / filename, content)
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
                    self.execution.save(role_root / "observations.parquet", content)
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
                    self.execution.save(role_root / "source-table.parquet", content)
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
            self.execution.save(root / "input/input-index.json", canonical(index))
            self.execution.save(root / "input/request.json", canonical(payload))
            return self.execution.transition(row, "PREPARED")
        if protocol == "OPEN_SCIENCE_CHECK":
            ensure(
                payload.get("acceptance_spec", {}).get("profile") == "open-science-v1"
                and isinstance(payload.get("coverage_plan"), dict),
                "INVALID_BINDING",
                "Open checker requires a frozen coverage plan",
            )
            self.execution.save(root / "input/request.json", canonical(payload))
            return self.execution.transition(row, "PREPARED")
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
            self.execution.save(root / "input" / filename, content)
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
                    self.execution.save(root / "input" / f"candidate-{index}.{extension}", data)
        self.execution.save(root / "input/request.json", canonical(payload))
        return self.execution.transition(row, "PREPARED")


class WorkflowInputMaterializer(WorkflowComponent):
    def run(self, row, root):
        engine_package = validate_engine_package(row["payload"])
        package = root / "package"
        package.mkdir(exist_ok=True)
        for name, document in engine_package["files"].items():
            self.execution.save(package / name, json.dumps(document, sort_keys=True).encode())
        self.execution.save(
            root / "engine-package.json",
            json.dumps(engine_package, sort_keys=True, separators=(",", ":")).encode(),
        )

        counter = 0

        def materialize(value):
            nonlocal counter
            if isinstance(value, dict) and value.get("class") == "File":
                binding = value["binding"]
                selection, representation = binding["selection"], binding["representation"]
                ref = selection["asset"]
                path = (
                    f"/internal/projects/{row['project_id']}/assets/{ref['asset_id']}/versions/"
                    f"{ref['version_id']}/representations/{selection['representation_id']}/content"
                )
                content = self.data.request("GET", path).content
                ensure(
                    "sha256:" + hashlib.sha256(content).hexdigest() == representation["digest"],
                    "INVALID_BINDING",
                    "Workflow input digest mismatch",
                )
                counter += 1
                filename = f"input-{counter}-{representation['digest'][7:23]}"
                self.execution.save(root / "input" / filename, content)
                return {"class": "File", "path": "/run/work/input/" + filename}
            if isinstance(value, dict):
                return {key: materialize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [materialize(item) for item in value]
            return value

        inputs = materialize(row["payload"]["inputs"])
        self.execution.save(root / "inputs.json", json.dumps(inputs, sort_keys=True).encode())
        token_path = root / "bridge.token"
        if not token_path.exists():
            self.execution.save(token_path, secrets.token_urlsafe(48).encode(), 0o400)
        token = token_path.read_text()
        return self.execution.transition(
            row,
            "PREPARED",
            publication="UNKNOWN",
            image_id=self.docker.image,
            bridge_token_digest=hashlib.sha256(token.encode()).hexdigest(),
            engine_epoch=0,
            engine_strategy=engine_package["strategy"],
            engine_package_digest=engine_package["package_digest"],
        )
