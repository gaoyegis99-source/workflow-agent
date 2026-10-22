"""Atomic whole-result publication and abort arbitration under the same control row."""

import hashlib

from platform_contracts.service import digest, ensure

from platform_data.application.data import Data

DESCRIPTOR_OUTPUTS = {
    "OPERATOR_RESULT": {"report", "table", "method"},
    "SPATIAL_VECTOR": {"vector", "manifest", "method"},
    "SPATIAL_SUMMARY": {"summary", "method"},
    "CARTOGRAPHIC_PRODUCT": {"image", "manifest", "method"},
}


def descriptor_fields(descriptor):
    kind = descriptor.get("kind") if isinstance(descriptor, dict) else None
    ensure(kind in DESCRIPTOR_OUTPUTS, "INVALID_BINDING", "Unknown result descriptor")
    if kind == "OPERATOR_RESULT":
        tabular = descriptor.get("tabular")
        ensure(
            isinstance(tabular, dict)
            and type(tabular.get("row_count")) is int
            and 0 <= tabular["row_count"] <= 100_000
            and isinstance(tabular.get("fields"), list)
            and 0 < len(tabular["fields"]) <= 128
            and len({field.get("field_id") for field in tabular["fields"]})
            == len(tabular["fields"])
            and all(
                field.get("type") in {"string", "int64", "double", "bool"}
                for field in tabular["fields"]
            ),
            "INVALID_BINDING",
            "Invalid tabular result descriptor",
        )
        return {"tabular": tabular}
    if kind == "SPATIAL_VECTOR":
        ensure(
            isinstance(descriptor.get("spatial"), dict) and descriptor.get("driver") == "GPKG",
            "INVALID_BINDING",
            "Invalid vector descriptor",
        )
        return {"spatial": descriptor["spatial"], "driver": descriptor["driver"]}
    if kind == "SPATIAL_SUMMARY":
        ensure(isinstance(descriptor.get("summary"), dict), "INVALID_BINDING", "Invalid summary")
        return {"summary": descriptor["summary"]}
    ensure(
        isinstance(descriptor.get("cartography"), dict)
        and isinstance(descriptor.get("template"), dict)
        and isinstance(descriptor.get("image"), dict),
        "INVALID_BINDING",
        "Invalid cartographic descriptor",
    )
    return {
        "cartography": descriptor["cartography"],
        "template": descriptor["template"],
        "image": descriptor["image"],
    }


class Publication:
    def __init__(self, repository, blobs):
        self.repo, self.blobs = repository, blobs

    def get(self, project, execution):
        return self.repo.publication(project, execution)

    def locked(self, c, project, execution, generation):
        ensure(
            type(generation) is int and generation > 0,
            "MALFORMED_REQUEST",
            "Positive publication generation required",
            422,
        )
        row = self.repo.lock_publication(c, project, execution, generation)
        ensure(row["project_id"] == project, "SCOPE_DENIED", "Project mismatch", 403)
        ensure(generation >= row["generation"], "REVISION_CONFLICT", "Stale publication generation")
        if generation != row["generation"]:
            self.repo.update_publication(c, execution, generation=generation)
        return row

    def prepare(self, project, execution, generation, manifest):
        ensure(
            manifest["execution_id"] == execution and manifest["project_id"] == project,
            "INVALID_BINDING",
            "Publication identity mismatch",
        )
        manifest_digest = digest(manifest)
        files = manifest["files"]
        if manifest.get("kind") == "WORKFLOW_RESULT":
            members = manifest.get("members", [])
            member_ids = {item.get("member_id") for item in members}
            available = {
                (item.get("member_id"), item.get("original_representation_id")) for item in files
            }

            def output_is_valid(value):
                if isinstance(value, dict) and set(value) == {
                    "member_id",
                    "representation_id",
                }:
                    return (value["member_id"], value["representation_id"]) in available
                if isinstance(value, dict):
                    return all(output_is_valid(item) for item in value.values())
                if isinstance(value, list):
                    return all(output_is_valid(item) for item in value)
                return value is None or type(value) in {bool, int, float, str}

            complete_members = True
            for member in members:
                source = member.get("manifest", {})
                expected_files = {
                    (
                        item.get("representation_id"),
                        item.get("format"),
                        item.get("digest"),
                        item.get("size"),
                    )
                    for item in source.get("files", [])
                }
                supplied_files = {
                    (
                        item.get("original_representation_id"),
                        item.get("format"),
                        item.get("digest"),
                        item.get("size"),
                    )
                    for item in files
                    if item.get("member_id") == member.get("member_id")
                }
                complete_members = (
                    complete_members and bool(expected_files) and expected_files == supplied_files
                )
            ensure(
                0 < len(member_ids) == len(members) <= 1000
                and None not in member_ids
                and len({f.get("representation_id") for f in files}) == len(files)
                and all(f.get("member_id") in member_ids for f in files)
                and len(available) == len(files)
                and complete_members
                and output_is_valid(manifest.get("outputs"))
                and sum(f.get("size", -1) for f in files) <= 100 * 1024 * 1024,
                "INVALID_BINDING",
                "Invalid workflow output set or size",
            )
        else:
            if manifest.get("asset_descriptor"):
                descriptor = manifest["asset_descriptor"]
                descriptor_fields(descriptor)
                required_outputs = DESCRIPTOR_OUTPUTS[descriptor["kind"]]
                ensure(
                    {f["representation_id"] for f in files} == required_outputs
                    and len(files) == len(required_outputs)
                    and sum(f["size"] for f in files) <= 100 * 1024 * 1024,
                    "INVALID_BINDING",
                    "Invalid spatial output set or size",
                )
            else:
                ensure(
                    {f["representation_id"] for f in files} == {"result", "table", "method"}
                    and len(files) == 3
                    and sum(f["size"] for f in files) <= 100 * 1024 * 1024,
                    "INVALID_BINDING",
                    "Invalid output set or size",
                )
        for file in files:
            self.blobs.path(file["digest"])
            ensure(
                type(file["size"]) is int and file["size"] >= 0,
                "INVALID_BINDING",
                "Invalid output size",
            )
        with self.repo.transaction() as c:
            row = self.locked(c, project, execution, generation)
            ensure(row["state"] != "ABORTED", "PRECONDITION_UNRESOLVED", "Publication was aborted")
            ensure(
                row["manifest_digest"] in (None, manifest_digest),
                "IDEMPOTENCY_CONFLICT",
                "Candidate manifest cannot change",
            )
            if row["manifest_digest"] is None:
                self.repo.update_publication(
                    c,
                    execution,
                    attempt_id=manifest["attempt_id"],
                    manifest_digest=manifest_digest,
                    payload=manifest,
                )
        return self.get(project, execution)

    def file(self, project, execution, generation, representation, content):
        row = self.get(project, execution)
        ensure(
            row["state"] != "ABORTED" and row["generation"] == generation,
            "REVISION_CONFLICT",
            "Publication is aborted or generation changed",
        )
        item = next(
            (f for f in row["payload"]["files"] if f["representation_id"] == representation), None
        )
        ensure(
            item is not None
            and len(content) == item["size"]
            and "sha256:" + hashlib.sha256(content).hexdigest() == item["digest"],
            "INVALID_BINDING",
            "Publication file mismatch",
        )
        self.blobs.store(content)
        return {"digest": item["digest"]}

    def commit(self, project, execution, generation):
        row = self.get(project, execution)
        if row["state"] not in ("ABORTED", "COMMITTED"):
            ensure(bool(row["manifest_digest"]), "PRECONDITION_UNRESOLVED", "No candidate")
            for item in row["payload"]["files"]:
                content = self.blobs.read(item["digest"])
                ensure(len(content) == item["size"], "INVALID_BINDING", "Output size changed")
        # READY is durable before the visibility transaction. Both transitions fence the worker.
        with self.repo.transaction() as c:
            row = self.locked(c, project, execution, generation)
            ensure(row["state"] != "ABORTED", "PRECONDITION_UNRESOLVED", "Publication was aborted")
            if row["state"] != "COMMITTED":
                self.repo.update_publication(c, execution, state="READY")
        with self.repo.transaction() as c:
            row = self.locked(c, project, execution, generation)
            ensure(row["state"] != "ABORTED", "PRECONDITION_UNRESOLVED", "Publication was aborted")
            if row["state"] == "COMMITTED":
                return row["receipt"]
            payload = row["payload"]
            if payload.get("kind") == "WORKFLOW_RESULT":
                references = {}
                files_by_member = {
                    member["member_id"]: [
                        item
                        for item in payload["files"]
                        if item["member_id"] == member["member_id"]
                    ]
                    for member in payload["members"]
                }
                for member in payload["members"]:
                    source = member["manifest"]
                    representations = [
                        {
                            "representation_id": item["original_representation_id"],
                            "format": item["format"],
                            "digest": item["digest"],
                            "size": item["size"],
                            **(
                                {"schema_id": item["schema_id"]}
                                if item.get("schema_id") is not None
                                else {}
                            ),
                        }
                        for item in files_by_member[member["member_id"]]
                    ]
                    identity = (
                        "w"
                        + hashlib.sha256(
                            (execution + ":" + member["member_id"]).encode()
                        ).hexdigest()[:32]
                    )
                    descriptor = source.get("asset_descriptor")
                    manifest = {
                        "asset_id": identity,
                        "version_id": "1",
                        "project_id": project,
                        "kind": "OPERATOR_RESULT",
                        "representations": representations,
                        "source": {
                            key: source[key]
                            for key in (
                                "execution_id",
                                "attempt_id",
                                "task_ref",
                                "target",
                                "parameters",
                                "semantics",
                            )
                        }
                        | (
                            {"inputs": source["inputs"], "provenance": source["provenance"]}
                            if descriptor
                            else {"input": source["input"]}
                        ),
                        "workflow": {
                            "execution_id": execution,
                            "member_id": member["member_id"],
                        },
                    }
                    if descriptor:
                        manifest["result_kind"] = descriptor["kind"]
                        manifest.update(descriptor_fields(descriptor))
                    else:
                        manifest["fields"] = source["fields"]
                        manifest["row_count"] = source["row_count"]
                    record = {
                        "asset_id": identity,
                        "version_id": "1",
                        "project_id": project,
                        "manifest": manifest,
                        "manifest_digest": "sha256:" + digest(manifest),
                    }
                    self.repo.add_asset(c, record)
                    ref = Data.asset_view(record)["ref"]
                    self.repo.add_publication_member(c, execution, member["member_id"], ref)
                    references[member["member_id"]] = ref

                def resolve(value):
                    if isinstance(value, dict) and set(value) == {
                        "member_id",
                        "representation_id",
                    }:
                        return {
                            "asset": references[value["member_id"]],
                            "representation_id": value["representation_id"],
                        }
                    if isinstance(value, dict):
                        return {key: resolve(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [resolve(item) for item in value]
                    return value

                receipt = {
                    "state": "COMMITTED",
                    "execution_id": execution,
                    "assets": list(references.values()),
                    "outputs": resolve(payload["outputs"]),
                }
                self.repo.update_publication(c, execution, state="COMMITTED", receipt=receipt)
                return receipt
            descriptor = payload.get("asset_descriptor")
            manifest = {
                "asset_id": execution,
                "version_id": "1",
                "project_id": project,
                "kind": "OPERATOR_RESULT",
                "representations": payload["files"],
                "source": {
                    k: payload[k]
                    for k in (
                        "execution_id",
                        "attempt_id",
                        "task_ref",
                        "target",
                        "parameters",
                        "semantics",
                    )
                }
                | (
                    {"inputs": payload["inputs"], "provenance": payload["provenance"]}
                    if descriptor
                    else {"input": payload["input"]}
                ),
            }
            if descriptor:
                manifest["result_kind"] = descriptor["kind"]
                manifest.update(descriptor_fields(descriptor))
            else:
                manifest["fields"] = payload["fields"]
                manifest["row_count"] = payload["row_count"]
            record = dict(
                asset_id=execution,
                version_id="1",
                project_id=project,
                manifest=manifest,
                manifest_digest="sha256:" + digest(manifest),
            )
            self.repo.add_asset(c, record)
            receipt = {
                "state": "COMMITTED",
                "execution_id": execution,
                "asset": Data.asset_view(record)["ref"],
            }
            self.repo.update_publication(c, execution, state="COMMITTED", receipt=receipt)
            return receipt

    def abort(self, project, execution, generation):
        with self.repo.transaction() as c:
            row = self.locked(c, project, execution, generation)
            if row["state"] == "COMMITTED":
                return row["receipt"]
            self.repo.update_publication(c, execution, state="ABORTED")
            return {"state": "ABORTED", "execution_id": execution}
