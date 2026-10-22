"""Execution-side validation of the deployment-generated checker catalogue."""

import hashlib
import json
from pathlib import Path

IMPLEMENTATIONS = {
    "platform-runtime-integrity-v1": "CHECKER",
    "p3-timeseries-reproduction-v1": "CHECKER",
    "spatial-vector-validity-v1": "GDAL_CHECKER",
    "cartographic-product-integrity-v1": "CHECKER",
    "p3-descriptive-claim-support-v1": "CHECKER",
    "tabular-profile-reproduction-v1": "CHECKER",
    "tabular-validation-reproduction-v1": "CHECKER",
    "tabular-project-reproduction-v1": "CHECKER",
    "tabular-filter-reproduction-v1": "CHECKER",
    "tabular-cast-reproduction-v1": "CHECKER",
    "tabular-aggregate-reproduction-v1": "CHECKER",
}


def _digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load(path, checker_image, gdal_image):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Checker catalogue cannot be read") from exc
    images = {"CHECKER": checker_image, "GDAL_CHECKER": gdal_image}
    if value.get("images") != images:
        raise ValueError("Checker catalogue image lock differs from Execution runtime")
    catalog = value.get("catalog", {})
    unsigned = {key: item for key, item in catalog.items() if key != "catalog_digest"}
    if catalog.get("catalog_digest") != _digest(unsigned):
        raise ValueError("Checker catalogue digest differs")
    capabilities = catalog.get("capabilities", [])
    found = {item.get("implementation_id"): item for item in capabilities}
    if set(found) != set(IMPLEMENTATIONS):
        raise ValueError("Checker catalogue implementation set differs")
    for identity, role in IMPLEMENTATIONS.items():
        item = found[identity]
        unsigned_item = {key: field for key, field in item.items() if key != "capability_digest"}
        if (
            item.get("capability_id") != identity
            or item.get("image_role") != role
            or item.get("image_digest") != images[role]
            or item.get("capability_digest") != _digest(unsigned_item)
        ):
            raise ValueError("Checker capability lock differs")
    return catalog
