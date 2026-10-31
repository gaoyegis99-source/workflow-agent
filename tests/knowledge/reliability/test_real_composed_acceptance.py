import subprocess
from datetime import UTC, datetime, timedelta

from platform_core.application.catalog import OPEN_CHECK_TARGET

from scripts.maintenance.project_environment import docker_executable
from tests.knowledge.checkers.test_composed_runs import analysis_request
from tests.knowledge.identity.test_contracts import EXPECTED
from tests.platform.database.processes import eventually
from tests.platform.services.harness import kill_owned_test_container


def bootstrap_open_task(cluster, token, *, budget=4, max_check_executions=3):
    cluster.bootstrap(token, budget=1, project_name="P5-R2.1 real composition")
    spec = {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "reproduce",
                "description": "Independently reproduce the published trend",
                "necessary": True,
                "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
            }
        ],
        "required_outputs": [{"role_id": "trend_result", "necessary": True, "expected": EXPECTED}],
        "allow_manual_review": False,
        "max_check_executions": max_check_executions,
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Verify one descriptive trend with trusted composed checkers",
                "requirements": [],
                "acceptance_spec": spec,
            },
            "budget": {"execution_count": budget, "max_in_flight": 1},
        },
        "open-task",
    )
    cluster.task = task["task_id"]
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [cluster.targets[1], OPEN_CHECK_TARGET],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        "open-grant",
    )
    return task, spec


def exact_trend_deliverable(cluster, execution):
    receipt = execution["runtime"]["publication_receipt"]
    ref = receipt["asset"]
    asset = cluster.request(
        "GET", cluster.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
    )
    result = next(
        value
        for value in asset["manifest"]["representations"]
        if value["representation_id"] == "result"
    )
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": execution["action_id"],
        "execution_id": execution["execution_id"],
        "output_port": "result",
        "asset_ref": ref,
        "representation_id": "result",
        "representation_digest": result["digest"],
        "schema_id": result["schema_id"],
        "schema_source": "MANIFEST",
    }
    task_path = cluster.p + "/tasks/" + cluster.task
    return cluster.request(
        "POST",
        task_path + "/deliverables",
        {
            "expected_spec_revision": 1,
            "role_bindings": [{"role_id": "trend_result", "output_ref": output_ref}],
        },
        "exact-deliverable",
    )


def register_analysis(cluster):
    task_path = cluster.p + "/tasks/" + cluster.task
    created = cluster.request(
        "POST", task_path + "/analysis-revisions", analysis_request(), "analysis"
    )
    return cluster.request(
        "POST",
        task_path + f"/analysis-revisions/{created['revision_id']}/register",
        {"expected_spec_revision": 1},
        "registration",
    )


def prepare_acceptance(cluster, token):
    task, spec = bootstrap_open_task(cluster, token)
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    # A reconciled container run can legitimately consume more than 90 seconds
    # when the rootless Docker API applies backpressure: each bounded inspect
    # attempt may take 30 seconds before the worker safely retries the same
    # external effect.  Keep the individual runtime timeout bounded and give
    # this real end-to-end deadline room for several recovery attempts.
    trend = cluster.completed_execution(cluster.submit(index=1, key="trend"), timeout=180)
    assert trend["state"] == "SUCCEEDED"
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/budget")["in_flight"] == 0
        ),
        timeout=30,
    )
    deliverable = exact_trend_deliverable(cluster, trend)
    registration = register_analysis(cluster)
    task_path = cluster.p + "/tasks/" + cluster.task
    body = {
        "expected_spec_revision": 1,
        "deliverable_id": deliverable["deliverable_id"],
        "analysis_registration_id": registration["registration_id"],
    }
    return task_path, body, spec, trend


def completed_acceptance(cluster, acceptance, timeout=120):
    return eventually(
        lambda: (
            value
            if (value := cluster.request("GET", acceptance["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=timeout,
    )


def running_checker_container(cluster, acceptance_id):
    with cluster.database.transaction() as uow:
        runs = uow.science.check_runs(acceptance_id)
        first = runs[0]
        if first["action_id"] is None:
            return None
        action = uow.actions.get(first["action_id"])
    if action["execution_id"] is None:
        return None
    name = "v7-p3-" + action["execution_id"]
    inspected = subprocess.run(
        [docker_executable(), "inspect", name, "--format", "{{.State.Running}}"],
        capture_output=True,
        text=True,
    )
    return (
        (first, action, name)
        if inspected.returncode == 0 and inspected.stdout.strip() == "true"
        else None
    )


def test_real_platform_execution_and_two_checker_containers_complete_verified_task(
    r2_cluster, owner
):
    cluster = r2_cluster
    task_path, body, _, _ = prepare_acceptance(cluster, owner[1])
    acceptance = cluster.request(
        "POST",
        task_path + "/acceptance",
        body,
        "real-composed-acceptance",
    )
    report = completed_acceptance(cluster, acceptance)
    assert report["status"] == "PASS", report
    assert [value["capability_id"] for value in report["check_runs"]] == [
        "platform-runtime-integrity-v1",
        "p3-timeseries-reproduction-v1",
    ]
    assert {value["status"] for value in report["check_runs"]} == {"PASS"}
    assert cluster.request("GET", task_path)["lifecycle"] == "COMPLETED"
    budget = cluster.request("GET", task_path + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
    assert len({value["action_id"] for value in report["check_runs"]}) == 2


def test_api_and_workers_restart_replay_original_acceptance_and_checker_actions(r2_cluster, owner):
    cluster = r2_cluster
    task_path, body, _, _ = prepare_acceptance(cluster, owner[1])
    cluster.kill(cluster.running["platform-worker-lanes"])
    cluster.kill(cluster.running["execution-worker"])

    headers = {"Idempotency-Key": "lost-acceptance-response"}
    first = cluster.client.post(task_path + "/acceptance", json=body, headers=headers)
    assert first.status_code == 202, first.text
    acceptance_id = first.json()["acceptance_id"]
    cluster.restart("platform")
    replay = cluster.client.post(task_path + "/acceptance", json=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["acceptance_id"] == acceptance_id

    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    report = completed_acceptance(cluster, replay.json())
    assert report["status"] == "PASS", report
    actions = [value["action_id"] for value in report["check_runs"]]
    assert len(actions) == len(set(actions)) == 2
    with cluster.database.transaction() as uow:
        assert len(uow.science.check_runs(acceptance_id)) == 2
    budget = cluster.request("GET", task_path + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)


def test_task_revision_makes_queued_composed_plan_stale_without_checker_execution(
    r2_cluster, owner
):
    cluster = r2_cluster
    task_path, body, spec, _ = prepare_acceptance(cluster, owner[1])
    cluster.kill(cluster.running["platform-worker-lanes"])
    cluster.kill(cluster.running["execution-worker"])
    acceptance = cluster.request("POST", task_path + "/acceptance", body, "stale-acceptance")
    task = cluster.request("GET", task_path)
    cluster.request(
        "POST",
        task_path + "/spec-revisions",
        {
            "expected_control_version": task["control_version"],
            "expected_spec_revision": 1,
            "spec": {
                "goal": "A revised goal invalidates the frozen checker plan",
                "requirements": [],
                "acceptance_spec": spec,
            },
        },
        "revise-before-checker",
    )
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    report = completed_acceptance(cluster, acceptance)
    assert report["status"] == "STALE", report
    assert not any(value["status"] in {"PASS", "REUSED"} for value in report["check_runs"]), report
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", task_path + "/budget"))["reserved"] == 0
            and value["in_flight"] == 0
            else None
        ),
        timeout=30,
    )
    assert budget["used"] == 1


def test_real_checker_termination_retries_only_failed_run_and_unrun_downstream(
    delayed_r2_cluster, owner
):
    cluster = delayed_r2_cluster
    task_path, body, _, _ = prepare_acceptance(cluster, owner[1])
    acceptance = cluster.request(
        "POST", task_path + "/acceptance", body, "terminated-checker-acceptance"
    )

    first_run, first_action, container = eventually(
        lambda: running_checker_container(cluster, acceptance["acceptance_id"]), timeout=45
    )
    kill_owned_test_container(container, cluster.test_run_id)
    failed = completed_acceptance(cluster, acceptance, timeout=120)
    assert failed["status"] == "ERROR", failed
    assert [value["status"] for value in failed["check_runs"]] == ["ERROR", "SKIPPED"]

    retry_body = body | {"retry_of": acceptance["acceptance_id"]}
    retry = cluster.request(
        "POST", task_path + "/acceptance", retry_body, "retry-terminated-checker"
    )
    report = completed_acceptance(cluster, retry, timeout=150)
    assert report["status"] == "PASS", report
    assert [value["status"] for value in report["check_runs"]] == ["PASS", "PASS"]

    with cluster.database.transaction() as uow:
        initial_runs = uow.science.check_runs(acceptance["acceptance_id"])
        retry_runs = uow.science.check_runs(retry["acceptance_id"])
    assert initial_runs[0]["action_id"] == first_action["action_id"]
    assert initial_runs[1]["action_id"] is None
    assert retry_runs[0]["retry_of_run_id"] == first_run["run_id"]
    assert retry_runs[1]["retry_of_run_id"] == initial_runs[1]["run_id"]
    assert len({value["action_id"] for value in retry_runs}) == 2
    budget = cluster.request("GET", task_path + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (4, 0, 0)


def test_task_cancel_stops_real_checker_and_releases_unstarted_downstream(
    delayed_r2_cluster, owner
):
    cluster = delayed_r2_cluster
    task_path, body, _, _ = prepare_acceptance(cluster, owner[1])
    acceptance = cluster.request(
        "POST", task_path + "/acceptance", body, "cancel-real-checker-acceptance"
    )
    _, _, container = eventually(
        lambda: running_checker_container(cluster, acceptance["acceptance_id"]), timeout=45
    )
    task = cluster.request("GET", task_path)
    cluster.request(
        "POST",
        task_path + "/cancel",
        {"expected_control_version": task["control_version"]},
        "cancel-during-real-checker",
    )
    report = completed_acceptance(cluster, acceptance, timeout=120)
    assert report["status"] == "ERROR", report
    assert [value["status"] for value in report["check_runs"]] == ["ERROR", "SKIPPED"]
    assert cluster.request("GET", task_path)["lifecycle"] == "CANCELLED"
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", task_path + "/budget"))["reserved"] == 0
            and value["in_flight"] == 0
            else None
        ),
        timeout=30,
    )
    assert budget["used"] == 2
    inspected = subprocess.run(
        [docker_executable(), "inspect", container, "--format", "{{.State.Running}}"],
        capture_output=True,
        text=True,
    )
    assert inspected.returncode != 0 or inspected.stdout.strip() == "false"
