"""P5-R2 trusted capability implementations; no caller supplied code or tolerances."""

import binascii
import hashlib
import json
import math
import struct
import zlib

from platform_checkers.check import ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE, same
from platform_checkers.reference import references
from platform_checkers.spatial import spatial_validity


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


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


def runtime_integrity(request, _root, _index):
    plan = request["coverage_plan"]
    artifacts = request.get("artifacts", [])
    role_bindings = {
        value["role_id"]: value["output_ref"] for value in request.get("role_bindings", [])
    }
    results = []
    for identity in request["checker_run"]["item_ids"]:
        item = _item(plan, identity)
        if item["check_type"] == "METHOD_PRECONDITION":
            decisions = plan["facts"].get("policy_decisions", [])
            if any(value.get("result") in {"MISSING", "CONFLICT"} for value in decisions):
                results.append(_result(item, "FAIL", "A frozen policy precondition failed."))
            else:
                results.append(
                    _result(
                        item,
                        "PASS" if decisions else "NOT_APPLICABLE_WITH_REASON",
                        "Applicable policy facts are satisfied."
                        if decisions
                        else "No policy applies to this delivery.",
                    )
                )
            continue
        role = item.get("role_id")
        bindings = role_bindings if role is None else {role: role_bindings.get(role)}
        valid = bool(artifacts) and all(value is not None for value in bindings.values())
        for output_ref in bindings.values():
            if output_ref is None:
                continue
            artifact = next(
                (value for value in artifacts if value.get("ref") == output_ref.get("asset_ref")),
                None,
            )
            manifest = artifact.get("manifest") if artifact else None
            representation = next(
                (
                    value
                    for value in (manifest or {}).get("representations", [])
                    if value.get("representation_id") == output_ref.get("representation_id")
                ),
                None,
            )
            valid = valid and bool(
                manifest
                and representation
                and representation.get("digest") == output_ref.get("representation_digest")
                and representation.get("schema_id") == output_ref.get("schema_id")
                and output_ref.get("schema_source") == "MANIFEST"
            )
        results.append(
            _result(
                item,
                "PASS" if valid else "FAIL",
                "Published output identities, schemas and manifests agree."
                if valid
                else "A published output identity, schema or manifest differs.",
            )
        )
    return results


def p3_reproduction(request, root, index):
    import pyarrow.parquet as pq

    plan = request["coverage_plan"]
    checked = {}
    for role in request["checker_run"]["input_roles"]:
        entry = index["roles"][role]
        manifest = entry["manifest"]
        source = manifest["source"]
        files = entry["files"]
        observations = pq.read_table(root / entry["source"]["path"]).to_pylist()
        reference_request = {
            "binding": {
                "selection": source["input"],
                "semantics": source["semantics"],
            },
            "parameters": source["parameters"],
        }
        operator = source["target"]["operator_id"]
        expected = references(observations, reference_request)[operator]
        result = json.loads(_bytes(root, files["result"]))
        table = pq.read_table(root / files["table"]["path"]).to_pylist()
        method = json.loads(_bytes(root, files["method"]))
        checked[role] = bool(
            manifest.get("kind") == "OPERATOR_RESULT"
            and result.get("schema") == "p3-result-v1"
            and result.get("operator_id") == operator
            and result.get("version_id") == "1.0.0"
            and result.get("parameters") == source["parameters"]
            and result.get("semantics") == source["semantics"]
            and method == {key: value for key, value in result.items() if key != "rows"}
            and same(table, result.get("rows"))
            and same(result.get("rows"), expected)
        )
    return [
        _result(
            item := _item(plan, identity),
            "PASS" if checked.get(item.get("role_id"), False) else "FAIL",
            "Independent Decimal reproduction and representations agree."
            if checked.get(item.get("role_id"), False)
            else "Independent Decimal reproduction or a representation differs.",
        )
        for identity in request["checker_run"]["item_ids"]
    ]


PALETTES = {
    "blue": ((76, 120, 168), (36, 74, 107)),
    "green": ((89, 161, 79), (47, 102, 48)),
    "orange": ((242, 142, 43), (164, 75, 0)),
}


def _paeth(a, b, c):
    value = a + b - c
    da, db, dc = abs(value - a), abs(value - b), abs(value - c)
    return a if da <= db and da <= dc else b if db <= dc else c


def _scanlines(raw, width, height, channels):
    stride = width * channels
    if len(raw) != height * (stride + 1):
        return None
    rows, previous, offset = [], bytearray(stride), 0
    for _ in range(height):
        kind, offset = raw[offset], offset + 1
        if kind > 4:
            return None
        encoded = raw[offset : offset + stride]
        offset += stride
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = (
                0
                if kind == 0
                else left
                if kind == 1
                else above
                if kind == 2
                else (left + above) // 2
                if kind == 3
                else _paeth(left, above, upper_left)
            )
            row[index] = (value + predictor) & 255
        rows.append(bytes(row))
        previous = row
    return rows


def _png(content, palette, legend):
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    position, width, height, color, data = 8, None, None, None, bytearray()
    while position + 12 <= len(content):
        length = struct.unpack(">I", content[position : position + 4])[0]
        kind = content[position + 4 : position + 8]
        payload = content[position + 8 : position + 8 + length]
        crc = struct.unpack(">I", content[position + 8 + length : position + 12 + length])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != crc:
            return None
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", payload[:10])
            if depth != 8 or color not in {2, 6}:
                return None
        elif kind == b"IDAT":
            data.extend(payload)
        elif kind == b"IEND":
            break
        position += 12 + length
    if width is None or height is None or color is None:
        return None
    try:
        decoder = zlib.decompressobj()
        expected = height * (1 + width * (3 if color == 2 else 4))
        decoded = decoder.decompress(bytes(data), expected + 1)
    except zlib.error:
        return None
    channels = 3 if color == 2 else 4
    if (
        width != 1754
        or height != 1240
        or len(decoded) != expected
        or not decoder.eof
        or decoder.unconsumed_tail
    ):
        return None
    rows = _scanlines(decoded, width, height, channels)
    colors = PALETTES.get(palette)
    if rows is None or colors is None:
        return None
    x0, x1 = 71, (1417 if legend else 1683)
    y0, y1 = 165, 1104
    colored, nonwhite = 0, 0
    for row in rows[y0:y1]:
        for index in range(x0 * channels, x1 * channels, channels):
            pixel = tuple(row[index : index + 3])
            nonwhite += pixel != (255, 255, 255)
            colored += any(
                max(abs(pixel[channel] - target[channel]) for channel in range(3)) <= 16
                for target in colors
            )
    if nonwhite <= 100 or colored == 0:
        return None
    return {
        "width": width,
        "height": height,
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


def cartographic_integrity(request, root, index):
    plan = request["coverage_plan"]
    checked = {}
    for role in request["checker_run"]["input_roles"]:
        entry = index["roles"][role]
        image = entry["files"].get("image")
        manifest_file = entry["files"].get("manifest")
        method_file = entry["files"].get("method")
        valid = all((image, manifest_file, method_file))
        if valid:
            content = _bytes(root, image)
            manifest = json.loads(_bytes(root, manifest_file))
            method = json.loads(_bytes(root, method_file))
            cartography = manifest.get("cartography", {})
            facts = _png(content, cartography.get("palette"), cartography.get("legend"))
            published = entry.get("manifest", {})
            source = entry.get("source_spatial", {})
            source_selection = source.get("selection", {})
            source_manifest = source.get("manifest", {})
            valid = bool(
                facts
                and manifest.get("kind") == "CARTOGRAPHIC_PRODUCT"
                and manifest.get("image", {}).get("digest") == facts["digest"]
                and manifest.get("image", {}).get("width") == facts["width"]
                and manifest.get("image", {}).get("height") == facts["height"]
                and method.get("template_digest")
                == manifest.get("template", {}).get("template_digest")
                and method.get("source_digest") == manifest.get("source", {}).get("digest")
                and method.get("source_digest") == source_selection.get("representation_digest")
                and source_selection.get("asset") is not None
                and source_manifest.get("spatial", {}).get("layers")
                and published.get("cartography") == manifest.get("cartography")
                and published.get("template") == manifest.get("template")
                and published.get("image") == manifest.get("image")
            )
        checked[role] = valid
    return [
        _result(
            item := _item(plan, identity),
            "PASS" if checked.get(item.get("role_id"), False) else "FAIL",
            "PNG, template, source and publication facts agree."
            if checked.get(item.get("role_id"), False)
            else "PNG, template or source facts differ.",
        )
        for identity in request["checker_run"]["item_ids"]
    ]


def claim_support(request, root, index):
    plan = request["coverage_plan"]
    claims = {value["claim_revision_id"]: value for value in plan["facts"].get("claims", [])}
    provenance = plan["facts"].get("observed_provenance", [])
    results = []
    for identity in request["checker_run"]["item_ids"]:
        item = _item(plan, identity)
        candidates = [
            claims[value] for value in item.get("claim_revision_ids", []) if value in claims
        ]
        valid = len(candidates) == 1
        reason = "The structured descriptive assertion is supported by the frozen result."
        if valid:
            claim = candidates[0]
            assertion = claim.get("result_assertion", {})
            role = assertion.get("role_id")
            entry = index["roles"].get(role)
            valid = bool(
                claim.get("proposition_type") == "DESCRIPTIVE"
                and claim.get("support_status") == "SUPPORTED"
                and claim.get("quantity")
                and entry
            )
        if valid:
            result = json.loads(_bytes(root, entry["files"]["result"]))
            rows = [row for row in result.get("rows", []) if row.get("group") == assertion["group"]]
            metric = assertion["metric"]
            actual = rows[0].get(metric) if len(rows) == 1 else None
            source_unit = result.get("semantics", {}).get("value_unit")
            count_metrics = {
                "expected_periods",
                "observed_periods",
                "missing_periods",
                "duplicate_periods",
                "null_count",
                "sample_count",
            }
            unit = (
                "count"
                if metric in count_metrics
                else "1"
                if metric in {"completeness", "r_squared"}
                else source_unit + "/day"
                if metric == "slope_per_day" and source_unit
                else source_unit + "/year"
                if metric == "slope_per_year" and source_unit
                else source_unit
            )
            quantity = claim["quantity"]
            numeric = type(actual) in {int, float} and math.isfinite(actual)
            valid = bool(
                numeric
                and unit == quantity["unit"]
                and math.isclose(
                    actual,
                    quantity["value"],
                    rel_tol=RELATIVE_TOLERANCE,
                    abs_tol=ABSOLUTE_TOLERANCE,
                )
            )
            output_ref = entry["output_ref"]
            supported_execution = output_ref.get(
                "parent_execution_id", output_ref.get("execution_id")
            )
            valid = valid and any(
                value.get("relation") == "SUPPORTED_BY"
                and value.get("subject", {}).get("object_id") == claim["claim_revision_id"]
                and value.get("object", {}).get("kind") == "EXECUTION"
                and value.get("object", {}).get("execution_id") == supported_execution
                for value in provenance
            )
        if not valid:
            reason = "Group, metric, unit, value or observed supporting execution differs."
        results.append(_result(item, "PASS" if valid else "FAIL", reason))
    return results


def tabular_reproduction(request, root, index, operator):
    # PyArrow belongs only to the tabular capabilities. Keep the import lazy so
    # the separate GDAL checker image can execute spatial capabilities without
    # carrying the tabular runtime dependency.
    from platform_checkers.tabular import reproduce_role

    plan = request["coverage_plan"]
    checked = {}
    for role in request["checker_run"]["input_roles"]:
        entry = index.get("roles", {}).get(role)
        try:
            checked[role] = bool(entry) and reproduce_role(root, entry, operator)
        except (KeyError, TypeError, ValueError, OSError):
            checked[role] = False
    return [
        _result(
            item := _item(plan, identity),
            "PASS" if checked.get(item.get("role_id"), False) else "FAIL",
            "Independent tabular recomputation and all representations agree."
            if checked.get(item.get("role_id"), False)
            else "Tabular source, recomputation, schema, method or provenance differs.",
        )
        for identity in request["checker_run"]["item_ids"]
    ]


def tabular_profile_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-profile")


def tabular_validation_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-validate")


def tabular_project_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-project")


def tabular_filter_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-filter")


def tabular_cast_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-cast")


def tabular_aggregate_reproduction(request, root, index):
    return tabular_reproduction(request, root, index, "table-aggregate")


CAPABILITIES = {
    "platform-runtime-integrity-v1": runtime_integrity,
    "p3-timeseries-reproduction-v1": p3_reproduction,
    "spatial-vector-validity-v1": spatial_validity,
    "cartographic-product-integrity-v1": cartographic_integrity,
    "p3-descriptive-claim-support-v1": claim_support,
    "tabular-profile-reproduction-v1": tabular_profile_reproduction,
    "tabular-validation-reproduction-v1": tabular_validation_reproduction,
    "tabular-project-reproduction-v1": tabular_project_reproduction,
    "tabular-filter-reproduction-v1": tabular_filter_reproduction,
    "tabular-cast-reproduction-v1": tabular_cast_reproduction,
    "tabular-aggregate-reproduction-v1": tabular_aggregate_reproduction,
}


def check(request, root):
    plan, run = request["coverage_plan"], request["checker_run"]
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    if plan.get("plan_digest") != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise ValueError("Coverage plan digest differs")
    frozen = next(
        (value for value in plan.get("checker_runs", []) if value["run_key"] == run["run_key"]),
        None,
    )
    if frozen is None or any(run.get(key) != value for key, value in frozen.items()):
        raise ValueError("Checker run differs from coverage plan")
    index_path = root / "input-index.json"
    index = json.loads(index_path.read_text()) if index_path.is_file() else {"roles": {}}
    implementation = CAPABILITIES.get(run["capability_id"])
    if implementation is None:
        raise ValueError("Unknown checker capability")
    items = implementation(request, root, index)
    status = "PASS"
    if any(value["status"] == "ERROR" for value in items):
        status = "ERROR"
    elif any(value["status"] == "FAIL" for value in items):
        status = "FAIL"
    elif any(value["status"] == "INDETERMINATE" for value in items):
        status = "INDETERMINATE"
    return {
        "schema_version": 2,
        "profile": "open-science-v1",
        "status": status,
        "plan_digest": plan["plan_digest"],
        "run_id": run["run_id"],
        "capability_id": run["capability_id"],
        "capability_version": run["capability_version"],
        "capability_digest": run["capability_digest"],
        "input_contract_digest": run["input_contract_digest"],
        "output_contract_digest": run["output_contract_digest"],
        "absolute_tolerance": ABSOLUTE_TOLERANCE,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "items": items,
    }
