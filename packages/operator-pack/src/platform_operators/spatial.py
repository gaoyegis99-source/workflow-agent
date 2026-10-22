"""Trusted spatial inspector and fixed spatial operators.

The module imports GDAL/QGIS lazily so the ordinary P3 image remains independent
of those system SDKs.  Paths and algorithm names are selected by the trusted
entrypoint, never by a client request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import unicodedata
from pathlib import Path

MAX_LAYERS = 64
MAX_FIELDS = 128
MAX_FEATURES = 100_000
MAX_BANDS = 64
MAX_RASTER_SAMPLES = 100_000_000
MAP_TEMPLATE = {
    "template_id": "v7-spatial-basic-v1",
    "page": {"width_mm": 297, "height_mm": 210, "dpi": 150},
    "margins_mm": 12,
    "title_height_mm": 15,
    "legend_width_mm": 45,
    "font": {"family": "Noto Sans CJK SC", "package": "fonts-noto-cjk=1:20230817+repack1-3"},
    "north_arrow": "/usr/share/qgis/svg/arrows/NorthArrow_01.svg",
    "palettes": {
        "blue": {"fill": "#4C78A8", "outline": "#244A6B"},
        "green": {"fill": "#59A14F", "outline": "#2F6630"},
        "orange": {"fill": "#F28E2B", "outline": "#A44B00"},
    },
}


class SpatialInputError(ValueError):
    pass


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _single_file(dataset, source: Path) -> None:
    expected = source.resolve()
    files = [Path(value).resolve() for value in (dataset.GetFileList() or [str(source)])]
    if files != [expected]:
        raise SpatialInputError("EXTERNAL_FILE_REFERENCE")


def _crs(spatial_ref) -> dict:
    if spatial_ref is None:
        raise SpatialInputError("CRS_REQUIRED")
    from osgeo import osr

    value = spatial_ref.Clone()
    if (
        value.Validate() != 0
        or not (value.IsProjected() or value.IsGeographic())
        or "undefined" in (value.GetName() or "").casefold()
    ):
        raise SpatialInputError("CRS_INVALID")
    try:
        value.AutoIdentifyEPSG()
    except RuntimeError:
        pass
    wkt = value.ExportToWkt(["FORMAT=WKT2_2019"])
    if not wkt:
        raise SpatialInputError("CRS_REQUIRED")
    authority_name = value.GetAuthorityName(None)
    authority_code = value.GetAuthorityCode(None)
    orientation_names = {
        osr.OAO_Other: "other",
        osr.OAO_North: "north",
        osr.OAO_South: "south",
        osr.OAO_East: "east",
        osr.OAO_West: "west",
        osr.OAO_Up: "up",
        osr.OAO_Down: "down",
    }
    axes = []
    for index in range(value.GetAxesCount()):
        axes.append(
            {
                "name": value.GetAxisName(None, index) or f"axis-{index}",
                "orientation": orientation_names.get(
                    value.GetAxisOrientation(None, index), "other"
                ),
            }
        )
    projected = bool(value.IsProjected())
    return {
        "wkt2": wkt,
        "authority": (
            f"{authority_name}:{authority_code}" if authority_name and authority_code else None
        ),
        "projected": projected,
        "geographic": bool(value.IsGeographic()),
        "linear_unit": value.GetLinearUnitsName() if projected else None,
        "linear_unit_to_metre": value.GetLinearUnits() if projected else None,
        "authority_axis_order": axes,
        "coordinate_order": "xy",
    }


def inspect_geopackage(source: Path) -> dict:
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    try:
        dataset = gdal.OpenEx(str(source), gdal.OF_VECTOR | gdal.OF_READONLY)
    except RuntimeError as exc:
        raise SpatialInputError("FORMAT_MISMATCH") from exc
    if dataset is None or dataset.GetDriver().ShortName != "GPKG":
        raise SpatialInputError("FORMAT_MISMATCH")
    _single_file(dataset, source)
    count = dataset.GetLayerCount()
    if not 0 < count <= MAX_LAYERS:
        raise SpatialInputError("LAYER_LIMIT")
    names, layers, total = set(), [], 0
    for index in range(count):
        layer = dataset.GetLayerByIndex(index)
        name = layer.GetName()
        normalized = unicodedata.normalize("NFKC", name).casefold()
        if not normalized or normalized in names:
            raise SpatialInputError("AMBIGUOUS_LAYER")
        names.add(normalized)
        definition = layer.GetLayerDefn()
        if definition.GetFieldCount() > MAX_FIELDS:
            raise SpatialInputError("FIELD_LIMIT")
        fields = []
        for field_index in range(definition.GetFieldCount()):
            field = definition.GetFieldDefn(field_index)
            fields.append(
                {
                    "field_id": f"f{field_index}",
                    "name": field.GetName(),
                    "type": field.GetTypeName(),
                }
            )
        feature_count = layer.GetFeatureCount(force=1)
        if feature_count < 0:
            raise SpatialInputError("FEATURE_COUNT_UNKNOWN")
        total += feature_count
        if total > MAX_FEATURES:
            raise SpatialInputError("FEATURE_LIMIT")
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                raise SpatialInputError("EMPTY_GEOMETRY")
            if not geometry.IsValid():
                raise SpatialInputError("INVALID_GEOMETRY")
        extent = None
        if feature_count:
            minimum_x, maximum_x, minimum_y, maximum_y = layer.GetExtent(force=1)
            extent = {
                "min_x": minimum_x,
                "min_y": minimum_y,
                "max_x": maximum_x,
                "max_y": maximum_y,
                "coordinate_order": "xy",
            }
        geometry_type = definition.GetGeomType()
        layers.append(
            {
                "layer_id": f"l{index}",
                "name": name,
                "geometry_type": ogr.GeometryTypeToName(ogr.GT_Flatten(geometry_type)),
                "has_z": bool(ogr.GT_HasZ(geometry_type)),
                "has_m": bool(ogr.GT_HasM(geometry_type)),
                "feature_count": feature_count,
                "empty": feature_count == 0,
                "geometry_valid": True,
                "bounds": extent,
                "crs": _crs(layer.GetSpatialRef()),
                "fields": fields,
            }
        )
    return {
        "schema_version": 1,
        "kind": "SPATIAL_VECTOR",
        "driver": "GPKG",
        "spatial": {"layers": layers, "total_feature_count": total},
    }


def inspect_geotiff(source: Path) -> dict:
    from osgeo import gdal

    gdal.UseExceptions()
    try:
        dataset = gdal.OpenEx(str(source), gdal.OF_RASTER | gdal.OF_READONLY)
    except RuntimeError as exc:
        raise SpatialInputError("FORMAT_MISMATCH") from exc
    if dataset is None or dataset.GetDriver().ShortName != "GTiff":
        raise SpatialInputError("FORMAT_MISMATCH")
    _single_file(dataset, source)
    if dataset.GetSubDatasets():
        raise SpatialInputError("SUBDATASETS_UNSUPPORTED")
    width, height, band_count = dataset.RasterXSize, dataset.RasterYSize, dataset.RasterCount
    if not 0 < band_count <= MAX_BANDS or width * height * band_count > MAX_RASTER_SAMPLES:
        raise SpatialInputError("RASTER_LIMIT")
    transform = dataset.GetGeoTransform(can_return_null=True)
    if transform is None:
        raise SpatialInputError("GEOREFERENCE_REQUIRED")
    corners = [
        gdal.ApplyGeoTransform(transform, x, y)
        for x, y in ((0, 0), (width, 0), (0, height), (width, height))
    ]
    bands = []
    for index in range(1, band_count + 1):
        band = dataset.GetRasterBand(index)
        bands.append(
            {
                "band_id": f"b{index}",
                "index": index,
                "data_type": gdal.GetDataTypeName(band.DataType),
                "nodata": band.GetNoDataValue(),
                "color_interpretation": gdal.GetColorInterpretationName(
                    band.GetColorInterpretation()
                ),
                "checksum": band.Checksum(),
            }
        )
    return {
        "schema_version": 1,
        "kind": "SPATIAL_RASTER",
        "driver": "GTiff",
        "spatial": {
            "crs": _crs(dataset.GetSpatialRef()),
            "bounds": {
                "min_x": min(value[0] for value in corners),
                "min_y": min(value[1] for value in corners),
                "max_x": max(value[0] for value in corners),
                "max_y": max(value[1] for value in corners),
                "coordinate_order": "xy",
            },
            "width": width,
            "height": height,
            "geotransform": list(transform),
            "pixel_size": [transform[1], transform[5]],
            "bands": bands,
        },
    }


def inspect(source: Path, format_id: str) -> dict:
    if not source.is_file() or source.is_symlink():
        raise SpatialInputError("SOURCE_UNAVAILABLE")
    result = (
        inspect_geopackage(source)
        if format_id == "geopackage"
        else inspect_geotiff(source)
        if format_id == "geotiff"
        else None
    )
    if result is None:
        raise SpatialInputError("FORMAT_UNSUPPORTED")
    result["source"] = {"digest": _digest(source), "size": source.stat().st_size}
    return result


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _resource_contract() -> tuple[dict, str]:
    font_root = Path("/usr/share/fonts/opentype/noto")
    font_files = sorted(font_root.glob("NotoSansCJK-*.ttc"))
    arrow = Path(MAP_TEMPLATE["north_arrow"])
    if not font_files or not arrow.is_file():
        raise SpatialInputError("TEMPLATE_RESOURCE_MISSING")
    resources = {
        "fonts": {path.name: _digest(path) for path in font_files},
        "north_arrow": {"name": arrow.name, "digest": _digest(arrow)},
    }
    payload = {"template": MAP_TEMPLATE, "resources": resources}
    fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return resources, fingerprint


def _map_extent(bounds, frame_ratio):
    values = [bounds.xMinimum(), bounds.yMinimum(), bounds.xMaximum(), bounds.yMaximum()]
    if not all(math.isfinite(value) for value in values):
        raise SpatialInputError("TRANSFORM_FAILED")
    width, height = bounds.width(), bounds.height()
    pad_x, pad_y = max(abs(width) * 0.05, 10.0), max(abs(height) * 0.05, 10.0)
    width, height = width + 2 * pad_x, height + 2 * pad_y
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise SpatialInputError("TRANSFORM_FAILED")
    centre_x, centre_y = bounds.center().x(), bounds.center().y()
    if width / height < frame_ratio:
        width = height * frame_ratio
    else:
        height = width / frame_ratio
    from qgis.core import QgsRectangle

    return QgsRectangle(
        centre_x - width / 2,
        centre_y - height / 2,
        centre_x + width / 2,
        centre_y + height / 2,
    )


def run_static_map(request: dict, source: Path, output: Path) -> None:
    binding = request["bindings"]["vector"]
    parameters = request["parameters"]
    layer_id = parameters["layer_id"]
    source_layer = _layer(binding["manifest"], layer_id)
    if source_layer["empty"]:
        raise SpatialInputError("EMPTY_INPUT")
    if parameters["template_id"] != MAP_TEMPLATE["template_id"]:
        raise SpatialInputError("TEMPLATE_UNSUPPORTED")
    if parameters["palette"] not in MAP_TEMPLATE["palettes"]:
        raise SpatialInputError("PALETTE_UNSUPPORTED")
    selection = binding.get("selection", {})
    selector = selection.get("selector")
    if selector is not None and selector != {"kind": "VECTOR_LAYER", "layer_id": layer_id}:
        raise SpatialInputError("LAYER_REQUIRED")

    from osgeo import gdal, osr
    from qgis.core import (
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsFillSymbol,
        QgsLayoutExporter,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemPicture,
        QgsLayoutItemScaleBar,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLineSymbol,
        QgsMarkerSymbol,
        QgsPrintLayout,
        QgsProject,
        QgsSingleSymbolRenderer,
        QgsUnitTypes,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor, QFont

    os.environ.setdefault("HOME", "/tmp/home")
    os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", "/tmp/qgis-profile")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp/runtime-platform"))
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    Path(os.environ["HOME"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(os.environ["QGIS_CUSTOM_CONFIG_PATH"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    staged_source = Path("/tmp/source.gpkg")
    shutil.copyfile(source, staged_source)
    project = QgsProject()
    vector = layout = title = map_item = legend = scale = north = None
    try:
        output_crs = QgsCoordinateReferenceSystem(parameters["output_crs"])
        if (
            not output_crs.isValid()
            or output_crs.isGeographic()
            or output_crs.mapUnits() != QgsUnitTypes.DistanceUnit.DistanceMeters
        ):
            raise SpatialInputError("PROJECTED_METRE_CRS_REQUIRED")
        vector = QgsVectorLayer(
            f"{staged_source}|layername={source_layer['name']}", source_layer["name"], "ogr"
        )
        if not vector.isValid() or vector.featureCount() <= 0:
            raise SpatialInputError("EMPTY_INPUT")
        geometry = vector.geometryType()
        if geometry not in {
            Qgis.GeometryType.Point,
            Qgis.GeometryType.Line,
            Qgis.GeometryType.Polygon,
        }:
            raise SpatialInputError("GEOMETRY_UNSUPPORTED")
        palette = MAP_TEMPLATE["palettes"][parameters["palette"]]
        if geometry == Qgis.GeometryType.Point:
            vector.setRenderer(
                QgsSingleSymbolRenderer(
                    QgsMarkerSymbol.createSimple(
                        {
                            "name": "circle",
                            "color": palette["fill"] + "E6",
                            "outline_color": palette["outline"],
                            "size": "2.5",
                        }
                    )
                )
            )
        elif geometry == Qgis.GeometryType.Line:
            vector.setRenderer(
                QgsSingleSymbolRenderer(
                    QgsLineSymbol.createSimple(
                        {"line_color": palette["outline"], "line_width": "0.8"}
                    )
                )
            )
        else:
            vector.setRenderer(
                QgsSingleSymbolRenderer(
                    QgsFillSymbol.createSimple(
                        {
                            "color": palette["fill"] + "59",
                            "outline_color": palette["outline"],
                            "outline_width": "0.4",
                        }
                    )
                )
            )
        project.setCrs(output_crs)
        project.addMapLayer(vector)
        transform = QgsCoordinateTransform(vector.crs(), output_crs, project)
        transformed = transform.transformBoundingBox(vector.extent())
        map_width = 228 if parameters["legend"] else 273
        map_height = 159
        extent = _map_extent(transformed, map_width / map_height)

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        page = layout.pageCollection().page(0)
        page.setPageSize(
            QgsLayoutSize(
                MAP_TEMPLATE["page"]["width_mm"],
                MAP_TEMPLATE["page"]["height_mm"],
                QgsUnitTypes.LayoutMillimeters,
            )
        )
        title = QgsLayoutItemLabel(layout)
        title.setText(parameters["title"])
        title.setFont(QFont(MAP_TEMPLATE["font"]["family"], 14, QFont.Weight.Bold))
        title.attemptMove(QgsLayoutPoint(12, 9, QgsUnitTypes.LayoutMillimeters))
        title.attemptResize(QgsLayoutSize(273, 15, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(title)

        map_item = QgsLayoutItemMap(layout)
        layout.addLayoutItem(map_item)
        map_item.attemptMove(QgsLayoutPoint(12, 28, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(map_width, map_height, QgsUnitTypes.LayoutMillimeters))
        map_item.setLayers([vector])
        map_item.setCrs(output_crs)
        map_item.setExtent(extent)
        map_item.setFrameEnabled(True)

        if parameters["legend"]:
            legend = QgsLayoutItemLegend(layout)
            layout.addLayoutItem(legend)
            legend.setTitle("Legend")
            legend.setLinkedMap(map_item)
            legend.setLegendFilterByMapEnabled(True)
            legend.setFontColor(QColor("#222222"))
            legend.attemptMove(QgsLayoutPoint(246, 32, QgsUnitTypes.LayoutMillimeters))
            legend.attemptResize(QgsLayoutSize(39, 70, QgsUnitTypes.LayoutMillimeters))

        scale = QgsLayoutItemScaleBar(layout)
        layout.addLayoutItem(scale)
        scale.setStyle("Single Box")
        scale.setLinkedMap(map_item)
        scale.setUnits(QgsUnitTypes.DistanceMeters)
        scale.setUnitLabel("m")
        scale.setNumberOfSegments(4)
        scale.setNumberOfSegmentsLeft(0)
        scale.applyDefaultSize(QgsUnitTypes.DistanceMeters)
        scale.attemptMove(QgsLayoutPoint(18, 177, QgsUnitTypes.LayoutMillimeters))

        north = QgsLayoutItemPicture(layout)
        layout.addLayoutItem(north)
        north.setPicturePath(MAP_TEMPLATE["north_arrow"])
        north.attemptMove(QgsLayoutPoint(218, 34, QgsUnitTypes.LayoutMillimeters))
        north.attemptResize(QgsLayoutSize(14, 22, QgsUnitTypes.LayoutMillimeters))

        resources, template_digest = _resource_contract()
        image = output / "map.png"
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = MAP_TEMPLATE["page"]["dpi"]
        settings.imageSize = QSize(1754, 1240)
        settings.exportMetadata = False
        status = QgsLayoutExporter(layout).exportToImage(str(image), settings)
        if status != QgsLayoutExporter.ExportResult.Success or not image.is_file():
            raise SpatialInputError("MAP_EXPORT_FAILED")
        image_digest = _digest(image)
        if image.stat().st_size > 20 * 1024 * 1024:
            raise SpatialInputError("MAP_OUTPUT_LIMIT")
        final_extent = map_item.extent()
        manifest = {
            "schema_version": 1,
            "kind": "CARTOGRAPHIC_PRODUCT",
            "format": "PNG",
            "source": {
                "digest": binding["representation"]["digest"],
                "layer_id": layer_id,
            },
            "template": {
                "template_id": MAP_TEMPLATE["template_id"],
                "template_digest": template_digest,
                "resources": resources,
            },
            "cartography": {
                "output_crs": parameters["output_crs"],
                "coordinate_order": "xy",
                "extent": {
                    "min_x": final_extent.xMinimum(),
                    "min_y": final_extent.yMinimum(),
                    "max_x": final_extent.xMaximum(),
                    "max_y": final_extent.yMaximum(),
                },
                "page": {
                    **MAP_TEMPLATE["page"],
                    "pixel_width": 1754,
                    "pixel_height": 1240,
                },
                "title": parameters["title"],
                "legend": parameters["legend"],
                "palette": parameters["palette"],
            },
            "image": {
                "digest": image_digest,
                "size": image.stat().st_size,
                "media_type": "image/png",
                "width": 1754,
                "height": 1240,
            },
        }
        _write_json(output / "manifest.json", manifest)
        _write_json(
            output / "method.json",
            {
                "schema_version": 1,
                "operator_id": "qgis-static-map",
                "version_id": "1.0.0",
                "method": "QGIS Print Layout with explicit CRS and trusted template",
                "qgis_version": Qgis.QGIS_VERSION,
                "gdal_version": gdal.VersionInfo("RELEASE_NAME"),
                "proj_version": ".".join(
                    str(value)
                    for value in (
                        osr.GetPROJVersionMajor(),
                        osr.GetPROJVersionMinor(),
                        osr.GetPROJVersionMicro(),
                    )
                ),
                "template_id": MAP_TEMPLATE["template_id"],
                "template_digest": template_digest,
                "parameters": parameters,
                "source_digest": binding["representation"]["digest"],
            },
        )
        # PyQGIS Print Layout can crash while Python tears down its QObject graph.
        # This command is a one-shot container, so commit every output and exit
        # without interpreter finalizers after the trusted export is complete.
        for path in output.iterdir():
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        os._exit(0)
    finally:
        staged_source.unlink(missing_ok=True)


def _layer(descriptor: dict, layer_id: str) -> dict:
    matches = [
        value
        for value in descriptor.get("spatial", {}).get("layers", [])
        if value.get("layer_id") == layer_id
    ]
    if len(matches) != 1:
        raise SpatialInputError("LAYER_REQUIRED")
    return matches[0]


def run_buffer(request: dict, source: Path, output: Path) -> None:
    binding = request["bindings"]["vector"]
    selector = binding["selection"]["selector"]
    descriptor = {
        "kind": binding["manifest"]["kind"],
        "spatial": binding["manifest"]["spatial"],
    }
    source_layer = _layer(descriptor, selector["layer_id"])
    if source_layer["empty"]:
        raise SpatialInputError("EMPTY_INPUT")
    if not source_layer["crs"]["projected"]:
        raise SpatialInputError("PROJECTED_CRS_REQUIRED")
    parameters = request["parameters"]
    distance = float(parameters["distance"]) * (1000 if parameters["unit"] == "kilometre" else 1)
    if not math.isfinite(distance) or not 0 < distance <= 10_000_000:
        raise SpatialInputError("DISTANCE_INVALID")
    output_crs = parameters["output_crs"]
    if re.fullmatch(r"EPSG:[1-9][0-9]{0,5}", output_crs) is None:
        raise SpatialInputError("OUTPUT_CRS_INVALID")

    from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsUnitTypes

    os.environ.setdefault("HOME", "/tmp/home")
    os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", "/tmp/qgis-profile")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp/runtime-platform"))
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    Path(os.environ["HOME"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(os.environ["QGIS_CUSTOM_CONFIG_PATH"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        import processing

        crs = QgsCoordinateReferenceSystem(output_crs)
        if (
            not crs.isValid()
            or crs.isGeographic()
            or crs.mapUnits() != QgsUnitTypes.DistanceUnit.DistanceMeters
        ):
            raise SpatialInputError("PROJECTED_METRE_CRS_REQUIRED")
        staged_source = Path("/tmp/source.gpkg")
        shutil.copyfile(source, staged_source)
        layer_source = f"{staged_source}|layername={source_layer['name']}"
        projected = output / "projected.gpkg"
        processing.run(
            "native:reprojectlayer",
            {"INPUT": layer_source, "TARGET_CRS": crs, "OUTPUT": str(projected)},
        )
        vector = output / "buffer.gpkg"
        processing.run(
            "native:buffer",
            {
                "INPUT": str(projected),
                "DISTANCE": distance,
                "SEGMENTS": int(parameters["segments"]),
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2.0,
                "DISSOLVE": False,
                "SEPARATE_DISJOINT": False,
                "OUTPUT": str(vector),
            },
        )
        descriptor = inspect(vector, "geopackage")
    finally:
        app.exitQgis()
    projected.unlink(missing_ok=True)
    produced = descriptor["spatial"]["layers"]
    if len(produced) != 1 or produced[0]["feature_count"] != source_layer["feature_count"]:
        raise SpatialInputError("OUTPUT_FEATURE_COUNT")
    _write_json(output / "manifest.json", descriptor)
    _write_json(
        output / "method.json",
        {
            "schema_version": 1,
            "operator_id": "qgis-buffer",
            "version_id": "1.0.0",
            "algorithms": ["native:reprojectlayer", "native:buffer"],
            "parameters": parameters,
            "source_layer_id": selector["layer_id"],
        },
    )


def run_summary(request: dict, source: Path, output: Path) -> None:
    descriptor = inspect_geopackage(source)
    layer = _layer(descriptor, request["parameters"]["layer_id"])
    _write_json(
        output / "summary.json",
        {
            "schema_version": 1,
            "operator_id": "spatial-vector-summary",
            "source_digest": _digest(source),
            "layer": layer,
        },
    )
    _write_json(
        output / "method.json",
        {
            "schema_version": 1,
            "operator_id": "spatial-vector-summary",
            "version_id": "1.0.0",
            "method": "GDAL/OGR full geometry inspection",
        },
    )


def execute(input_dir: Path, output_dir: Path) -> None:
    request = json.loads((input_dir / "request.json").read_text())
    implementation = request["target"]["execution_binding"]["implementation_id"]
    if implementation == "qgis-buffer-v1":
        run_buffer(request, input_dir / "vector.gpkg", output_dir)
    elif implementation == "spatial-vector-summary-v1":
        run_summary(request, input_dir / "vector.gpkg", output_dir)
    elif implementation == "qgis-static-map-v1":
        run_static_map(request, input_dir / "vector.gpkg", output_dir)
    else:
        raise SpatialInputError("IMPLEMENTATION_UNSUPPORTED")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--format", choices=("geopackage", "geotiff"), required=True)
    inspect_parser.add_argument("--input", type=Path, required=True)
    inspect_parser.add_argument("--output", type=Path, required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--input", type=Path, default=Path("/input"))
    execute_parser.add_argument("--output", type=Path, default=Path("/output"))
    args = parser.parse_args()
    signal.alarm(180)
    implementation = None
    try:
        if args.command == "inspect":
            _write_json(args.output, inspect(args.input, args.format))
        else:
            args.output.mkdir(parents=True, exist_ok=True)
            marker = os.open(args.output / "started.marker", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(marker)
            implementation = json.loads((args.input / "request.json").read_text())["target"][
                "execution_binding"
            ]["implementation_id"]
            execute(args.input, args.output)
            for path in args.output.iterdir():
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            if implementation == "qgis-static-map-v1":
                os._exit(0)
    except (SpatialInputError, KeyError, ValueError, RuntimeError) as exc:
        target = args.output if args.command == "execute" else args.output.parent
        target.mkdir(parents=True, exist_ok=True)
        _write_json(
            target / "error.json",
            {"code": "INPUT_ERROR", "reason": str(exc), "category": type(exc).__name__},
        )
        if implementation == "qgis-static-map-v1":
            os._exit(2)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
