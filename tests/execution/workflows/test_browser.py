from playwright.sync_api import expect, sync_playwright

from tests.execution.workflows.test_workflow_e2e import PACKAGE_READY_TIMEOUT, package
from tests.platform.database.processes import eventually
from tests.platform.services.test_browser import open_page


def test_browser_runs_frozen_workflow_shows_nodes_and_completes(p4_cluster, owner):
    cluster = p4_cluster
    cluster.bootstrap(owner[1], budget=3)
    receipt = cluster.request("POST", cluster.p + "/workflow-packages", package(cluster.targets))
    workers = cluster.launch_platform_worker_lanes()

    def ready():
        status = workers.poll()
        assert status is None, (
            f"Platform worker lane exited with status {status}; inspect {cluster.directory}"
        )
        value = cluster.request("GET", receipt["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    eventually(ready, timeout=PACKAGE_READY_TIMEOUT)
    cluster.launch("execution-worker")
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, cluster, owner[1])
        page.get_by_role("tab", name="授权").click()
        workflow = page.get_by_label("附加 READY 工作流")
        expect(workflow.locator("option")).to_have_count(2, timeout=20_000)
        workflow.select_option(index=1)
        page.get_by_role("button", name="创建授权").click()
        expect(page.locator(".toast")).to_contain_text("授权已创建", timeout=20_000)

        page.get_by_role("tab", name="运行").click()
        workflow = page.get_by_label("READY 工作流")
        expect(workflow.locator("option")).to_have_count(2, timeout=20_000)
        workflow.select_option(index=1)
        page.get_by_role("button", name="运行工作流").click()
        activity = page.get_by_role("heading", name="运行活动").locator("xpath=ancestor::section")
        expect(activity).to_contain_text("SUCCEEDED", timeout=120_000)

        page.get_by_role("link", name="运行", exact=True).click()
        row = page.locator(".ui-table tbody tr.clickable", has_text="SUCCEEDED").first
        expect(row).to_be_visible(timeout=30_000)
        row.click()
        nodes = page.get_by_role("heading", name="工作流节点").locator("xpath=ancestor::section")
        expect(nodes).to_contain_text("complete", timeout=30_000)
        expect(nodes).to_contain_text("trend")
        expect(page.locator(".metric-grid")).to_contain_text("COMMITTED")

        page.get_by_role("link", name="任务", exact=True).click()
        page.locator(".ui-table tbody tr.clickable", has_text=cluster.task).click()
        page.get_by_role("tab", name="交付").click()
        execution = page.get_by_label("成功工作流执行")
        expect(execution.locator("option")).to_have_count(2, timeout=30_000)
        execution.select_option(index=1)
        page.get_by_label("发布输出（JSON 字符串数组）").fill('["complete_result", "trend_result"]')
        page.get_by_role("button", name="登记工作流交付").click()
        expect(page.locator(".toast")).to_contain_text("工作流候选交付已登记", timeout=20_000)
        page.get_by_role("tab", name="验收").click()
        deliverable = page.get_by_label("当前候选交付")
        expect(deliverable.locator("option")).to_have_count(2, timeout=20_000)
        deliverable.select_option(index=1)
        page.get_by_role("button", name="提交验收").click()
        expect(page.get_by_role("heading", name="验收 PASS")).to_be_visible(timeout=60_000)
        expect(page.locator(".metric-grid")).to_contain_text("COMPLETED")
        expect(page.locator(".metric-grid")).to_contain_text("3 / 3")
        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        browser.close()
