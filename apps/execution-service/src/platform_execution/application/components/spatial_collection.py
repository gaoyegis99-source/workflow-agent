"""Spatial output validation and provenance collection."""

import hashlib
import json
import math

from platform_contracts.service import ensure

from platform_execution.cartography import verify_png

from ._schemas import output_schema
from .protocols import ActionComponent


class SpatialOutputCollector(ActionComponent):
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
            return self.execution.transition(
                row,
                "REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=files,
                manifest=manifest,
            )
        return self.execution.transition(row, "PUBLISHING", manifest=manifest)
