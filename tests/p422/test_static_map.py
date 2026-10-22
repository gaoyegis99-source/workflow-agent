import hashlib
import json
from pathlib import Path

import pytest
from platform_execution.cartography import verify_png

from tests.p42.test_spatial_operators import execute, inspect, request

FIXTURES = Path(__file__).parents[1] / "p42" / "fixtures"


def map_request(manifest, **changes):
    parameters = {
        "layer_id": "l0",
        "template_id": "v7-spatial-basic-v1",
        "output_crs": "EPSG:32650",
        "title": "缓冲区成果",
        "legend": True,
        "palette": "blue",
    }
    parameters.update(changes)
    return request("qgis-static-map", "qgis-static-map-v1", manifest, parameters)


@pytest.mark.parametrize("palette,legend", [("blue", True), ("green", False), ("orange", True)])
def test_real_qgis_static_map_reprojects_and_uses_trusted_template(
    spatial_images, tmp_path, palette, legend
):
    source = FIXTURES / "points-wgs84.gpkg"
    descriptor = inspect(spatial_images["native"], source, tmp_path)
    result, output = execute(
        spatial_images["qgis"],
        source,
        map_request(descriptor, palette=palette, legend=legend),
        tmp_path,
        "map-" + palette,
    )
    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.iterdir()} == {
        "started.marker",
        "map.png",
        "manifest.json",
        "method.json",
    }
    image = (output / "map.png").read_bytes()
    facts = verify_png(image, palette, legend)
    manifest = json.loads((output / "manifest.json").read_text())
    method = json.loads((output / "method.json").read_text())
    assert manifest["kind"] == "CARTOGRAPHIC_PRODUCT"
    assert manifest["source"]["digest"] == descriptor["source"]["digest"]
    assert manifest["image"]["digest"] == facts["digest"]
    assert manifest["image"]["size"] == len(image)
    assert manifest["cartography"]["output_crs"] == "EPSG:32650"
    assert manifest["cartography"]["page"]["pixel_width"] == 1754
    assert manifest["template"]["template_digest"] == method["template_digest"]
    assert set(manifest["template"]["resources"]["fonts"]) == {
        "NotoSansCJK-Bold.ttc",
        "NotoSansCJK-Regular.ttc",
    }


def test_static_map_is_deterministic_inside_the_frozen_image(spatial_images, tmp_path):
    source = FIXTURES / "points-utm.gpkg"
    descriptor = inspect(spatial_images["native"], source, tmp_path)
    digests = []
    manifests = []
    for name in ("first", "second"):
        result, output = execute(
            spatial_images["qgis"], source, map_request(descriptor), tmp_path, name
        )
        assert result.returncode == 0, result.stderr
        digests.append(hashlib.sha256((output / "map.png").read_bytes()).hexdigest())
        manifests.append(json.loads((output / "manifest.json").read_text()))
    assert digests[0] == digests[1]
    assert manifests[0] == manifests[1]


@pytest.mark.parametrize(
    "fixture,changes,reason",
    [
        ("empty.gpkg", {}, "EMPTY_INPUT"),
        ("points-utm.gpkg", {"output_crs": "EPSG:4326"}, "PROJECTED_METRE_CRS_REQUIRED"),
        ("points-utm.gpkg", {"layer_id": "missing"}, "LAYER_REQUIRED"),
    ],
)
def test_static_map_rejects_invalid_spatial_requests(
    spatial_images, tmp_path, fixture, changes, reason
):
    source = FIXTURES / fixture
    descriptor = inspect(spatial_images["native"], source, tmp_path)
    result, output = execute(
        spatial_images["qgis"],
        source,
        map_request(descriptor, **changes),
        tmp_path,
        fixture + reason,
    )
    assert result.returncode == 2
    assert json.loads((output / "error.json").read_text())["reason"] == reason
    assert not (output / "map.png").exists()
