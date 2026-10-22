import json
from pathlib import Path

import pytest
from platform_contracts.science import SpatialExecutionCreate, StaticMapParameters
from platform_core.application.catalog import OPERATORS, ports, target
from platform_core.application.workflow_compiler import compile_package
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


def map_target():
    item = next(value for value in OPERATORS if value["operator_id"] == "qgis-static-map")
    return target(item)


def parameters(**changes):
    value = {
        "layer_id": "l0",
        "template_id": "v7-spatial-basic-v1",
        "output_crs": "EPSG:32650",
        "title": "缓冲区成果",
        "legend": True,
        "palette": "blue",
    }
    value.update(changes)
    return value


def test_static_map_contract_and_catalog_are_closed():
    value = SpatialExecutionCreate(
        task_id="t",
        expected_spec_revision=1,
        target=map_target(),
        inputs={"vector": SELECTION},
        parameters=parameters(),
    )
    assert isinstance(value.parameters, StaticMapParameters)
    inputs, outputs = ports(map_target())
    assert set(inputs) == {"vector", "parameters"}
    assert outputs == {"image": "File", "manifest": "File", "method": "File"}


@pytest.mark.parametrize(
    "changes",
    [
        {"layer_id": "l1"},
        {"template_id": "user-template"},
        {"output_crs": "urn:ogc:def:crs:EPSG::32650"},
        {"title": " title"},
        {"title": "e\u0301"},
        {"title": "bad\u202etitle"},
        {"palette": "custom"},
        {"legend": "yes"},
    ],
)
def test_static_map_contract_rejects_untrusted_parameters(changes):
    with pytest.raises(ValidationError):
        SpatialExecutionCreate(
            task_id="t",
            expected_spec_revision=1,
            target=map_target(),
            inputs={"vector": SELECTION},
            parameters=parameters(**changes),
        )


def test_three_node_workflow_package_is_valid_and_old_package_remains_unchanged():
    current = json.loads(Path("examples/workflows/qgis-buffer-summary-map-v1.json").read_text())
    old = json.loads(Path("examples/workflows/qgis-buffer-summary-v1.json").read_text())
    compiled = compile_package(current)
    assert current["name"] == "qgis-buffer-summary-map-v1"
    assert len(compiled["invocations"]) == 3
    assert set(current["documents"]["main"]["outputs"]) == {
        "buffer",
        "buffer_manifest",
        "buffer_method",
        "summary",
        "summary_method",
        "map_image",
        "map_manifest",
        "map_method",
    }
    assert old["name"] == "qgis-buffer-summary-v1"
    assert set(old["documents"]["main"]["steps"]) == {"buffer", "summary"}
