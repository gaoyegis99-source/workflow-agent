import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platform_core.application.catalog import OPEN_CHECK_TARGET, build_catalog, target
from playwright.sync_api import expect, sync_playwright

from tests.authoring.browser_helpers import connect_workspace
from tests.p2.processes import eventually
from tests.p5r2.test_composed_runs import analysis_request
from tests.p71b.test_real_agent_acceptance import CASES, _acceptance, _upload
from tests.p72b.test_real_filter_acceptance import filter_parameters
from tests.p73a.test_real_aggregate_acceptance import AGGREGATE_PARAMETERS


def authoring_once(cluster):
    process = cluster.start(
        [sys.executable, "-m", "platform_agent_runtime.authoring_worker", "--once"],
        cluster.env,
    )
    assert process.wait(timeout=120) == 0


def submit_trial_in_studio(
    cluster,
    token,
    definition,
    stable_key,
    candidate_id,
    task_id,
    inputs,
    expectations,
    max_nodes,
):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, cluster.env["V7_PLATFORM_URL"], token, cluster.project, "releases")
        expect(page.get_by_role("status")).to_contain_text("个草稿", timeout=20_000)
        page.get_by_label("工作流定义").select_option(definition["definition_id"])
        candidate = page.get_by_role("button", name=re.compile(r"READY · r\d+")).filter(
            has_text=candidate_id
        )
        expect(candidate).to_be_visible(timeout=20_000)
        candidate.click()
        page.get_by_label("已有 DIRECT Task").select_option(task_id)
        page.get_by_label("最大计算节点").fill(str(max_nodes))
        page.get_by_label("冻结输入 JSON").fill(json.dumps(inputs))
        page.get_by_label("样例断言 JSON").fill(json.dumps(expectations))
        page.get_by_role("button", name="使用任务预算试运行").click()
        expect(page.get_by_role("status")).to_contain_text("真实试运行已提交", timeout=20_000)
        browser.close()
    page = cluster.request(
        "GET", cluster.p + f"/workflow-definitions/{definition['definition_id']}/trial-runs"
    )
    matches = [
        item
        for item in page["items"]
        if item["candidate_id"] == candidate_id and item["task_id"] == task_id
    ]
    assert len(matches) == 1
    return matches[0]


def publish_release_in_studio(
    cluster, token, definition, stable_key, candidate_id, version_label="1.0.0"
):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, cluster.env["V7_PLATFORM_URL"], token, cluster.project, "releases")
        expect(page.get_by_role("status")).to_contain_text("个草稿", timeout=20_000)
        page.get_by_label("工作流定义").select_option(definition["definition_id"])
        candidate = page.get_by_role("button", name=re.compile(r"READY · r\d+")).filter(
            has_text=candidate_id
        )
        expect(candidate).to_be_visible(timeout=20_000)
        candidate.click()
        expect(page.locator(".facts strong", has_text="PASS")).to_be_visible(timeout=20_000)
        page.get_by_label("SemVer").fill(version_label)
        page.get_by_role("button", name="发布 PASS 试运行").click()
        expect(page.get_by_role("status")).to_contain_text(
            f"已发布不可变版本 {version_label}", timeout=20_000
        )
        expect(page.locator(".facts strong", has_text=version_label)).to_be_visible()
        browser.close()
    return cluster.request(
        "GET",
        cluster.p + f"/workflow-definitions/{definition['definition_id']}/releases/{version_label}",
    )


def test_real_candidate_trial_and_release_uses_existing_task_budget(cluster, owner):
    _, token = owner
    selection = _upload(cluster, token)
    source = json.loads(
        Path("examples/workflows/table-filter-aggregate-profile-v1.json").read_text()
    )
    root = source["documents"]["main"]
    root["inputs"] = {"table": "File"}
    root["steps"]["filter"]["in"]["parameters"] = {"default": filter_parameters("f1")}
    root["steps"]["aggregate"]["in"]["parameters"] = {"default": AGGREGATE_PARAMETERS}
    root["steps"]["profile"]["in"]["parameters"] = {"default": {"fields": ["f0", "f1", "f2", "f3"]}}
    definition = cluster.request(
        "POST",
        cluster.p + "/workflow-definitions",
        {
            "expected_revision": 0,
            "stable_key": "a3-real-table",
            "metadata": {"title": "A3 real table workflow"},
            "layout": {},
        },
        key="a3-real-definition",
    )
    revision = cluster.request(
        "POST",
        cluster.p + f"/workflow-definitions/{definition['definition_id']}/revisions",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "source": source,
            "metadata": {"title": "A3 real table workflow"},
            "layout": {},
        },
        key="a3-real-revision",
    )
    descriptor = cluster.request("GET", cluster.p + "/authoring/operator-descriptors")
    validation = cluster.request(
        "POST",
        cluster.p + f"/workflow-definitions/{definition['definition_id']}/validations",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": descriptor["semantic_contract_digest"],
        },
        key="a3-real-validation",
    )
    authoring_once(cluster)
    validation_view = cluster.request("GET", validation["status_url"])
    assert validation_view["status"] == "PASSED", json.dumps(validation_view, indent=2)
    candidate = cluster.request(
        "POST",
        cluster.p + f"/workflow-definitions/{definition['definition_id']}/candidates",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "validation_id": validation["validation_id"],
        },
        key="a3-real-candidate",
    )
    cluster.launch("platform-worker")

    def candidate_ready():
        value = cluster.request("GET", candidate["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    candidate_view = eventually(candidate_ready, timeout=40)
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Qualify a frozen workflow sample",
                "requirements": [],
                "acceptance_spec": _acceptance("table-profile", CASES["table-profile"]),
            },
            "budget": {"execution_count": 5, "max_in_flight": 2},
        },
        key="a3-real-task",
    )
    task_path = cluster.p + "/tasks/" + task["task_id"]
    analysis = cluster.request(
        "POST", task_path + "/analysis-revisions", analysis_request(), "a3-real-analysis"
    )
    registration = cluster.request(
        "POST",
        task_path + f"/analysis-revisions/{analysis['revision_id']}/register",
        {"expected_spec_revision": 1},
        "a3-real-registration",
    )
    catalog = build_catalog(
        {
            "OPERATOR": cluster.env["V7_OPERATOR_IMAGE"],
            "QGIS": "sha256:" + "2" * 64,
            "SPATIAL_NATIVE": "sha256:" + "3" * 64,
            "TABULAR": cluster.env["V7_TABULAR_OPERATOR_IMAGE"],
        }
    )
    leaves = [
        target(item)
        for item in catalog["operators"]
        if item["operator_id"] in {"table-filter", "table-aggregate", "table-profile"}
    ]
    workflow_target = {"kind": "WORKFLOW", "package_ref": candidate_view["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [workflow_target, *leaves, OPEN_CHECK_TARGET],
            "resources": [selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        key="a3-real-grant",
    )
    trial = submit_trial_in_studio(
        cluster,
        token,
        definition,
        "a3-real-table",
        candidate["candidate_id"],
        task["task_id"],
        {"table": selection},
        [
            {
                "output": "aggregate_report",
                "schema_id": "table-aggregate-report-v1",
            },
            {
                "output": "profile_report",
                "schema_id": "table-profile-report-v1",
            },
        ],
        3,
    )
    cluster.launch("execution-worker")
    trial_path = (
        cluster.p
        + f"/workflow-definitions/{definition['definition_id']}/trial-runs/{trial['trial_id']}"
    )

    def completed():
        current = cluster.request("GET", trial_path)
        if not current.get("execution_id"):
            return None
        execution = cluster.request("GET", cluster.p + "/executions/" + current["execution_id"])
        return execution if execution["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"} else None

    execution = eventually(completed, timeout=240)
    assert execution["state"] == "SUCCEEDED", execution
    authoring_once(cluster)
    trial_view = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", trial_path))["case_verdict"] != "PENDING"
            else None
        ),
        timeout=30,
    )
    assert trial_view["case_verdict"] == "PASS", trial_view
    release = publish_release_in_studio(
        cluster,
        token,
        definition,
        "a3-real-table",
        candidate["candidate_id"],
    )
    assert release["qualification"] == "SAMPLE_VERIFIED"
    assert release["current_executable"]

    receipt = execution["runtime"]["publication_receipt"]
    published = receipt["outputs"]["profile_report"]
    ref = published["asset"]
    asset = cluster.request(
        "GET", cluster.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
    )
    representation = next(
        item
        for item in asset["manifest"]["representations"]
        if item["representation_id"] == "report"
    )
    with cluster.database.transaction() as uow:
        parent = uow.actions.by_execution(cluster.project, execution["execution_id"])
        nodes = {node["node_id"]: node for node in uow.workflows.nodes(parent["action_id"])}
    node = nodes[asset["manifest"]["workflow"]["member_id"]]
    deliverable = cluster.request(
        "POST",
        task_path + "/deliverables",
        {
            "expected_spec_revision": 1,
            "role_bindings": [
                {
                    "role_id": "profile_report",
                    "output_ref": {
                        "kind": "WORKFLOW_OUTPUT",
                        "parent_action_id": parent["action_id"],
                        "parent_execution_id": execution["execution_id"],
                        "node_action_id": node["node_id"],
                        "node_execution_id": node["observation"]["execution_id"],
                        "call_path": node["request"]["call_path"],
                        "scatter_path": node["request"]["scatter_indices"],
                        "published_output_port": "profile_report",
                        "published_path": [],
                        "output_port": "report",
                        "asset_ref": ref,
                        "representation_id": "report",
                        "representation_digest": representation["digest"],
                        "schema_id": "table-profile-report-v1",
                        "schema_source": "MANIFEST",
                    },
                }
            ],
        },
        "a3-real-deliverable",
    )
    acceptance = cluster.request(
        "POST",
        task_path + "/acceptance",
        {
            "expected_spec_revision": 1,
            "deliverable_id": deliverable["deliverable_id"],
            "analysis_registration_id": registration["registration_id"],
        },
        "a3-real-acceptance",
    )
    report = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", acceptance["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=180,
    )
    assert report["status"] == "PASS", report
    assert [run["capability_id"] for run in report["check_runs"]] == [
        "platform-runtime-integrity-v1",
        "tabular-profile-reproduction-v1",
    ]
    assert cluster.request("GET", task_path)["lifecycle"] == "COMPLETED"
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (5, 0, 0)
