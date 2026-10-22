"""Fixed P5.3 checker for frozen coverage facts; no caller supplied expressions."""

import hashlib
import json


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _status(item, facts):
    kind = item["check_type"]
    if kind == "HUMAN_REVIEW":
        return "REVIEW_REQUIRED", "A qualified project owner must review this clause."
    if kind == "RUNTIME_INTEGRITY":
        output_id = item.get("output_id")
        if item.get("role_id") and item.get("output_ref") is None:
            return "FAIL", "A required exact output role is unbound."
        if not facts.get("artifacts"):
            return "FAIL", "No atomically published artifact is present."
        if output_id and output_id not in facts.get("available_output_ids", []):
            return "FAIL", "A required output is absent."
        manifests = [artifact.get("manifest") for artifact in facts["artifacts"]]
        if any(
            not isinstance(value, dict) or not value.get("representations") for value in manifests
        ):
            return "FAIL", "An artifact lacks its immutable representation manifest."
        return "PASS", "Published artifact identities and manifests are present."
    if kind == "DATA_VALIDITY":
        return (
            "INDETERMINATE",
            "Technical manifest integrity is covered separately; no registered domain "
            "validity checker covers this data.",
        )
    if kind == "METHOD_PRECONDITION":
        decisions = facts.get("policy_decisions", [])
        bad = [value for value in decisions if value.get("result") in {"MISSING", "CONFLICT"}]
        if bad:
            return "FAIL", "A frozen policy decision reports missing or conflicting preconditions."
        if not decisions:
            return "NOT_APPLICABLE_WITH_REASON", "No policy was applicable to this delivery."
        return "PASS", "All applicable frozen policy decisions are satisfied."
    if kind == "RESULT_REPRODUCTION":
        return "INDETERMINATE", "No registered independent reproducer covers this result type."
    if kind == "CLAIM_SUPPORT":
        claims = facts.get("claims", [])
        if not claims:
            return "INDETERMINATE", "No current claim was supplied."
        if any(value.get("support_status") != "SUPPORTED" for value in claims):
            return "FAIL", "A current claim is not marked supported."
        supported = {
            value.get("subject", {}).get("object_id")
            for value in facts.get("observed_provenance", [])
            if value.get("relation") == "SUPPORTED_BY"
            and value.get("subject", {}).get("kind") == "CLAIM_REVISION"
        }
        if any(value.get("claim_revision_id") not in supported for value in claims):
            return "FAIL", "Each current claim requires its own observed supporting source."
        if any(value.get("proposition_type") == "CAUSAL" for value in claims):
            return "INDETERMINATE", "The generic checker cannot establish causal identification."
        return (
            "INDETERMINATE",
            "Exact supporting provenance is present, but no registered domain checker "
            "establishes that the result supports the claim.",
        )
    return "ERROR", "Unknown fixed check type."


def check(request):
    if request.get("checker_run") is not None:
        from pathlib import Path

        from platform_checkers.capabilities import check as check_capability

        return check_capability(request, Path("/input"))
    plan = request["coverage_plan"]
    supplied = plan.get("plan_digest")
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    calculated = hashlib.sha256(canonical(unsigned)).hexdigest()
    if supplied != calculated:
        return {
            "schema_version": 1,
            "profile": "open-science-v1",
            "status": "ERROR",
            "category": "PLAN_DIGEST_MISMATCH",
            "plan_digest": supplied,
            "items": [],
        }
    seen, results = set(), []
    for item in plan.get("items", []):
        if item["item_id"] in seen:
            return {
                "schema_version": 1,
                "profile": "open-science-v1",
                "status": "ERROR",
                "category": "DUPLICATE_COVERAGE_ITEM",
                "plan_digest": supplied,
                "items": [],
            }
        seen.add(item["item_id"])
        if not item["automatic"]:
            continue
        status, reason = _status(item, plan["facts"])
        results.append(
            {
                "item_id": item["item_id"],
                "check_type": item["check_type"],
                "status": status,
                "reason": reason,
            }
        )
    if not results:
        overall = "ERROR"
    elif any(value["status"] in {"FAIL", "ERROR"} for value in results):
        overall = "FAIL"
    elif any(value["status"] == "INDETERMINATE" for value in results):
        overall = "INDETERMINATE"
    else:
        overall = "PASS"
    return {
        "schema_version": 1,
        "profile": "open-science-v1",
        "status": overall,
        "plan_digest": supplied,
        "checker_catalog": "open-science-checker-v1",
        "items": results,
    }
