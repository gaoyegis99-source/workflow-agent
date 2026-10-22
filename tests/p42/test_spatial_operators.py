import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def run_container(image, input_dir, output_dir):
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--user",
            f"{os.getuid() or 65532}:{os.getgid() or 65532}",
            "--mount",
            f"type=bind,src={input_dir.resolve()},dst=/input,readonly",
            "--mount",
            f"type=bind,src={output_dir.resolve()},dst=/output",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=200,
    )
    return result


def inspect(image, path, tmp_path):
    output = tmp_path / (path.stem + "-inspection.json")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid() or 65532}:{os.getgid() or 65532}",
            "--mount",
            f"type=bind,src={path.resolve()},dst=/input/source.gpkg,readonly",
            "--mount",
            f"type=bind,src={tmp_path.resolve()},dst=/output",
            image,
            "platform-spatial",
            "inspect",
            "--format",
            "geopackage",
            "--input",
            "/input/source.gpkg",
            "--output",
            "/output/" + output.name,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text())


def request(operator, implementation, manifest, parameters):
    source = manifest["source"]
    return {
        "kind": "OPERATOR",
        "target": {
            "kind": "OPERATOR",
            "operator_id": operator,
            "version_id": "1.0.0",
            "execution_binding": {"kind": "LOCAL", "implementation_id": implementation},
        },
        "bindings": {
            "vector": {
                "selection": {
                    "asset": {
                        "project_id": "p",
                        "asset_id": "a",
                        "version_id": "1",
                        "version_manifest_digest": "sha256:" + "1" * 64,
                    },
                    "representation_id": "original",
                    "representation_digest": source["digest"],
                    "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
                    "semantic_revision": None,
                },
                "representation": {
                    "representation_id": "original",
                    "format": "geopackage",
                    "digest": source["digest"],
                    "size": source["size"],
                },
                "manifest": manifest,
                "semantics": None,
            }
        },
        "parameters": parameters,
    }


def execute(image, source, payload, tmp_path, name):
    root = tmp_path / name
    input_dir, output_dir = root / "input", root / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()
    (input_dir / "vector.gpkg").write_bytes(source.read_bytes())
    (input_dir / "request.json").write_text(json.dumps(payload))
    result = run_container(image, input_dir, output_dir)
    return result, output_dir


def test_real_qgis_buffer_then_independent_native_summary(spatial_images, tmp_path):
    source = FIXTURES / "points-utm.gpkg"
    manifest = inspect(spatial_images["native"], source, tmp_path)
    buffer_request = request(
        "qgis-buffer",
        "qgis-buffer-v1",
        manifest,
        {"distance": 100, "unit": "metre", "output_crs": "EPSG:32650", "segments": 8},
    )
    result, output = execute(spatial_images["qgis"], source, buffer_request, tmp_path, "buffer")
    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.iterdir()} == {
        "started.marker",
        "buffer.gpkg",
        "manifest.json",
        "method.json",
    }
    produced = json.loads((output / "manifest.json").read_text())
    layer = produced["spatial"]["layers"][0]
    assert layer["name"] == "buffer"
    assert layer["feature_count"] == 2
    assert layer["geometry_valid"] is True
    assert layer["crs"]["authority"] == "EPSG:32650"
    assert layer["bounds"]["min_x"] < 500000
    vector_digest = "sha256:" + hashlib.sha256((output / "buffer.gpkg").read_bytes()).hexdigest()
    assert produced["source"]["digest"] == vector_digest

    summary_request = request(
        "spatial-vector-summary",
        "spatial-vector-summary-v1",
        produced,
        {"layer_id": "l0"},
    )
    result, summary_output = execute(
        spatial_images["native"], output / "buffer.gpkg", summary_request, tmp_path, "summary"
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((summary_output / "summary.json").read_text())
    assert summary["source_digest"] == vector_digest
    assert summary["layer"]["feature_count"] == layer["feature_count"]
    assert summary["layer"]["bounds"] == layer["bounds"]


def test_projected_feet_input_is_explicitly_reprojected_to_metre_output(spatial_images, tmp_path):
    source = FIXTURES / "points-feet.gpkg"
    manifest = inspect(spatial_images["native"], source, tmp_path)
    assert manifest["spatial"]["layers"][0]["crs"]["linear_unit"] != "metre"
    payload = request(
        "qgis-buffer",
        "qgis-buffer-v1",
        manifest,
        {"distance": 0.1, "unit": "kilometre", "output_crs": "EPSG:32618", "segments": 8},
    )
    result, output = execute(spatial_images["qgis"], source, payload, tmp_path, "feet")
    assert result.returncode == 0, result.stderr
    descriptor = json.loads((output / "manifest.json").read_text())
    layer = descriptor["spatial"]["layers"][0]
    assert layer["crs"]["authority"] == "EPSG:32618"
    assert layer["crs"]["linear_unit"] == "metre"
    assert layer["feature_count"] == manifest["spatial"]["layers"][0]["feature_count"]


@pytest.mark.parametrize(
    "fixture,parameters,reason",
    [
        (
            "points-wgs84.gpkg",
            {"distance": 100, "unit": "metre", "output_crs": "EPSG:32650", "segments": 8},
            "PROJECTED_CRS_REQUIRED",
        ),
        (
            "empty.gpkg",
            {"distance": 100, "unit": "metre", "output_crs": "EPSG:32650", "segments": 8},
            "EMPTY_INPUT",
        ),
        (
            "points-utm.gpkg",
            {"distance": 100, "unit": "metre", "output_crs": "EPSG:4326", "segments": 8},
            "PROJECTED_METRE_CRS_REQUIRED",
        ),
    ],
)
def test_buffer_rejects_scientifically_invalid_inputs(
    spatial_images, tmp_path, fixture, parameters, reason
):
    source = FIXTURES / fixture
    manifest = inspect(spatial_images["native"], source, tmp_path)
    payload = request("qgis-buffer", "qgis-buffer-v1", manifest, parameters)
    result, output = execute(spatial_images["qgis"], source, payload, tmp_path, fixture)
    assert result.returncode == 2
    assert json.loads((output / "error.json").read_text())["reason"] == reason
    assert not (output / "buffer.gpkg").exists()
