import pytest
from platform_contracts.science import (
    BufferParameters,
    RasterSelector,
    SpatialAnnotation,
    SpatialExecutionCreate,
    SpatialUploadFinalize,
    UploadCreate,
)
from platform_core.application.catalog import OPERATORS, operator, ports, target
from pydantic import ValidationError

REF = {
    "project_id": "p",
    "asset_id": "a",
    "version_id": "1",
    "version_manifest_digest": "sha256:" + "1" * 64,
}
SELECTION = {
    "asset": REF,
    "representation_id": "original",
    "representation_digest": "sha256:" + "2" * 64,
    "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
    "semantic_revision": None,
}


def test_spatial_contracts_and_catalog_are_closed():
    qgis = next(item for item in OPERATORS if item["operator_id"] == "qgis-buffer")
    qgis_target = target(qgis)
    assert (
        UploadCreate(
            filename="points.gpkg",
            format="geopackage",
            size=1,
            digest="sha256:" + "0" * 64,
        ).format
        == "geopackage"
    )
    assert SpatialUploadFinalize(kind="SPATIAL").profile == "strict-spatial-v1"
    value = SpatialExecutionCreate(
        task_id="t",
        expected_spec_revision=1,
        target=qgis_target,
        inputs={"vector": SELECTION},
        parameters={
            "distance": 1,
            "unit": "kilometre",
            "output_crs": "EPSG:32650",
            "segments": 8,
        },
    )
    assert value.parameters.distance == 1
    assert operator(qgis_target)["runtime"] == "QGIS"
    inputs, outputs = ports(qgis_target)
    assert set(inputs) == {"vector", "parameters"}
    assert outputs == {"vector": "File", "manifest": "File", "method": "File"}


@pytest.mark.parametrize(
    "changes",
    [
        {"distance": 0},
        {"distance": float("nan")},
        {"distance": 10001, "unit": "kilometre"},
        {"unit": "degree"},
        {"output_crs": "urn:ogc:def:crs:EPSG::32650"},
    ],
)
def test_invalid_buffer_parameters_are_rejected(changes):
    body = {"distance": 100, "unit": "metre", "output_crs": "EPSG:32650", "segments": 8}
    body.update(changes)
    with pytest.raises(ValidationError):
        BufferParameters.model_validate(body)


def test_semantic_namespace_values_are_bounded_and_finite():
    value = SpatialAnnotation(
        namespace="https://example.org/groundwater",
        term="monitoring_well",
        target={"kind": "LAYER", "layer_id": "l0"},
        value=True,
    )
    assert value.value is True
    for namespace, annotation_value in [("relative", True), ("https://example.org", float("inf"))]:
        with pytest.raises(ValidationError):
            SpatialAnnotation(
                namespace=namespace,
                term="x",
                target={"kind": "ASSET"},
                value=annotation_value,
            )


def test_operator_and_parameter_shape_must_match():
    summary = next(item for item in OPERATORS if item["operator_id"] == "spatial-vector-summary")
    with pytest.raises(ValidationError):
        SpatialExecutionCreate(
            task_id="t",
            expected_spec_revision=1,
            target=target(summary),
            inputs={"vector": SELECTION},
            parameters={"distance": 1, "unit": "metre", "output_crs": "EPSG:32650"},
        )


def test_raster_band_selection_rejects_duplicates():
    with pytest.raises(ValidationError):
        RasterSelector(kind="RASTER", band_ids=["b1", "b1"])
