#!/usr/bin/env python3
"""Audit a V7 verification bundle for evidence overclaiming.

This is intentionally read-only.  It does not call the product API, Docker,
PostgreSQL, or a model provider.  It checks the machine-readable evidence
already produced by a run and separates accepted results from raw attempts,
authenticated functional evidence, synthetic browser evidence, and historical
cleanup incidents.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def junit_stats(path: Path) -> dict[str, int | bool]:
    if not path.exists():
        return {"exists": False, "tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {"exists": True, "tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    cases = list(root.iter("testcase"))
    return {
        "exists": True,
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="verification bundle directory")
    parser.add_argument("--output", type=Path, help="output directory (default: report/audit)")
    args = parser.parse_args()
    report = args.report.resolve()
    output = (args.output or report / "audit").resolve()
    output.mkdir(parents=True, exist_ok=True)

    summary = load_json(report / "summary.json", {})
    manifest = load_json(report / "attempt-manifest.json", {})
    public = load_json(report / "openapi-probe.json", {})
    authenticated = load_json(report / "authenticated-interface-evidence.json", {})
    authenticated_inventory = load_json(report / "authenticated-openapi-inventory.json", {})
    if authenticated_inventory.get("evidence_kind") == "authenticated_route_response":
        authenticated = {
            "operations": authenticated_inventory.get("results", []),
            "evidence_kind": authenticated_inventory.get("evidence_kind"),
        }
    internal = load_json(report / "internal-route-probe.json", {})
    internal_inventory = load_json(report / "internal-route-coverage.json", {})
    internal_http = load_json(report / "internal-route-http-evidence.json", {})
    browser = load_json(report / "browser-101-probe.json", {})
    browser_http = load_json(report / "browser-101-http-evidence.json", {})
    # Prefer a fresh probe that exercised a real Platform API over the retained
    # historical Playwright route-interception artifact.  The fixture scope is
    # still reported as a limitation below; it is not production evidence.
    if browser_http.get("status") == "PASS":
        browser = browser_http
    source_maps = load_json(report / "source-map-check.json", {})
    asset_audit = load_json(report / "release-assets-audit.json", {})
    cleanup = load_json(report / "cleanup.json", {})
    release_check = load_json(report / "release-lock-check.json", {})

    findings: list[dict[str, object]] = []
    limitations: list[dict[str, object]] = []
    accepted = junit_stats(report / "all-tests.xml")
    raw = junit_stats(report / "all-attempts.xml")
    raw_failures = int(manifest.get("attempt_failures", raw["failures"]))
    if raw_failures:
        findings.append(
            {
                "id": "RAW_ATTEMPTS_NOT_ZERO",
                "severity": "HIGH",
                "message": (
                    "raw attempts contain failures; selected JUnit cannot be treated "
                    "as a zero-failure run"
                ),
                "evidence": {"attempt_failures": raw_failures, "accepted": accepted},
            }
        )

    public_results = public.get("results", []) if isinstance(public, dict) else []
    functional_success = [
        item
        for item in public_results
        if item.get("evidence_kind") == "success"
        and item.get("path") not in {"/api/v1/health", "/api/v1/ready"}
    ]
    authenticated_operations = authenticated.get("operations", [])
    inventory_has_evidence = bool(public_results) and all(
        item.get("evidence_kind") in {"success", "deterministic_rejection"}
        for item in public_results
    )
    if public_results and not functional_success and not authenticated_operations:
        findings.append(
            {
                "id": "OPENAPI_NO_AUTH_FUNCTIONAL_SUCCESS",
                "severity": "HIGH",
                "message": (
                    "OpenAPI coverage is based on anonymous rejection/health evidence; "
                    "no authenticated business success was observed"
                ),
                "evidence": {"operations": len(public_results), "functional_success": 0},
            }
        )
    elif public_results and not inventory_has_evidence:
        findings.append(
            {
                "id": "OPENAPI_EVIDENCE_GAP",
                "severity": "HIGH",
                "message": (
                    "one or more OpenAPI operations has no success or deterministic "
                    "rejection evidence"
                ),
                "evidence": {
                    "operations": len(public_results),
                    "missing": sum(
                        item.get("evidence_kind") not in {"success", "deterministic_rejection"}
                        for item in public_results
                    ),
                },
            }
        )
    elif public_results and len(authenticated_operations) < len(public_results):
        # The inventory contract accepts a deterministic permission rejection
        # as operation evidence.  Preserve the authenticated-success scope
        # gap as a limitation, but do not call a complete rejection inventory
        # a route failure.
        limitations.append(
            {
                "id": "OPENAPI_AUTH_SCOPE_PARTIAL",
                "severity": "MEDIUM",
                "message": (
                    "authenticated gateway evidence covers only a subset of the OpenAPI inventory"
                ),
                "evidence": {
                    "operations": len(public_results),
                    "authenticated_gateway_operations": len(authenticated_operations),
                },
            }
        )

    http_keys = {
        (item.get("service"), item.get("method"), item.get("path"))
        for item in internal_http.get("operations", [])
    }
    null_routes = [
        item
        for item in internal_inventory.get("routes", [])
        if item.get("observed_status_code") is None
        and (item.get("service"), item.get("method"), item.get("path")) not in http_keys
    ]
    if null_routes:
        findings.append(
            {
                "id": "INTERNAL_ROUTE_EVIDENCE_GAP",
                "severity": "MEDIUM",
                "message": (
                    "internal routes have full-suite evidence only and no observed HTTP status"
                ),
                "evidence": {
                    "count": len(null_routes),
                    "routes": [
                        f"{item.get('service')} {item.get('method')} {item.get('path')}"
                        for item in null_routes
                    ],
                },
            }
        )

    browser_kind = str(browser.get("evidence_kind", "")).lower()
    browser_transport = str(browser.get("transport", "")).lower()
    if (
        browser.get("status") == "SYNTHETIC_PASS"
        or browser_kind.startswith("synthetic")
        or "intercept" in browser_transport
        or (browser.get("status") == "PASS" and not browser.get("transport"))
    ):
        findings.append(
            {
                "id": "SYNTHETIC_BROWSER_PAGINATION",
                "severity": "HIGH",
                "message": (
                    "101-item browser check uses synthetic or intercepted API responses; "
                    "it cannot prove the real gateway/service pagination path"
                ),
                "evidence": {"browser_probe": browser},
            }
        )
    elif browser.get("evidence_kind") == "real_platform_api_with_http_service_fixtures":
        limitations.append(
            {
                "id": "BROWSER_SERVICE_FIXTURE_SCOPE",
                "severity": "MEDIUM",
                "message": (
                    "The 101-item browser flow used a real Platform API and TCP HTTP service "
                    "fixtures, but did not run the production Gateway and Data/Knowledge "
                    "containers in the same evidence bundle."
                ),
                "evidence": {"browser_probe": browser},
            }
        )

    authenticated_statuses = [
        item.get("status_code") for item in authenticated_operations if isinstance(item, dict)
    ]
    dependency_unavailable = sum(status in {502, 503} for status in authenticated_statuses)
    if dependency_unavailable:
        limitations.append(
            {
                "id": "OPENAPI_DEPENDENCY_UNAVAILABLE",
                "severity": "MEDIUM",
                "message": (
                    "authenticated OpenAPI inventory reached routes that returned dependency "
                    "unavailable; it does not prove the configured Data/Knowledge success path"
                ),
                "evidence": {
                    "operations": len(authenticated_operations),
                    "dependency_unavailable": dependency_unavailable,
                    "status_codes": {
                        str(status): authenticated_statuses.count(status)
                        for status in sorted(set(authenticated_statuses), key=str)
                    },
                },
            }
        )

    current_pointer = report.parents[2] / "docs" / "verification" / "p82-current-release.json"
    pointer = load_json(current_pointer, {})
    locked_assets = source_maps.get("locked_asset_count")
    if locked_assets is None and isinstance(pointer, dict):
        release_directory = pointer.get("release_directory")
        lock_path = (
            report.parents[2] / release_directory / "release-lock.json"
            if release_directory
            else None
        )
        lock = load_json(lock_path, {}) if lock_path else {}
        locked_assets = len(lock.get("web_assets", {})) if isinstance(lock, dict) else None
    checked_assets = len(source_maps.get("source_map_checks", []))
    if locked_assets is not None and checked_assets != locked_assets:
        findings.append(
            {
                "id": "SOURCE_MAP_COVERAGE_GAP",
                "severity": "LOW",
                "message": "source-map evidence checks fewer assets than the release lock",
                "evidence": {"checked": checked_assets, "locked": locked_assets},
            }
        )
    asset_map_count = asset_audit.get("source_maps", {}).get("checked")
    if asset_map_count is not None and asset_map_count != locked_assets:
        findings.append(
            {
                "id": "SOURCE_MAP_AUDIT_SCOPE",
                "severity": "LOW",
                "message": "release asset audit does not cover every locked JavaScript asset",
                "evidence": {"checked": asset_map_count, "locked": locked_assets},
            }
        )
    if asset_audit and not asset_audit.get("root", {}).get("csp_present", False):
        findings.append(
            {
                "id": "CSP_EVIDENCE_MISSING",
                "severity": "MEDIUM",
                "message": "root response has no machine-readable Content-Security-Policy evidence",
                "evidence": {"root": asset_audit.get("root", {})},
            }
        )

    removed = cleanup.get("explicit_stage_residue_removed", {})
    removed_total = sum(value for value in removed.values() if isinstance(value, int))
    baseline_removed = cleanup.get("baseline_pre_existing_removed", [])
    if removed_total or baseline_removed:
        findings.append(
            {
                "id": "CLEANUP_BASELINE_SCOPE",
                "severity": "MEDIUM",
                "message": (
                    "cleanup evidence records residue or baseline removals; verify run-id "
                    "ownership "
                    "before claiming clean isolation"
                ),
                "evidence": {
                    "explicit_removed": removed,
                    "baseline_pre_existing_removed": baseline_removed,
                },
            }
        )

    current_digest = None
    if isinstance(pointer, dict):
        current_digest = pointer.get("release_digest")
    if (
        release_check.get("lock_digest")
        and current_digest
        and release_check.get("lock_digest") != current_digest
    ):
        findings.append(
            {
                "id": "RELEASE_IDENTITY_DRIFT",
                "severity": "HIGH",
                "message": "report release-lock evidence differs from the current release pointer",
                "evidence": {"lock": release_check.get("lock_digest"), "current": current_digest},
            }
        )

    result = {
        "schema_version": 1,
        "report": str(report),
        "summary_status": summary.get("status"),
        "accepted_junit": accepted,
        "raw_attempts_junit": raw,
        "raw_attempt_failures": raw_failures,
        "openapi": {
            "operations": len(public_results),
            "functional_success": len(functional_success),
            "authenticated_gateway_operations": len(authenticated_operations),
            "deterministic_rejections": sum(
                item.get("evidence_kind") == "deterministic_rejection" for item in public_results
            ),
        },
        "internal": {
            "operations": internal.get("route_operation_count"),
            "full_suite_only": len(null_routes),
            "supplemental_http_evidence": len(internal_http.get("operations", [])),
            "probe_failures": internal.get("failure_count"),
        },
        "browser_101": {
            "status": browser.get("status"),
            "evidence_kind": browser.get("evidence_kind", "unspecified"),
            "transport": browser.get("transport"),
        },
        "source_maps": {
            "checked": checked_assets,
            "locked": locked_assets,
            "asset_audit_checked": asset_map_count,
            "csp_present": asset_audit.get("root", {}).get("csp_present") if asset_audit else None,
        },
        "release": {"current_pointer": current_digest, "lock": release_check.get("lock_digest")},
        "findings": findings,
        "limitations": limitations,
        "status": "FAIL_WITH_FINDINGS" if findings else "PASS_EVIDENCE_ADEQUATE",
    }
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Verification evidence audit",
        "",
        f"Status: **{result['status']}**",
        "",
        (
            f"Accepted JUnit: {accepted['tests']} tests, failures={accepted['failures']}, "
            f"errors={accepted['errors']}, skipped={accepted['skipped']}."
        ),
        (
            f"Raw attempts: {raw['tests']} cases, failures={raw_failures}; see "
            "`all-attempts.xml` and `attempt-manifest.json`."
        ),
        f"OpenAPI functional successes: {len(functional_success)} / {len(public_results)} "
        "(health/ready excluded).",
        f"Authenticated gateway evidence: {len(authenticated_operations)} operations "
        "(subset only).",
        f"Recorded limitations: {len(limitations)}.",
        f"Internal routes without observed status: {len(null_routes)}.",
        (
            f"Source maps checked: {checked_assets}; locked asset count: "
            f"{locked_assets if locked_assets is not None else 'not recorded'}."
        ),
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            lines.extend([f"- **{finding['id']} [{finding['severity']}]** {finding['message']}"])
    else:
        lines.append("- None")
    lines.extend(["", "## Limitations", ""])
    if limitations:
        for limitation in limitations:
            lines.extend(
                [f"- **{limitation['id']} [{limitation['severity']}]** {limitation['message']}"]
            )
    else:
        lines.append("- None")
    (output / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"status": result["status"], "findings": len(findings), "output": str(output)},
            ensure_ascii=False,
        )
    )
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
