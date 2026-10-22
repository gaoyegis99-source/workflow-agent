import json

from playwright.sync_api import expect, sync_playwright

from tests.authoring.browser_helpers import connect_task, create_workspace, upload_asset


def click(page, name):
    button = page.get_by_role("button", name=name, exact=True)
    button.click()
    expect(button).to_be_enabled(timeout=20_000)


def open_page(playwright, cluster, token):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    connect_task(
        page,
        cluster.env["V7_PLATFORM_URL"],
        token,
        cluster.project,
        cluster.task,
    )
    return browser, page


def test_browser_complete_refresh_offline_and_no_token_storage(cluster, owner, tmp_path):
    cluster.launch("platform-worker")
    cluster.launch("execution-worker")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        project_id = create_workspace(
            page, cluster.env["V7_PLATFORM_URL"], owner[1], "Browser synthetic", "assets"
        )
        cluster.client.headers["Authorization"] = "Bearer " + owner[1]
        cluster.project = project_id
        cluster.p = "/api/v1/projects/" + project_id

        source = tmp_path / "browser-synthetic.csv"
        source.write_text("station,time,value\nA,2024-01-01,1\nA,2024-01-02,3\nA,2024-01-03,5\n")
        columns = json.dumps(
            [{"name": name, "type": "string"} for name in ["station", "time", "value"]]
        )
        upload_asset(page, source, columns)
        page.locator(".ui-table tbody tr.clickable").first.click()
        page.get_by_role("tab", name="字段 / 空间").click()
        expect(page.locator(".json-view")).to_contain_text("f0", timeout=30_000)
        page.get_by_role("tab", name="语义修订").click()
        page.get_by_label("语义负载（JSON）").fill(
            json.dumps(
                {
                    "timezone": "UTC",
                    "value_unit": "m",
                    "quantity": "water_level_elevation",
                }
            )
        )
        page.get_by_role("button", name="保存修订").click()
        expect(page.get_by_role("status")).to_contain_text("语义修订已追加", timeout=20_000)

        page.get_by_role("link", name="任务", exact=True).click()
        page.get_by_role("button", name="创建任务").click()
        page.get_by_label("任务模板").select_option("TIMESERIES")
        page.get_by_label("研究目标").fill("Synthetic completeness and descriptive trend")
        page.get_by_role("button", name="下一步").click()
        page.get_by_label("观测资产").select_option(index=1)
        expect(page.get_by_label("分组字段")).to_be_visible(timeout=20_000)
        page.get_by_label("分组值（JSON）").fill('["A", "absent"]')
        page.get_by_label("允许部分不可评估").select_option("yes")
        page.get_by_role("button", name="下一步").click()
        page.get_by_label("执行次数").fill("4")
        page.get_by_role("dialog", name="创建任务").get_by_role(
            "button", name="创建任务", exact=True
        ).click()
        expect(page.get_by_role("tab", name="范围")).to_be_visible(timeout=20_000)
        expect(page.locator(".json-view").first).to_contain_text("p3-timeseries-v1")
        cluster.task = page.url.rstrip("/").split("/")[-1]

        page.get_by_role("tab", name="授权").click()
        page.get_by_role("button", name="创建授权").click()
        expect(page.locator(".toast")).to_contain_text("授权已创建", timeout=20_000)
        page.get_by_role("tab", name="运行").click()
        activity = page.get_by_role("heading", name="运行活动").locator("xpath=ancestor::section")
        click(page, "timeseries-completeness")
        expect(activity).to_contain_text("SUCCEEDED", timeout=60_000)
        click(page, "timeseries-completeness")
        click(page, "statistics-linear-trend")
        expect(activity.locator("tbody tr")).to_have_count(3, timeout=60_000)
        expect(activity.locator(".ui-badge.success")).to_have_count(3, timeout=60_000)

        page.get_by_role("tab", name="交付").click()
        completeness = page.get_by_label("timeseries-completeness")
        expect(completeness.locator("option")).to_have_count(3, timeout=60_000)
        completeness.select_option(index=1)
        trend = page.get_by_label("statistics-linear-trend")
        expect(trend.locator("option")).to_have_count(2, timeout=60_000)
        trend.select_option(index=1)
        page.get_by_role("button", name="登记候选交付").click()
        expect(page.locator(".toast")).to_contain_text("候选交付已登记", timeout=20_000)
        page.get_by_role("tab", name="验收").click()
        deliverable = page.get_by_label("当前候选交付")
        expect(deliverable.locator("option")).to_have_count(2, timeout=20_000)
        deliverable.select_option(index=1)
        page.get_by_role("button", name="提交验收").click()
        expect(page.get_by_role("heading", name="验收 PASS")).to_be_visible(timeout=60_000)
        expect(page.locator(".metric-grid")).to_contain_text("COMPLETED")
        expect(page.locator(".metric-grid")).to_contain_text("4 / 4")

        page.get_by_role("link", name="数据资产", exact=True).click()
        expect(page.locator(".ui-table tbody tr.clickable")).to_have_count(4, timeout=60_000)
        page.locator(".ui-table tbody tr.clickable", has_text="OPERATOR_RESULT").first.click()
        page.get_by_role("tab", name="表示形式").click()
        with page.expect_download() as transfer:
            page.get_by_role("button", name="下载").first.click()
        assert transfer.value
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert owner[1] not in page.url

        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        page.goto(
            f"{cluster.env['V7_PLATFORM_URL']}/projects/{cluster.project}/tasks/{cluster.task}"
        )
        page.get_by_label("临时 Bearer Token").fill(owner[1])
        page.get_by_role("button", name="连接并加载项目").click()
        expect(page.locator(".metric-grid")).to_contain_text("COMPLETED", timeout=20_000)
        browser.close()


def test_browser_current_permissions_and_budget_error(cluster, owner):
    from tests.p3.test_acceptance import finish_operators

    path, _ = finish_operators(cluster, owner[1], budget=2)
    principal = cluster.database.create_principal("viewer")
    token = cluster.database.issue_token(principal)["token"]
    cluster.database.set_member(cluster.project, principal, "VIEWER", actor=owner[0])
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, cluster, token)
        page.get_by_role("tab", name="授权").click()
        expect(page.get_by_role("button", name="创建授权")).to_be_disabled()
        assert cluster.request("GET", path)["lifecycle"] == "OPEN"

        page.get_by_role("button", name="断开连接").click()
        page.get_by_label("临时 Bearer Token").fill(owner[1])
        page.get_by_role("button", name="连接并加载项目").click()
        page.get_by_role("tab", name="验收").click()
        page.get_by_label("当前候选交付").select_option(index=1)
        page.get_by_role("button", name="提交验收").click()
        expect(page.locator(".toast")).to_contain_text("预算或在途名额不足", timeout=20_000)
        browser.close()


def test_browser_cancel_unknown_is_visible_during_data_outage(cluster, owner):
    from tests.p2.processes import eventually

    cluster.bootstrap(owner[1])
    command = cluster.submit()
    cluster.launch("platform-worker")
    eventually(lambda: cluster.request("GET", command["status_url"])["result"])
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, cluster, owner[1])
        cluster.kill(cluster.running["data"])
        cluster.launch("execution-worker")
        page.get_by_role("button", name="取消任务").click()
        page.get_by_label("原因").fill("Browser cancellation during Data outage")
        page.get_by_role("button", name="确认操作").click()
        expect(page.locator(".metric-grid")).to_contain_text("CANCELLED", timeout=20_000)
        expect(page.locator(".metric-grid")).to_contain_text("UNKNOWN", timeout=30_000)
        browser.close()


def test_browser_marks_old_report_after_requirement_revision(cluster, owner):
    from tests.p3.test_acceptance import control_before_completion

    control_before_completion(cluster, owner, "revise")
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, cluster, owner[1])
        page.get_by_role("tab", name="验收").click()
        expect(page.get_by_text("旧要求报告，不适用于当前修订")).to_be_visible()
        expect(page.locator(".metric-grid")).to_contain_text("OPEN")
        browser.close()
