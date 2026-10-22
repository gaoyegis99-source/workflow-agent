import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.authoring.browser_helpers import create_workspace, upload_asset
from tests.p2.processes import eventually
from tests.p42.test_spatial_workflow_e2e import FIXTURE, p42_cluster  # noqa: F401


def test_browser_runs_and_previews_trusted_static_map(
    p42_cluster,  # noqa: F811
    owner,
):
    cluster = p42_cluster
    cluster.launch("execution-worker")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        project_id = create_workspace(
            page, cluster.env["V7_PLATFORM_URL"], owner[1], "Browser cartography", "assets"
        )
        cluster.client.headers["Authorization"] = "Bearer " + owner[1]
        cluster.project = project_id
        cluster.p = "/api/v1/projects/" + project_id
        source = json.loads(Path("examples/workflows/qgis-buffer-summary-map-v1.json").read_text())
        queued = cluster.request("POST", cluster.p + "/workflow-packages", source)
        cluster.launch("platform-worker")
        eventually(
            lambda: (
                value
                if (value := cluster.request("GET", queued["status_url"]))["status"] == "READY"
                else None
            ),
            timeout=40,
        )

        upload_asset(page, FIXTURE)
        page.locator(".ui-table tbody tr.clickable").first.click()
        page.get_by_role("tab", name="字段 / 空间").click()
        expect(page.locator(".json-view")).to_contain_text("EPSG:32650", timeout=30_000)
        page.get_by_role("tab", name="语义修订").click()
        page.get_by_role("button", name="保存修订").click()

        page.get_by_role("link", name="任务", exact=True).click()
        page.get_by_role("button", name="创建任务").click()
        page.get_by_label("任务模板").select_option("SPATIAL")
        page.get_by_label("研究目标").fill("Browser trusted static map")
        page.get_by_role("button", name="下一步").click()
        asset = page.get_by_label("空间矢量资产")
        expect(asset.locator("option")).to_have_count(2, timeout=20_000)
        asset.select_option(index=1)
        expect(page.get_by_text("精确图层、算子和地图参数", exact=False)).to_be_visible(
            timeout=20_000
        )
        page.get_by_role("button", name="下一步").click()
        page.get_by_role("dialog", name="创建任务").get_by_role(
            "button", name="创建任务", exact=True
        ).click()
        cluster.task = page.url.rstrip("/").split("/")[-1]

        page.get_by_role("tab", name="授权").click()
        page.get_by_label("空间资产").select_option(index=1)
        expect(page.get_by_label("精确图层").locator("option")).to_have_count(2, timeout=20_000)
        page.get_by_label("附加 READY 工作流").select_option(index=1)
        page.get_by_role("button", name="创建授权").click()

        page.get_by_role("tab", name="运行").click()
        page.get_by_label("空间资产").select_option(index=1)
        expect(page.get_by_label("精确图层").locator("option")).to_have_count(2, timeout=20_000)
        page.get_by_label("READY 工作流").select_option(index=1)
        expect(page.get_by_label("节点额度")).to_have_value("3")
        page.get_by_label("地图标题").fill("浏览器缓冲区成果")
        page.get_by_role("button", name="运行工作流").click()
        activity = page.get_by_role("heading", name="运行活动").locator("xpath=ancestor::section")
        expect(activity).to_contain_text("SUCCEEDED", timeout=200_000)
        expect(page.locator(".metric-grid")).to_contain_text("OPEN")

        assets = cluster.request("GET", cluster.p + "/assets")["items"]
        map_asset = next(
            item for item in assets if item["manifest"].get("result_kind") == "CARTOGRAPHIC_PRODUCT"
        )
        page.get_by_role("link", name="数据资产", exact=True).click()
        expect(page.locator(".ui-table tbody tr.clickable")).to_have_count(4, timeout=30_000)
        page.locator(".ui-table tbody tr.clickable", has_text=map_asset["ref"]["asset_id"]).click()
        page.get_by_role("tab", name="静态地图").click()
        page.get_by_role("button", name="加载预览").click()
        image = page.get_by_role("img", name=f"{map_asset['ref']['asset_id']} 静态地图")
        expect(image).to_be_visible(timeout=30_000)
        expect(image).to_have_js_property("naturalWidth", 1754)
        page.get_by_role("tab", name="表示形式").click()
        with page.expect_download() as transfer:
            page.get_by_role("button", name="下载").first.click()
        assert transfer.value
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        browser.close()
