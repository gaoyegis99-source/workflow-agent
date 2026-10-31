"""Generate auditable P4.2.1 spatial fixtures with fixed GDAL behavior."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from osgeo import gdal, ogr, osr

ROOT = Path(__file__).resolve().parent / "fixtures"
FIXED_DATE = "2026-01-01T00:00:00Z"


def srs(epsg: int):
    value = osr.SpatialReference()
    value.ImportFromEPSG(epsg)
    value.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return value


def vector(
    name: str,
    *,
    epsg: int | None = 32650,
    layers: list[tuple[str, list[str]]] | None = None,
    invalid: bool = False,
    empty_geometry: bool = False,
):
    path = ROOT / name
    driver = ogr.GetDriverByName("GPKG")
    dataset = driver.CreateDataSource(str(path))
    assert dataset is not None
    defaults = [("monitoring_points", ["500000 3000000", "500200 3000200"])]
    for layer_name, points in layers or defaults:
        geometry_type = ogr.wkbPolygon if invalid else ogr.wkbPoint
        layer = dataset.CreateLayer(layer_name, srs(epsg) if epsg else None, geometry_type)
        layer.CreateField(ogr.FieldDefn("station_id", ogr.OFTString))
        for index, coordinate in enumerate(points):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("station_id", f"S{index + 1}")
            if not empty_geometry:
                if invalid:
                    feature.SetGeometry(
                        ogr.CreateGeometryFromWkt(
                            "POLYGON ((500000 3000000, 500100 3000100, "
                            "500000 3000100, 500100 3000000, 500000 3000000))"
                        )
                    )
                else:
                    feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({coordinate})"))
            assert layer.CreateFeature(feature) == 0
    dataset = None
    # Make GeoPackage metadata deterministic across regeneration.
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_UPDATE)
    dataset.ExecuteSQL(f"UPDATE gpkg_contents SET last_change = '{FIXED_DATE}'")
    dataset = None


def raster(name: str, *, epsg: int | None = 32650, bands: int = 1, nodata=None):
    path = ROOT / name
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), 8, 6, bands, gdal.GDT_Int16, options=["COMPRESS=DEFLATE"]
    )
    dataset.SetGeoTransform([500000, 10, 0, 3000000, 0, -10])
    if epsg:
        dataset.SetSpatialRef(srs(epsg))
    for index in range(1, bands + 1):
        band = dataset.GetRasterBand(index)
        band.Fill(index * 10)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        band.SetColorInterpretation(gdal.GCI_GrayIndex)
    dataset = None


def digest(path: Path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for path in ROOT.iterdir():
        path.unlink()
    vector("points-utm.gpkg")
    vector(
        "points-feet.gpkg",
        epsg=2263,
        layers=[("feet_points", ["1000000 200000", "1000100 200100"])],
    )
    vector("points-wgs84.gpkg", epsg=4326, layers=[("wells", ["116.3 39.9", "116.4 40.0"])])
    vector("empty.gpkg", layers=[("empty_points", [])])
    vector(
        "multi-layer.gpkg",
        layers=[("wells", ["500000 3000000"]), ("districts", ["500300 3000300"])],
    )
    vector(
        "ambiguous-layers.gpkg",
        layers=[("Cafe\u0301", ["500000 3000000"]), ("Caf\u00e9", ["500300 3000300"])],
    )
    vector("missing-crs.gpkg", epsg=None)
    vector("invalid-geometry.gpkg", invalid=True)
    vector("empty-geometry.gpkg", empty_geometry=True)
    raster("single-band.tif")
    raster("multi-band-nodata.tif", bands=3, nodata=-9999)
    raster("missing-crs.tif", epsg=None)
    (ROOT / "corrupt.gpkg").write_bytes(b"SQLite format 3\0not-a-geopackage")
    (ROOT / "corrupt.tif").write_bytes(b"II*\0not-a-geotiff")
    checksums = {
        path.name: {"size": path.stat().st_size, "digest": digest(path)}
        for path in sorted(ROOT.iterdir())
    }
    (ROOT / "checksums.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    os.environ.setdefault("OGR_CURRENT_DATE", FIXED_DATE)
    gdal.UseExceptions()
    main()
