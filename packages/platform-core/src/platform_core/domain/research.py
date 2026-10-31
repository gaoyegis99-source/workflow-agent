"""Pure P5.2 policy evaluation over already acquired structured facts."""

from platform_core.domain.canonical import canonical_json, digest_uri

POLICY_ID = "groundwater-head-comparison-v1"
POLICY_VERSION = "1.0.0"
POLICY_SPEC = {
    "policy_id": POLICY_ID,
    "policy_version": POLICY_VERSION,
    "quantity": ["water_level_elevation", "depth_to_water_with_conversion"],
    "requirements": [
        "monitoring_point_identity",
        "convertible_length_unit",
        "compatible_vertical_datum",
        "compatible_temporal_scope",
        "compatible_aggregation",
    ],
}
LENGTH_UNITS = {
    "m": "metre",
    "meter": "metre",
    "metre": "metre",
    "cm": "centimetre",
    "centimeter": "centimetre",
    "centimetre": "centimetre",
    "mm": "millimetre",
    "millimeter": "millimetre",
    "millimetre": "millimetre",
    "ft": "foot",
    "foot": "foot",
    "feet": "foot",
}


def canonical(value):
    return canonical_json(value)


def digest(value):
    return digest_uri(value)


POLICY_DIGEST = digest(POLICY_SPEC)


def reason(code, message, role=None):
    value = {"code": code, "message": message, "input_role": role}
    return value


def evaluate_groundwater_comparison(comparison):
    if comparison is None:
        facts = []
        result = "NOT_APPLICABLE"
        reasons = [reason("NO_HEAD_COMPARISON", "No groundwater-head comparison was declared")]
    else:
        facts = comparison["series"]
        missing = []
        conflict = []
        for item in facts:
            role = item["input_role"]
            if not item.get("monitoring_point_id"):
                missing.append(
                    reason("MONITORING_POINT_MISSING", "Monitoring point is missing", role)
                )
            quantity = item.get("quantity")
            if not quantity:
                missing.append(reason("QUANTITY_MISSING", "Compared quantity is missing", role))
            elif quantity == "depth_to_water" and not item.get("elevation_conversion"):
                missing.append(
                    reason(
                        "ELEVATION_CONVERSION_MISSING",
                        "Depth-to-water requires a sourced elevation conversion",
                        role,
                    )
                )
            unit = item.get("unit")
            if not unit:
                missing.append(reason("UNIT_MISSING", "Measurement unit is missing", role))
            elif unit.casefold() not in LENGTH_UNITS:
                conflict.append(
                    reason("UNIT_NOT_CONVERTIBLE", "Measurement unit is not convertible", role)
                )
            if not item.get("vertical_datum"):
                missing.append(reason("VERTICAL_DATUM_MISSING", "Vertical datum is missing", role))
            if not item.get("temporal_scope"):
                missing.append(reason("TIME_SCOPE_MISSING", "Temporal scope is missing", role))
            if not item.get("aggregation"):
                missing.append(reason("AGGREGATION_MISSING", "Aggregation is missing", role))

        datums = {item.get("vertical_datum") for item in facts if item.get("vertical_datum")}
        if len(datums) > 1:
            for item in facts:
                if item.get("vertical_datum") and not item.get("datum_conversion"):
                    conflict.append(
                        reason(
                            "VERTICAL_DATUM_CONFLICT",
                            "Vertical datums differ without a sourced conversion",
                            item["input_role"],
                        )
                    )
        scopes = {
            (item["temporal_scope"]["start"], item["temporal_scope"]["end"])
            for item in facts
            if item.get("temporal_scope")
        }
        if len(scopes) > 1:
            conflict.append(
                reason("TIME_SCOPE_CONFLICT", "Compared temporal scopes are not identical")
            )
        aggregations = {item.get("aggregation") for item in facts if item.get("aggregation")}
        if len(aggregations) > 1:
            conflict.append(
                reason("AGGREGATION_CONFLICT", "Compared aggregation rules are incompatible")
            )
        if conflict:
            result, reasons = "CONFLICT", [*conflict, *missing]
        elif missing:
            result, reasons = "MISSING", missing
        else:
            result, reasons = "APPLICABLE", []
    return {
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "policy_digest": POLICY_DIGEST,
        "input_digest": digest(comparison),
        "facts_used": facts,
        "result": result,
        "reasons": reasons,
    }
