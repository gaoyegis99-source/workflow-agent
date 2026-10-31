"""Deterministic P5.3 coverage planning and completion rules.

This module is deliberately pure. It compiles already verified Platform facts;
it performs no network or database I/O and executes no caller supplied rules.
"""

from platform_core.application.control import digest
from platform_core.domain.control import require


def _current_claims(rows):
    latest = {}
    for row in rows:
        current = latest.get(row["claim_id"])
        if current is None or row["revision"] > current["revision"]:
            latest[row["claim_id"]] = row
    return [latest[key] for key in sorted(latest)]


def _evidence_refs(provenance):
    found = {}
    for relation in provenance:
        source = relation.get("object", {})
        if source.get("kind") != "EVIDENCE":
            continue
        ref = source.get("ref", {})
        if not all(
            ref.get(name) for name in ("project_id", "document_id", "version_id", "unit_id")
        ):
            continue
        found[digest(ref)] = ref
    return [found[key] for key in sorted(found)]


_UNSET = object()


def compile_coverage(
    spec,
    candidate,
    registrations,
    claims,
    policies,
    provenance,
    analyses=None,
    selected_registration_id=_UNSET,
    catalog=None,
):
    """Compile the fixed open-science-v1 catalogue into a deterministic plan."""

    require(spec["profile"] == "open-science-v1", "INVALID_BINDING", "Unknown profile")
    require(
        candidate.get("identity_mode") == "EXACT"
        and all("role_id" in item for item in spec.get("required_outputs", [])),
        "PRECONDITION_UNRESOLVED",
        "Legacy output identities are not executable",
    )
    artifacts = candidate["payload"].get("artifacts", [])
    outputs = set(candidate["payload"].get("executions", [])) | {
        item["output"] for item in candidate["payload"].get("workflow_outputs", [])
    }
    for artifact in artifacts:
        manifest = artifact.get("manifest", {})
        operator_id = manifest.get("source", {}).get("target", {}).get("operator_id")
        if operator_id:
            outputs.add(operator_id)
        outputs.update(
            item["representation_id"]
            for item in manifest.get("representations", [])
            if isinstance(item.get("representation_id"), str)
        )
    outputs = sorted(outputs)
    registrations = [
        value
        for value in registrations
        if value["task_spec_revision"] == candidate["spec_revision"]
    ]
    if analyses is not None:
        superseded = {
            value["supersedes_revision_id"]
            for value in analyses
            if value.get("supersedes_revision_id") is not None
        }
        registrations = [value for value in registrations if value["revision_id"] not in superseded]
    if selected_registration_id is _UNSET:
        # Compatibility for historical callers and stored 0.6.x plans. New API
        # acceptance requests always pass an explicit identity or None.
        if spec["delivery_class"] == "VERIFIED":
            require(
                len(registrations) == 1,
                "PRECONDITION_UNRESOLVED",
                "Verified delivery requires one unambiguous registered analysis",
            )
        selected = (
            max(
                registrations,
                key=lambda value: (str(value.get("registered_at", "")), value["registration_id"]),
            )
            if registrations
            else None
        )
    else:
        selected = next(
            (
                value
                for value in registrations
                if value["registration_id"] == selected_registration_id
            ),
            None,
        )
        require(
            selected_registration_id is None or selected is not None,
            "PRECONDITION_UNRESOLVED",
            "Selected analysis registration is not current for this task revision",
        )
        if spec["delivery_class"] == "VERIFIED":
            require(
                selected is not None,
                "PRECONDITION_UNRESOLVED",
                "Verified delivery requires an explicit analysis registration",
            )
    current_claim_rows = _current_claims(claims)
    if selected:
        current_claim_rows = [
            value
            for value in current_claim_rows
            if value["payload"].get("analysis_registration_id") == selected["registration_id"]
        ]
        relevant_subjects = {selected["revision_id"]} | {
            value["claim_revision_id"] for value in current_claim_rows
        }
        provenance = [
            value
            for value in provenance
            if value.get("subject", {}).get("object_id") in relevant_subjects
        ]
        policies = [
            value for value in policies if value["decision_id"] == selected["policy_decision_id"]
        ]
    else:
        # Exploratory delivery may cite facts from an unregistered analysis.
        # Preserve its observed evidence and policy limitations, but it cannot
        # promote any formal claim without a matching registration.
        current_claim_rows = []
    facts = {
        "candidate_digest": candidate["digest"],
        "artifacts": artifacts,
        "available_output_ids": outputs,
        "registration": (
            {
                "registration_id": selected["registration_id"],
                "analysis_id": selected["analysis_id"],
                "revision_id": selected["revision_id"],
                "revision": selected["revision"],
                "task_spec_revision": selected["task_spec_revision"],
                "observation_watermark": selected["observation_watermark"],
                "watermark_digest": selected["watermark_digest"],
                "policy_decision_id": selected["policy_decision_id"],
            }
            if selected
            else None
        ),
        "claims": [
            row["payload"]
            | {
                "claim_id": row["claim_id"],
                "claim_revision_id": row["claim_revision_id"],
                "revision": row["revision"],
            }
            for row in current_claim_rows
        ],
        "policy_decisions": [
            {
                "decision_id": row["decision_id"],
                "policy_id": row["policy_id"],
                "policy_version": row["policy_version"],
                "policy_digest": row["policy_digest"],
                "result": row["result"],
                "reasons": row["reasons"],
            }
            for row in policies
        ],
        "observed_provenance": [
            {
                "relation": row["relation"],
                "subject": row["subject"],
                "object": row["object"],
                "role": row["role"],
                "relation_digest": row["relation_digest"],
            }
            for row in provenance
            if row["provenance_kind"] == "OBSERVED_PROVENANCE"
        ],
    }
    raw_items = [
        {
            "requirement_id": "system-runtime-integrity",
            "check_type": "RUNTIME_INTEGRITY",
            "necessary": True,
            "automatic": True,
            "description": "Published artifacts and frozen provenance are internally consistent.",
        }
    ]
    exact_outputs = [value for value in spec["required_outputs"] if "role_id" in value]
    role_bindings = {
        value["role_id"]: value["output_ref"]
        for value in candidate["payload"].get("role_bindings", [])
    }
    for output in exact_outputs:
        binding = role_bindings.get(output["role_id"])
        raw_items.append(
            {
                "requirement_id": "role-" + output["role_id"],
                "check_type": "RUNTIME_INTEGRITY",
                "necessary": output["necessary"],
                "automatic": True,
                "description": "Exact output role " + output["role_id"],
                "role_id": output["role_id"],
                "output_ref": binding,
            }
        )
    for requirement in spec["requirements"]:
        for check_type in requirement["checks"]:
            raw_items.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "check_type": check_type,
                    "necessary": requirement["necessary"],
                    "automatic": check_type != "HUMAN_REVIEW",
                    "description": requirement["description"],
                }
            )
    if catalog is None:
        items = []
        for ordinal, item in enumerate(raw_items):
            items.append(item | {"item_id": f"coverage-{ordinal:04d}", "ordinal": ordinal})
        checker_runs = []
        schema_version = 2
        checker_catalog = "open-science-checker-v1"
    else:
        items, checker_runs = _compile_capability_items(
            raw_items, exact_outputs, role_bindings, current_claim_rows, catalog
        )
        schema_version = 3
        checker_catalog = catalog["catalog_digest"]
    plan = {
        "schema_version": schema_version,
        "profile": "open-science-v1",
        "revision": 1,
        "delivery_class": spec["delivery_class"],
        "max_check_executions": spec["max_check_executions"],
        "task_spec_revision": candidate["spec_revision"],
        "checker_catalog": checker_catalog,
        "capability_catalog": catalog if catalog is not None else None,
        "requirements": spec["requirements"],
        "required_outputs": spec["required_outputs"],
        "role_bindings": role_bindings,
        "analysis_registration_id": selected["registration_id"] if selected else None,
        "limitations": [],
        "items": items,
        "checker_runs": checker_runs,
        "facts": facts,
    }
    plan["source_digest"] = digest(
        {
            "spec": spec,
            "candidate": candidate["digest"],
            "facts": facts,
        }
    )
    plan["plan_digest"] = digest(plan)
    return plan, _evidence_refs(provenance)


def _compile_capability_items(raw_items, exact_outputs, role_bindings, claims, catalog):
    """Attach each automatic item to one exact trusted capability or UNCOVERED."""

    from platform_core.application.checker_catalog import matches

    expected_by_role = {value["role_id"]: value["expected"] for value in exact_outputs}
    capability_by_id = {value["capability_id"]: value for value in catalog["capabilities"]}
    runtime = capability_by_id["platform-runtime-integrity-v1"]
    p3_reproduction = capability_by_id["p3-timeseries-reproduction-v1"]
    claim_checker = capability_by_id["p3-descriptive-claim-support-v1"]
    expanded = []

    def covered(item, capability, roles=(), claim_ids=()):
        return item | {
            "coverage_status": "COVERED",
            "capability_id": capability["capability_id"],
            "capability_version": capability["version"],
            "capability_digest": capability["capability_digest"],
            "implementation_id": capability["implementation_id"],
            "image_digest": capability["image_digest"],
            "input_contract_digest": capability["input_contract_digest"],
            "output_contract_digest": capability["output_contract_digest"],
            "input_roles": sorted(roles),
            "claim_revision_ids": sorted(claim_ids),
        }

    def uncovered(item, reason):
        return item | {
            "coverage_status": "UNCOVERED",
            "capability_id": None,
            "capability_version": None,
            "capability_digest": None,
            "implementation_id": None,
            "image_digest": None,
            "input_contract_digest": None,
            "output_contract_digest": None,
            "input_roles": [],
            "claim_revision_ids": [],
            "uncovered_reason": reason,
        }

    for item in raw_items:
        check_type = item["check_type"]
        if not item["automatic"]:
            expanded.append(
                item
                | {
                    "coverage_status": "MANUAL",
                    "capability_id": None,
                    "input_roles": [],
                    "claim_revision_ids": [],
                }
            )
            continue
        if check_type in runtime["check_types"]:
            roles = [item["role_id"]] if item.get("role_id") else []
            expanded.append(covered(item, runtime, roles))
            continue
        if check_type in {"DATA_VALIDITY", "RESULT_REPRODUCTION"}:
            matched = False
            for role in sorted(expected_by_role):
                expected = expected_by_role[role]
                capability = next(
                    (
                        value
                        for value in catalog["capabilities"]
                        if value["capability_id"]
                        not in {
                            runtime["capability_id"],
                            claim_checker["capability_id"],
                        }
                        and matches(value, check_type, expected)
                    ),
                    None,
                )
                if capability is not None and role in role_bindings:
                    matched = True
                    expanded.append(covered(item | {"role_id": role}, capability, [role]))
            if not matched:
                expanded.append(uncovered(item, "NO_MATCHING_TRUSTED_CAPABILITY"))
            continue
        if check_type == "CLAIM_SUPPORT":
            matched = False
            for claim in claims:
                payload = claim["payload"]
                assertion = payload.get("result_assertion")
                role = assertion.get("role_id") if isinstance(assertion, dict) else None
                expected = expected_by_role.get(role)
                if (
                    assertion
                    and payload.get("proposition_type") == "DESCRIPTIVE"
                    and payload.get("support_status") == "SUPPORTED"
                    and role in role_bindings
                    and matches(claim_checker, check_type, expected or {})
                ):
                    matched = True
                    expanded.append(
                        covered(
                            item | {"role_id": role},
                            claim_checker,
                            [role],
                            [claim["claim_revision_id"]],
                        )
                    )
            if not matched:
                expanded.append(uncovered(item, "NO_SUPPORTED_DESCRIPTIVE_ASSERTION"))
            continue
        expanded.append(uncovered(item, "NO_MATCHING_TRUSTED_CAPABILITY"))

    items = []
    for ordinal, item in enumerate(expanded):
        initial_status = {
            "COVERED": "PLANNED",
            "UNCOVERED": "INDETERMINATE",
            "MANUAL": "REVIEW_REQUIRED",
        }[item["coverage_status"]]
        items.append(
            item
            | {
                "item_id": f"coverage-{ordinal:04d}",
                "ordinal": ordinal,
                "initial_status": initial_status,
            }
        )

    grouped = {}
    for item in items:
        identity = item.get("capability_id")
        if item.get("coverage_status") != "COVERED" or identity is None:
            continue
        grouped.setdefault(identity, []).append(item)
    order = {
        "platform-runtime-integrity-v1": 0,
        "p3-timeseries-reproduction-v1": 1,
        "spatial-vector-validity-v1": 1,
        "cartographic-product-integrity-v1": 1,
        "tabular-profile-reproduction-v1": 1,
        "tabular-validation-reproduction-v1": 1,
        "tabular-project-reproduction-v1": 1,
        "tabular-filter-reproduction-v1": 1,
        "tabular-cast-reproduction-v1": 1,
        "tabular-aggregate-reproduction-v1": 1,
        "p3-descriptive-claim-support-v1": 2,
    }
    checker_runs = []
    for index, identity in enumerate(sorted(grouped, key=lambda value: (order[value], value))):
        capability = capability_by_id[identity]
        members = grouped[identity]
        dependencies = []
        if identity != runtime["capability_id"]:
            dependencies.append(runtime["capability_id"])
        if identity == claim_checker["capability_id"]:
            # A claim is never checked against a result that failed independent reproduction.
            roles = {role for item in members for role in item["input_roles"]}
            if roles and any(
                role in item["input_roles"]
                for item in grouped.get(p3_reproduction["capability_id"], [])
                for role in roles
            ):
                dependencies.append(p3_reproduction["capability_id"])
        checker_runs.append(
            {
                "run_key": f"check-run-{index:02d}",
                "capability_id": identity,
                "capability_version": capability["version"],
                "capability_digest": capability["capability_digest"],
                "implementation_id": capability["implementation_id"],
                "image_digest": capability["image_digest"],
                "input_contract_digest": capability["input_contract_digest"],
                "output_contract_digest": capability["output_contract_digest"],
                "item_ids": [item["item_id"] for item in members],
                "input_roles": sorted(
                    {role for item in members for role in item.get("input_roles", [])}
                ),
                "claim_revision_ids": sorted(
                    {claim for item in members for claim in item.get("claim_revision_ids", [])}
                ),
                "depends_on": dependencies,
            }
        )
    return items, checker_runs


def outcome(delivery_class, items):
    """Return acceptance state without mutating historical check facts."""

    necessary = [item for item in items if item["necessary"]]
    if not necessary:
        return "FAIL", ["EMPTY_COVERAGE"]
    if any(item["status"] == "ERROR" for item in necessary):
        return "ERROR", [item["item_id"] for item in necessary if item["status"] == "ERROR"]
    if any(item["status"] == "STALE" for item in necessary):
        return "STALE", [item["item_id"] for item in necessary if item["status"] == "STALE"]
    failures = [item for item in necessary if item["status"] == "FAIL"]
    if failures:
        return "FAIL", [item["item_id"] for item in failures]
    unresolved = [
        item
        for item in necessary
        if item["status"] in {"PLANNED", "INDETERMINATE", "REVIEW_REQUIRED"}
    ]
    runtime = [item for item in necessary if item["check_type"] == "RUNTIME_INTEGRITY"]
    if any(item["status"] != "PASS" for item in runtime):
        return "FAIL", ["RUNTIME_INTEGRITY_UNRESOLVED"]
    if delivery_class == "VERIFIED" and unresolved:
        if any(item["status"] == "REVIEW_REQUIRED" for item in unresolved):
            return "REVIEW_REQUIRED", [item["item_id"] for item in unresolved]
        return "INDETERMINATE", [item["item_id"] for item in unresolved]
    return "PASS", [item["item_id"] for item in unresolved]


def validate_checker_report(plan, report):
    """Validate every fixed checker result before it can update authoritative coverage."""

    require(
        report.get("profile") == "open-science-v1"
        and report.get("plan_digest") == plan["plan_digest"],
        "INVALID_BINDING",
        "Checker used another coverage plan",
    )
    expected = {item["item_id"]: item for item in plan["items"] if item["automatic"]}
    results = report.get("items")
    require(isinstance(results, list), "INVALID_BINDING", "Checker results are malformed")
    ids = [value.get("item_id") for value in results if isinstance(value, dict)]
    require(
        len(ids) == len(results) == len(set(ids)) and set(ids) == set(expected),
        "INVALID_BINDING",
        "Checker results do not exactly cover the automatic plan items",
    )
    allowed = {
        "PASS",
        "FAIL",
        "INDETERMINATE",
        "REVIEW_REQUIRED",
        "NOT_APPLICABLE_WITH_REASON",
        "ERROR",
    }
    for result in results:
        item = expected[result["item_id"]]
        require(
            result.get("check_type") == item["check_type"]
            and result.get("status") in allowed
            and isinstance(result.get("reason"), str)
            and bool(result["reason"].strip()),
            "INVALID_BINDING",
            "Checker result differs from the frozen coverage item",
        )
    if any(value["status"] in {"FAIL", "ERROR"} for value in results):
        aggregate = "FAIL"
    elif any(value["status"] == "INDETERMINATE" for value in results):
        aggregate = "INDETERMINATE"
    elif any(value["status"] == "REVIEW_REQUIRED" for value in results):
        aggregate = "REVIEW_REQUIRED"
    else:
        aggregate = "PASS"
    require(
        report.get("status") == aggregate,
        "INVALID_BINDING",
        "Checker aggregate status differs from its item results",
    )


def validate_capability_report(plan, run, report):
    """Reject partial, extra, stale or cross-capability checker output."""

    require(
        plan.get("schema_version") == 3
        and report.get("schema_version") == 2
        and report.get("profile") == "open-science-v1"
        and report.get("plan_digest") == plan["plan_digest"]
        and report.get("run_id") == run["run_id"]
        and report.get("capability_id") == run["capability_id"]
        and report.get("capability_version") == run["capability_version"]
        and report.get("capability_digest") == run["capability_digest"]
        and report.get("input_contract_digest") == run["input_contract_digest"]
        and report.get("output_contract_digest") == run["output_contract_digest"],
        "INVALID_BINDING",
        "Checker report differs from its frozen run",
    )
    expected = {
        item["item_id"]: item for item in plan["items"] if item["item_id"] in run["item_ids"]
    }
    results = report.get("items")
    require(
        isinstance(results, list)
        and len(results) == len(expected) == len(run["item_ids"])
        and len({item.get("item_id") for item in results if isinstance(item, dict)}) == len(results)
        and {item.get("item_id") for item in results} == set(expected),
        "INVALID_BINDING",
        "Checker report does not exactly cover its frozen item set",
    )
    allowed = {
        "PASS",
        "FAIL",
        "INDETERMINATE",
        "NOT_APPLICABLE_WITH_REASON",
        "ERROR",
    }
    for result in results:
        item = expected[result["item_id"]]
        require(
            item.get("coverage_status") == "COVERED"
            and item.get("capability_id") == run["capability_id"]
            and result.get("check_type") == item["check_type"]
            and result.get("status") in allowed
            and isinstance(result.get("reason"), str)
            and bool(result["reason"].strip()),
            "INVALID_BINDING",
            "Checker result differs from the frozen capability item",
        )
    aggregate = "PASS"
    if any(item["status"] == "ERROR" for item in results):
        aggregate = "ERROR"
    elif any(item["status"] == "FAIL" for item in results):
        aggregate = "FAIL"
    elif any(item["status"] == "INDETERMINATE" for item in results):
        aggregate = "INDETERMINATE"
    require(
        report.get("status") == aggregate,
        "INVALID_BINDING",
        "Checker run aggregate differs from its item results",
    )
