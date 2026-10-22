import hashlib
from pathlib import Path

import pytest

P = "/internal/projects/project"
FIXTURES = Path(__file__).parent / "fixtures"


def upload(client, name, format_id, key=None):
    content = (FIXTURES / name).read_bytes()
    created = client.post(
        P + "/uploads",
        headers={"Idempotency-Key": key or name},
        json={
            "filename": name,
            "format": format_id,
            "size": len(content),
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    assert client.put(f"{P}/uploads/{upload_id}/content", content=content).status_code == 200
    queued = client.post(
        f"{P}/uploads/{upload_id}/finalize",
        json={"kind": "SPATIAL", "profile": "strict-spatial-v1"},
    )
    assert queued.status_code == 202, queued.text
    return upload_id


def imported(spatial_data, name="points-utm.gpkg", format_id="geopackage"):
    client, worker, _ = spatial_data
    upload_id = upload(client, name, format_id)
    assert worker.tick()
    return client.get(f"{P}/uploads/{upload_id}").json()


def test_geopackage_import_layer_crs_semantics_and_resolve(spatial_data):
    client, _, _ = spatial_data
    result = imported(spatial_data)
    assert result["status"] == "READY", result
    ref = result["asset"]
    asset = client.get(f"{P}/assets/{ref['asset_id']}/versions/1").json()
    manifest = asset["manifest"]
    assert manifest["driver"] == "GPKG"
    layer = manifest["spatial"]["layers"][0]
    assert layer["layer_id"] == "l0"
    assert layer["feature_count"] == 2
    assert layer["crs"]["authority"] == "EPSG:32650"
    assert layer["crs"]["coordinate_order"] == "xy"
    assert layer["crs"]["authority_axis_order"]
    semantic = client.post(
        f"{P}/assets/{ref['asset_id']}/semantic-revisions",
        headers={"Idempotency-Key": "semantic"},
        json={
            "expected_revision": 0,
            "version_id": "1",
            "semantics": {
                "profile": "spatial-generic-v1",
                "annotations": [
                    {
                        "namespace": "https://example.org/groundwater",
                        "term": "monitoring_well",
                        "target": {"kind": "LAYER", "layer_id": "l0"},
                        "value": True,
                    }
                ],
            },
        },
    )
    assert semantic.status_code == 201, semantic.text
    selection = {
        "asset": ref,
        "representation_id": "original",
        "representation_digest": manifest["representations"][0]["digest"],
        "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
        "semantic_revision": 1,
    }
    resolved = client.post(P + "/resolve", json=selection)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["semantics"]["profile"] == "spatial-generic-v1"
    wrong = selection | {"selector": {"kind": "VECTOR_LAYER", "layer_id": "missing"}}
    assert client.post(P + "/resolve", json=wrong).status_code == 409


def test_geotiff_import_bands_nodata_and_download(spatial_data):
    client, _, _ = spatial_data
    result = imported(spatial_data, "multi-band-nodata.tif", "geotiff")
    assert result["status"] == "READY", result
    ref = result["asset"]
    asset = client.get(f"{P}/assets/{ref['asset_id']}/versions/1").json()
    raster = asset["manifest"]["spatial"]
    assert (raster["width"], raster["height"]) == (8, 6)
    assert [band["band_id"] for band in raster["bands"]] == ["b1", "b2", "b3"]
    assert all(band["nodata"] == -9999 for band in raster["bands"])
    downloaded = client.get(
        f"{P}/assets/{ref['asset_id']}/versions/1/representations/original/content"
    )
    assert downloaded.content == (FIXTURES / "multi-band-nodata.tif").read_bytes()


@pytest.mark.parametrize(
    "name,format_id,reason",
    [
        ("ambiguous-layers.gpkg", "geopackage", "AMBIGUOUS_LAYER"),
        ("missing-crs.gpkg", "geopackage", "CRS"),
        ("invalid-geometry.gpkg", "geopackage", "INVALID_GEOMETRY"),
        ("empty-geometry.gpkg", "geopackage", "EMPTY_GEOMETRY"),
        ("corrupt.gpkg", "geopackage", "FORMAT"),
        ("missing-crs.tif", "geotiff", "CRS"),
        ("corrupt.tif", "geotiff", "FORMAT"),
    ],
)
def test_invalid_spatial_inputs_are_never_published(spatial_data, name, format_id, reason):
    client, _, _ = spatial_data
    result = imported(spatial_data, name, format_id)
    assert result["status"] == "FAILED", result
    assert reason in result["error"]["reason"]


def test_empty_and_multilayer_assets_import_without_guessing(spatial_data):
    client, _, _ = spatial_data
    empty = imported(spatial_data, "empty.gpkg")
    assert empty["status"] == "READY"
    empty_asset = client.get(f"{P}/assets/{empty['asset']['asset_id']}/versions/1").json()
    assert empty_asset["manifest"]["spatial"]["layers"][0]["empty"] is True
    multi = imported(spatial_data, "multi-layer.gpkg")
    assert multi["status"] == "READY"
    asset = client.get(f"{P}/assets/{multi['asset']['asset_id']}/versions/1").json()
    assert [layer["layer_id"] for layer in asset["manifest"]["spatial"]["layers"]] == [
        "l0",
        "l1",
    ]
