import json
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
from platform_checkers.capabilities import check
from platform_contracts.service import digest
from platform_core.application.checker_catalog import build_catalog
from platform_core.application.open_acceptance import compile_coverage, validate_capability_report
from platform_operators.cli import run

from tests.p3.test_science import request as operator_request
from tests.p3.test_science import rows
from tests.p5r1.test_planning import registration
from tests.p5r2.test_catalog_and_planning import r2_spec


def fixture(tmp_path):
    operator = operator_request()
    result = run(operator, rows())
    method = {key: value for key, value in result.items() if key != "rows"}
    role = tmp_path / "roles/trend_result"
    role.mkdir(parents=True)
    (role / "result.bin").write_text(json.dumps(result))
    pq.write_table(pa.Table.from_pylist(result["rows"]), role / "table.bin")
    (role / "method.bin").write_text(json.dumps(method))
    pq.write_table(pa.Table.from_pylist(rows()), role / "observations.parquet")
    representations = []
    for identity in ("result", "table", "method"):
        path = role / f"{identity}.bin"
        representations.append(
            {
                "representation_id": identity,
                "format": "parquet" if identity == "table" else "json",
                "digest": "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "schema_id": {
                    "result": "p3-result-v1",
                    "table": "p3-table-v1",
                    "method": "p3-method-v1",
                }[identity],
            }
        )
    source = {
        "target": operator["target"],
        "task_ref": {"task_id": "task", "spec_revision": 1},
        "input": operator["binding"]["selection"],
        "parameters": operator["parameters"],
        "semantics": operator["binding"]["semantics"],
        "execution_id": "execution",
    }
    manifest = {
        "kind": "OPERATOR_RESULT",
        "representations": representations,
        "source": source,
    }
    ref = {
        "project_id": "project",
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + digest(manifest),
    }
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": "action",
        "execution_id": "execution",
        "output_port": "result",
        "asset_ref": ref,
        "representation_id": "result",
        "representation_digest": representations[0]["digest"],
        "schema_id": "p3-result-v1",
        "schema_source": "MANIFEST",
    }
    candidate = {
        "digest": "candidate",
        "spec_revision": 1,
        "identity_mode": "EXACT",
        "payload": {
            "identity_mode": "EXACT",
            "artifacts": [{"ref": ref, "manifest": manifest}],
            "executions": [],
            "workflow_outputs": [],
            "role_bindings": [{"role_id": "trend_result", "output_ref": output_ref}],
        },
    }
    plan, _ = compile_coverage(
        r2_spec("DATA_VALIDITY", "RESULT_REPRODUCTION"),
        candidate,
        [registration()],
        [],
        [],
        [],
        [],
        "registration",
        build_catalog(),
    )
    frozen = next(
        value
        for value in plan["checker_runs"]
        if value["capability_id"] == "p3-timeseries-reproduction-v1"
    )
    run_record = frozen | {"run_id": "run"}
    request = {
        "coverage_plan": plan,
        "checker_run": run_record | {"catalog_digest": plan["checker_catalog"]},
        "artifacts": candidate["payload"]["artifacts"],
        "role_bindings": candidate["payload"]["role_bindings"],
    }
    index = {
        "roles": {
            "trend_result": {
                "output_ref": output_ref,
                "manifest": manifest,
                "files": {
                    item["representation_id"]: {
                        "path": f"roles/trend_result/{item['representation_id']}.bin",
                        "digest": item["digest"],
                        "format": item["format"],
                        "schema_id": item["schema_id"],
                    }
                    for item in representations
                },
                "source": {
                    "path": "roles/trend_result/observations.parquet",
                    "selection": source["input"],
                },
            }
        }
    }
    (tmp_path / "input-index.json").write_text(json.dumps(index))
    return request, run_record, result


def test_decimal_reproduction_passes_and_wrong_slope_fails(tmp_path):
    request, run_record, result = fixture(tmp_path)
    report = check(request, tmp_path)
    assert report["status"] == "PASS"
    validate_capability_report(request["coverage_plan"], run_record, report)

    changed = deepcopy(result)
    changed["rows"][0]["slope_per_year"] += 1
    path = tmp_path / "roles/trend_result/result.bin"
    path.write_text(json.dumps(changed))
    index = json.loads((tmp_path / "input-index.json").read_text())
    entry = index["roles"]["trend_result"]["files"]["result"]
    entry["digest"] = "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    (tmp_path / "input-index.json").write_text(json.dumps(index))
    assert check(request, tmp_path)["status"] == "FAIL"


def test_report_rejects_missing_extra_and_old_capability(tmp_path):
    request, run_record, _ = fixture(tmp_path)
    report = check(request, tmp_path)
    forged = deepcopy(report)
    forged["items"].pop()
    try:
        validate_capability_report(request["coverage_plan"], run_record, forged)
    except Exception as error:
        assert getattr(error, "code", None) == "INVALID_BINDING"
    else:
        raise AssertionError("Partial report was accepted")
    forged = deepcopy(report)
    forged["capability_version"] = "0.9.0"
    try:
        validate_capability_report(request["coverage_plan"], run_record, forged)
    except Exception as error:
        assert getattr(error, "code", None) == "INVALID_BINDING"
    else:
        raise AssertionError("Old checker version was accepted")


def test_descriptive_scalar_claim_uses_value_unit_and_observed_execution(tmp_path):
    request, _, result = fixture(tmp_path)
    role_binding = request["role_bindings"][0]
    candidate = {
        "digest": "candidate",
        "spec_revision": 1,
        "identity_mode": "EXACT",
        "payload": {
            "identity_mode": "EXACT",
            "artifacts": request["artifacts"],
            "executions": [],
            "workflow_outputs": [],
            "role_bindings": [role_binding],
        },
    }
    expected = result["rows"][0]["slope_per_year"]
    claim = {
        "claim_id": "claim",
        "claim_revision_id": "claim-revision",
        "revision": 1,
        "payload": {
            "analysis_registration_id": "registration",
            "proposition_type": "DESCRIPTIVE",
            "support_status": "SUPPORTED",
            "quantity": {"value": expected, "unit": "m/year"},
            "result_assertion": {
                "kind": "P3_SCALAR",
                "role_id": "trend_result",
                "group": "A",
                "metric": "slope_per_year",
            },
        },
    }
    provenance = [
        {
            "relation": "SUPPORTED_BY",
            "provenance_kind": "OBSERVED_PROVENANCE",
            "subject": {"kind": "CLAIM_REVISION", "object_id": "claim-revision"},
            "object": {"kind": "EXECUTION", "execution_id": "execution"},
            "role": "support",
            "relation_digest": "sha256:" + "a" * 64,
        }
    ]
    plan, _ = compile_coverage(
        r2_spec("CLAIM_SUPPORT"),
        candidate,
        [registration()],
        [claim],
        [],
        provenance,
        [],
        "registration",
        build_catalog(),
    )
    frozen = next(
        value
        for value in plan["checker_runs"]
        if value["capability_id"] == "p3-descriptive-claim-support-v1"
    )
    run_record = frozen | {"run_id": "claim-run"}
    claim_request = request | {
        "coverage_plan": plan,
        "checker_run": run_record | {"catalog_digest": plan["checker_catalog"]},
    }
    report = check(claim_request, tmp_path)
    assert report["status"] == "PASS"
    validate_capability_report(plan, run_record, report)

    changed = deepcopy(plan)
    changed["facts"]["claims"][0]["quantity"]["value"] += 10
    unsigned = {key: value for key, value in changed.items() if key != "plan_digest"}
    changed["plan_digest"] = digest(unsigned)
    changed_request = claim_request | {
        "coverage_plan": changed,
        "checker_run": run_record | {"catalog_digest": changed["checker_catalog"]},
    }
    assert check(changed_request, tmp_path)["status"] == "FAIL"
