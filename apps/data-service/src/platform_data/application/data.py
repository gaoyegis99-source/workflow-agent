"""Import orchestration. File and database adapters are injected, never cross-service tables."""

import re

from platform_contracts.pagination import decode_cursor, encode_cursor
from platform_contracts.service import ensure

from platform_data.domain.model import checksum


class Data:
    def __init__(self, repository, blobs):
        self.repo, self.blobs = repository, blobs

    def create(self, project, actor, key, request):
        if request["format"] in {"geopackage", "geotiff", "markdown", "text"}:
            ensure(
                re.fullmatch(r"[^/\\\x00]{1,200}", request["filename"]) is not None,
                "MALFORMED_REQUEST",
                "Spatial upload filename must be a plain filename",
                422,
            )

        def operation(c):
            id = self.repo.id()
            self.repo.add_upload(c, id, project, request | {"actor_id": actor})
            return self.view(self.repo.upload(c, project, id))

        return self.repo.write(f"{actor}:{project}:upload", key, request, operation)

    @staticmethod
    def view(row):
        return {
            "upload_id": row["upload_id"],
            "project_id": row["project_id"],
            "status": row["state"],
            **row["payload"],
        }

    def get(self, project, id):
        with self.repo.transaction() as c:
            return self.view(self.repo.upload(c, project, id))

    def list_page(self, project, kind, cursor=None, limit=100):
        scope = f"data:{kind}:{project}"
        key_count = 1 if kind == "uploads" else 2
        try:
            after = decode_cursor(cursor, scope, key_count)
        except ValueError:
            ensure(False, "MALFORMED_REQUEST", "Invalid list cursor", 400)
        rows = self.repo.list_records(project, kind, after, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items = [self.view(row) if kind == "uploads" else self.asset_view(row) for row in rows]
        next_cursor = None
        if more:
            row = rows[-1]
            keys = (
                (row["upload_id"],) if kind == "uploads" else (row["asset_id"], row["version_id"])
            )
            next_cursor = encode_cursor(scope, keys)
        return {"items": items, "next_cursor": next_cursor}

    def list(self, project, kind, limit=100):
        """Application-level convenience used by transactional service tests."""
        return self.list_page(project, kind, None, limit)["items"]

    def content(self, project, id, content):
        # Immutable bytes are durable before linking from a short database transaction.
        current = self.get(project, id)
        ensure(
            len(content) == current["size"] and checksum(content) == current["digest"],
            "INVALID_BINDING",
            "Upload size or digest mismatch",
            422,
        )
        self.blobs.store(content)
        with self.repo.transaction() as c:
            row = self.repo.upload(c, project, id, True)
            ensure(
                row["payload"]["digest"] == checksum(content),
                "IDEMPOTENCY_CONFLICT",
                "Upload content changed",
            )
            if row["state"] == "AWAITING_CONTENT":
                self.repo.update_upload(c, id, state="UPLOADED")
        return self.get(project, id)

    def finalize(self, project, actor, id, key, request):
        def operation(c):
            row = self.repo.upload(c, project, id, True)
            ensure(
                row["state"] == "UPLOADED",
                "PRECONDITION_UNRESOLVED",
                "Upload content must be complete and not finalized",
            )
            format_id = row["payload"]["format"]
            spatial = format_id in {"geopackage", "geotiff"}
            document = format_id in {"markdown", "text"}
            ensure(
                (spatial and request.get("kind") == "SPATIAL")
                or (document and request.get("kind") == "DOCUMENT")
                or (not spatial and not document and "kind" not in request),
                "MALFORMED_REQUEST",
                "Finalize contract does not match the uploaded format",
                422,
            )
            ensure(
                format_id != "csv" or request["columns"],
                "MALFORMED_REQUEST",
                "CSV requires explicit column types",
                422,
            )
            payload = row["payload"] | {"conversion": request}
            self.repo.update_upload(c, id, state="QUEUED", payload=payload)
            return {
                "upload_id": id,
                "status": "QUEUED",
                "status_url": f"/api/v1/projects/{project}/uploads/{id}",
            }

        return self.repo.write(f"{actor}:{project}:finalize:{id}", key, request, operation)

    @staticmethod
    def asset_view(row):
        return {
            "ref": {
                "project_id": row["project_id"],
                "asset_id": row["asset_id"],
                "version_id": row["version_id"],
                "version_manifest_digest": row["manifest_digest"],
            },
            "manifest": row["manifest"],
        }

    def asset(self, project, id, version):
        with self.repo.transaction() as c:
            result = self.asset_view(self.repo.asset(c, project, id, version))
            result["semantic_revisions"] = self.repo.semantic_revisions(c, id, version)
            return result

    def semantic(self, project, actor, id, key, request):
        def operation(c):
            asset = self.repo.lock_asset(c, project, id, request["version_id"])
            manifest = asset["manifest"]
            semantics = request["semantics"]
            if semantics.get("profile") == "spatial-generic-v1":
                ensure(
                    manifest.get("kind") in {"SPATIAL_VECTOR", "SPATIAL_RASTER", "OPERATOR_RESULT"},
                    "INVALID_BINDING",
                    "Spatial semantics require a spatial asset",
                    422,
                )
                spatial = manifest.get("spatial", {})
                layers = {item["layer_id"]: item for item in spatial.get("layers", [])}
                bands = {item["band_id"] for item in spatial.get("bands", [])}
                for annotation in semantics["annotations"]:
                    target = annotation["target"]
                    if target["kind"] in {"LAYER", "FIELD"}:
                        ensure(
                            target["layer_id"] in layers,
                            "INVALID_BINDING",
                            "Semantic annotation references an unknown layer",
                            422,
                        )
                    if target["kind"] == "FIELD":
                        ensure(
                            target["field_id"]
                            in {
                                field["field_id"] for field in layers[target["layer_id"]]["fields"]
                            },
                            "INVALID_BINDING",
                            "Semantic annotation references an unknown field",
                            422,
                        )
                    if target["kind"] == "BAND":
                        ensure(
                            target["band_id"] in bands,
                            "INVALID_BINDING",
                            "Semantic annotation references an unknown band",
                            422,
                        )
            revisions = self.repo.semantic_revisions(c, id, request["version_id"])
            previous = revisions[-1]["revision"] if revisions else 0
            ensure(
                previous == request["expected_revision"],
                "REVISION_CONFLICT",
                "Semantic revision changed",
            )
            self.repo.add_semantic(c, id, request["version_id"], previous + 1, request["semantics"])
            return {
                "asset_id": id,
                "version_id": request["version_id"],
                "revision": previous + 1,
                "semantics": request["semantics"],
            }

        return self.repo.write(f"{actor}:{project}:semantics:{id}", key, request, operation)

    def representation(self, project, id, version, representation):
        value = self.asset(project, id, version)
        item = next(
            (
                r
                for r in value["manifest"]["representations"]
                if r["representation_id"] == representation
            ),
            None,
        )
        ensure(item is not None, "RESOURCE_NOT_FOUND", "Representation not found", 404)
        # Verify before exposing a file to FileResponse.
        self.blobs.read(item["digest"])
        return self.blobs.path(item["digest"]), item

    def resolve(self, project, selection):
        ref = selection["asset"]
        ensure(ref["project_id"] == project, "SCOPE_DENIED", "Input project differs", 403)
        value = self.asset(project, ref["asset_id"], ref["version_id"])
        ensure(value["ref"] == ref, "INVALID_BINDING", "Asset manifest changed")
        manifest = value["manifest"]
        rep = next(
            (
                r
                for r in manifest["representations"]
                if r["representation_id"] == selection["representation_id"]
            ),
            None,
        )
        spatial_selection = "selector" in selection
        table_selection = selection.get("kind") == "TABLE"
        if spatial_selection:
            ensure(
                rep is not None
                and rep["format"] in {"geopackage", "geotiff"}
                and manifest["kind"] in {"SPATIAL_VECTOR", "SPATIAL_RASTER", "OPERATOR_RESULT"},
                "INVALID_BINDING",
                "Input must use a checked spatial representation",
            )
        elif table_selection:
            tabular = manifest.get("tabular", manifest)
            fields = tabular.get("fields", [])
            ensure(
                rep is not None
                and rep["format"] == "parquet"
                and manifest["kind"] in {"OBSERVATIONS", "OPERATOR_RESULT"}
                and type(tabular.get("row_count")) is int
                and 0 <= tabular["row_count"] <= 100_000
                and 0 < len(fields) <= 128
                and len({field.get("field_id") for field in fields}) == len(fields)
                and all(
                    field.get("type") in {"string", "int64", "double", "bool"} for field in fields
                ),
                "INVALID_BINDING",
                "Input must use a checked flat Parquet representation",
            )
        else:
            ensure(
                rep is not None
                and rep["format"] == "parquet"
                and rep["representation_id"] == "normalized"
                and manifest["kind"] == "OBSERVATIONS",
                "INVALID_BINDING",
                "Input must use normalized Parquet",
            )
        ensure(
            rep["digest"] == selection["representation_digest"],
            "INVALID_BINDING",
            "Representation digest mismatch",
        )
        revision = selection.get("semantic_revision")
        semantics = next(
            (s["payload"] for s in value["semantic_revisions"] if s["revision"] == revision),
            None,
        )
        if spatial_selection:
            ensure(
                revision is None or semantics is not None,
                "INVALID_BINDING",
                "Semantic revision missing",
            )
            selector = selection["selector"]
            spatial = manifest.get("spatial", {})
            if selector["kind"] == "VECTOR_LAYER":
                ensure(
                    manifest["kind"] in {"SPATIAL_VECTOR", "OPERATOR_RESULT"}
                    and selector["layer_id"]
                    in {item["layer_id"] for item in spatial.get("layers", [])},
                    "INVALID_BINDING",
                    "Unknown vector layer",
                )
            else:
                available = {item["band_id"] for item in spatial.get("bands", [])}
                ensure(
                    manifest["kind"] == "SPATIAL_RASTER" and set(selector["band_ids"]) <= available,
                    "INVALID_BINDING",
                    "Unknown raster band",
                )
        elif table_selection:
            semantics = None
        else:
            ensure(semantics is not None, "INVALID_BINDING", "Semantic revision missing")
            ensure(
                set(selection["field_bindings"].values())
                <= {f["field_id"] for f in manifest["fields"]},
                "INVALID_BINDING",
                "Unknown field",
            )
        return {
            "selection": selection,
            "manifest": manifest,
            "semantics": semantics,
            "representation": rep,
            **(
                {
                    "table": {
                        "fields": manifest.get("tabular", manifest)["fields"],
                        "row_count": manifest.get("tabular", manifest)["row_count"],
                    }
                }
                if table_selection
                else {}
            ),
        }
