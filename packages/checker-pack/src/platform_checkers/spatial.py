"""Trusted GDAL adapter used only by the locked spatial-checker image."""

import hashlib


def _bytes(root, entry):
    path = (root / entry["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError("Checker input path is invalid")
    content = path.read_bytes()
    if "sha256:" + hashlib.sha256(content).hexdigest() != entry["digest"]:
        raise ValueError("Checker input digest differs")
    return content


def _item(plan, identity):
    return next(value for value in plan["items"] if value["item_id"] == identity)


def _result(item, status, reason):
    return {
        "item_id": item["item_id"],
        "check_type": item["check_type"],
        "status": status,
        "reason": reason,
    }


def spatial_validity(request, root, index):
    """Independently compare a GeoPackage with its frozen spatial manifest."""

    from osgeo import ogr, osr

    ogr.UseExceptions()
    plan = request["coverage_plan"]
    checked = {}
    for role in request["checker_run"]["input_roles"]:
        entry = index["roles"][role]
        vector_entry = entry["files"].get("vector")
        valid = vector_entry is not None
        if valid:
            path = root / vector_entry["path"]
            _bytes(root, vector_entry)
            dataset = ogr.Open(str(path), 0)
            valid = dataset is not None and dataset.GetLayerCount() > 0
            if valid:
                trusted_layers = entry["manifest"].get("spatial", {}).get("layers", [])
                actual = []
                for ordinal in range(dataset.GetLayerCount()):
                    layer = dataset.GetLayerByIndex(ordinal)
                    extent = layer.GetExtent(force=1)
                    actual.append(
                        {
                            "name": layer.GetName(),
                            "feature_count": layer.GetFeatureCount(force=1),
                            "extent": list(extent) if extent else None,
                            "crs": (
                                layer.GetSpatialRef().ExportToWkt()
                                if layer.GetSpatialRef()
                                else None
                            ),
                        }
                    )
                    layer.ResetReading()
                    for feature in layer:
                        geometry = feature.GetGeometryRef()
                        if geometry is None or not geometry.IsValid():
                            valid = False
                            break
                valid = valid and len(actual) == len(trusted_layers)
                for fact, trusted in zip(actual, trusted_layers, strict=True):
                    bounds = trusted.get("bounds")
                    expected_extent = (
                        [bounds["min_x"], bounds["max_x"], bounds["min_y"], bounds["max_y"]]
                        if bounds is not None
                        else None
                    )
                    trusted_crs = trusted.get("crs", {})
                    expected_srs = osr.SpatialReference()
                    crs_valid = (
                        bool(trusted_crs.get("wkt2"))
                        and expected_srs.ImportFromWkt(trusted_crs.get("wkt2", "")) == 0
                    )
                    actual_srs = osr.SpatialReference()
                    actual_valid = (
                        bool(fact["crs"]) and actual_srs.ImportFromWkt(fact["crs"] or "") == 0
                    )
                    valid = bool(
                        valid
                        and fact["name"] == trusted.get("name")
                        and fact["feature_count"] == trusted.get("feature_count")
                        and fact["extent"] == expected_extent
                        and expected_srs.IsSame(actual_srs)
                        if crs_valid and actual_valid
                        else False
                    )
        checked[role] = valid
    return [
        _result(
            item := _item(plan, identity),
            "PASS" if checked.get(item.get("role_id"), False) else "FAIL",
            "Independent GDAL inspection agrees with the spatial manifest."
            if checked.get(item.get("role_id"), False)
            else "CRS, geometry, count or spatial manifest differs.",
        )
        for identity in request["checker_run"]["item_ids"]
    ]
