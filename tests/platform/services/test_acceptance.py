from copy import deepcopy

import pytest
from platform_checkers.check import check
from platform_contracts.service import digest
from platform_operators.cli import run

from scripts.maintenance.project_environment import docker_executable
from tests.platform.database.processes import eventually
from tests.platform.services.test_science import request, rows


def examples():
    req = request()
    spec = {
        "input": req["binding"]["selection"],
        "parameters": req["parameters"],
        "required_outputs": ["statistics-linear-trend"],
        "allow_not_evaluable": True,
        "minimum_evaluable": 1,
    }
    result = run(req, rows())
    manifest = {
        "kind": "OPERATOR_RESULT",
        "source": {
            "target": req["target"],
            "task_ref": {"task_id": "task", "spec_revision": 1},
            "input": spec["input"],
            "parameters": spec["parameters"],
            "semantics": req["binding"]["semantics"],
        },
    }
    req.update(
        acceptance_spec=spec,
        task_id="task",
        expected_spec_revision=1,
        artifacts=[
            {"manifest": manifest, "ref": {"version_manifest_digest": "sha256:" + digest(manifest)}}
        ],
    )
    output = {
        "result": result,
        "table": deepcopy(result["rows"]),
        "method": {k: v for k, v in result.items() if k != "rows"},
    }
    return req, [output]


@pytest.mark.parametrize(
    "fault", ["none", "slope", "missing-group", "fake-reason", "source", "table", "method-claim"]
)
def test_checker_detects_wrong_science_and_provenance(fault):
    req, outputs = examples()
    result = outputs[0]["result"]
    if fault == "slope":
        result["rows"][0]["slope_per_day"] += 0.1
    elif fault == "missing-group":
        result["rows"].pop()
    elif fault == "fake-reason":
        result["rows"][0].update(status="NOT_EVALUABLE", reason="INSUFFICIENT_DISTINCT_TIMES")
    elif fault == "source":
        req["artifacts"][0]["manifest"]["source"]["task_ref"]["task_id"] = "other"
    elif fault == "table":
        outputs[0]["table"][0]["slope_per_day"] = 999
    elif fault == "method-claim":
        result["method"] = "Significant causal prediction; p=0.00001."
        outputs[0]["method"]["method"] = result["method"]
    if fault != "table":
        outputs[0]["table"] = deepcopy(result["rows"])
    report = check(req, rows(), outputs)
    assert report["status"] == ("PASS" if fault == "none" else "FAIL")


def test_checker_reports_missing_output_as_coverage_failure():
    req, _ = examples()
    report = check(req, rows(), [])
    assert report["status"] == "FAIL"
    assert "OUTPUT_COVERAGE" in report["failures"]


def test_two_operators_check_and_complete_use_three_executions(cluster, owner):
    cluster.bootstrap(owner[1])
    commands = [cluster.submit(index=i) for i in range(2)]
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    executions = [cluster.completed_execution(command) for command in commands]
    assert all(value["state"] == "SUCCEEDED" for value in executions), executions
    task_path = cluster.p + "/tasks/" + cluster.task
    eventually(lambda: cluster.request("GET", task_path + "/budget")["in_flight"] == 0)
    candidate = cluster.register_operator_deliverable(executions)
    body = {"expected_spec_revision": 1, "deliverable_id": candidate["deliverable_id"]}
    receipt = cluster.request("POST", task_path + "/acceptance", body, "check")
    assert cluster.request("POST", task_path + "/acceptance", body, "check") == receipt
    assert cluster.request("POST", task_path + "/acceptance", body, "different-key") == receipt

    def done():
        value = cluster.request("GET", receipt["status_url"])
        return value if value["status"] != "QUEUED" else None

    report = eventually(done, timeout=60)
    assert report["status"] == "PASS", report
    task = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", task_path))["lifecycle"] == "COMPLETED"
            else None
        )
    )
    assert task["spec_revision"] == 1
    budget = cluster.request("GET", task_path + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
    assert report["completion"]["report_digest"] == report["report_digest"]
    assert report["report"]["scope"].startswith("Frozen structured")


def finish_operators(cluster, token, budget=3):
    cluster.bootstrap(token, budget=budget)
    commands = [cluster.submit(index=i) for i in range(2)]
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    executions = [cluster.completed_execution(command) for command in commands]
    assert all(value["state"] == "SUCCEEDED" for value in executions), executions
    path = cluster.p + "/tasks/" + cluster.task
    eventually(lambda: cluster.request("GET", path + "/budget")["in_flight"] == 0)
    candidate = cluster.register_operator_deliverable(executions)
    return path, {"expected_spec_revision": 1, "deliverable_id": candidate["deliverable_id"]}


def fault_image(cluster, kind):
    import subprocess
    from pathlib import Path

    directory = cluster.directory / "fault-image"
    directory.mkdir()
    if kind == "operator":
        (directory / "fault.py").write_bytes(
            Path(__file__).with_name("fault_operator.py").read_bytes()
        )
        instructions = 'COPY fault.py /fault.py\nENTRYPOINT ["python", "/fault.py"]\n'
    else:
        instructions = 'ENTRYPOINT ["python", "-c", "raise SystemExit(2)"]\n'
    (directory / "Dockerfile").write_text("FROM groundwater-v7-" + kind + ":0.2.1\n" + instructions)
    subprocess.run(
        [
            docker_executable(),
            "build",
            "-q",
            "--iidfile",
            str(directory / "image-id"),
            str(directory),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return (directory / "image-id").read_text().strip()


def test_wrong_results_never_complete_and_new_key_cannot_reroll(cluster, owner):
    cluster.install_operator_image(fault_image(cluster, "operator"))
    path, body = finish_operators(cluster, owner[1])
    receipt = cluster.request("POST", path + "/acceptance", body)
    report = eventually(
        lambda: (
            r
            if (r := cluster.request("GET", receipt["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=60,
    )
    assert report["status"] == "FAIL", report
    assert cluster.request("GET", path)["lifecycle"] == "OPEN"
    repeat = cluster.request("POST", path + "/acceptance", body, "new-key-after-fail")
    assert repeat["acceptance_id"] == receipt["acceptance_id"] and repeat["status"] == "FAIL"
    assert cluster.request("GET", path + "/budget")["used"] == 3
    forged = cluster.client.post(
        path + "/acceptance", json=body | {"result": "PASS"}, headers={"Idempotency-Key": "forged"}
    )
    assert forged.status_code == 400


def test_acceptance_budget_is_not_exempt(cluster, owner):
    path, body = finish_operators(cluster, owner[1], budget=2)
    response = cluster.client.post(
        path + "/acceptance", json=body, headers={"Idempotency-Key": "no-budget"}
    )
    assert response.status_code == 409 and response.json()["code"] == "BUDGET_BLOCKED"
    assert cluster.request("GET", path + "/acceptance")["items"] == []


def test_checker_error_requires_explicit_budgeted_retry(cluster, owner):
    image = cluster.env["V7_CHECKER_IMAGE"]
    cluster.env["V7_CHECKER_IMAGE"] = fault_image(cluster, "checker")
    path, body = finish_operators(cluster, owner[1], budget=4)
    receipt = cluster.request("POST", path + "/acceptance", body)
    report = eventually(
        lambda: (
            r
            if (r := cluster.request("GET", receipt["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=60,
    )
    assert report["status"] == "ERROR" and report["report"]["executed"]
    response = cluster.client.post(
        path + "/acceptance", json=body, headers={"Idempotency-Key": "implicit-retry"}
    )
    assert response.status_code == 409
    cluster.env["V7_CHECKER_IMAGE"] = image
    cluster.restart("execution-worker")
    retry = cluster.request(
        "POST", path + "/acceptance", body | {"retry_of": receipt["acceptance_id"]}
    )
    result = eventually(
        lambda: (
            r if (r := cluster.request("GET", retry["status_url"]))["status"] != "QUEUED" else None
        ),
        timeout=60,
    )
    assert result["status"] == "PASS"
    assert cluster.request("GET", path + "/budget")["used"] == 4


def control_before_completion(cluster, owner, control):
    from platform_contracts import DeploymentContext
    from platform_core.application.acceptance import AcceptanceProcessor
    from platform_execution.adapters.repository import Repository
    from platform_infrastructure.services import ExecutionClient
    from platform_infrastructure.worker import Worker

    path, body = finish_operators(cluster, owner[1])
    cluster.kill(cluster.running["platform-worker-lanes"])
    receipt = cluster.request("POST", path + "/acceptance", body)
    transport = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"],
        cluster.env["V7_PLATFORM_EXECUTION_TOKEN"],
        deployment=DeploymentContext(),
    )
    worker = Worker(cluster.database, transport, lane="action")
    report = cluster.request("GET", receipt["status_url"])
    # Deliver the checker action while leaving completion controlled by this test driver.
    for _ in range(10):
        worker.process_one()
        try:
            row = Repository(cluster.service_databases[1]).get(action=report["action_id"])
            break
        except Exception:
            continue
    row = eventually(
        lambda: (
            r
            if (r := Repository(cluster.service_databases[1]).get(action=report["action_id"]))[
                "state"
            ]
            == "SUCCEEDED"
            else None
        ),
        timeout=45,
    )
    assert row["executed"]
    if control == "revise":
        cluster.request(
            "POST",
            path + "/spec-revisions",
            {
                "expected_control_version": 1,
                "expected_spec_revision": 1,
                "spec": {
                    "goal": "Revised goal",
                    "requirements": [],
                    "acceptance_spec": cluster.spec,
                },
            },
        )
    else:
        cluster.request("POST", path + "/" + control, {"expected_control_version": 1})

    def observed():
        worker.process_one()
        AcceptanceProcessor(cluster.database, transport).process()
        report = cluster.request("GET", receipt["status_url"])
        return report if report["status"] != "QUEUED" else None

    report = eventually(observed, timeout=20)
    assert report["status"] == "PASS"
    assert cluster.request("GET", path)["lifecycle"] != "COMPLETED"
    assert report["stale"] == (control == "revise")
    if control == "pause":
        cluster.request("POST", path + "/resume", {"expected_control_version": 2})
        AcceptanceProcessor(cluster.database, transport).process()
        assert cluster.request("GET", path)["lifecycle"] == "COMPLETED"
    transport.close()


@pytest.mark.parametrize("control", ["pause", "cancel", "revise"])
def test_control_and_revision_win_before_completion(cluster, owner, control):
    control_before_completion(cluster, owner, control)
