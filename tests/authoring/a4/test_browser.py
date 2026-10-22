import json

from platform_core.application.control import ControlService
from playwright.sync_api import expect, sync_playwright

from tests.authoring.a4.helpers import catalog_and_materials
from tests.authoring.browser_helpers import connect_workspace


def test_browser_submits_inert_material_and_shows_deployment_match(
    authoring_server, database, owner
):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a4-browser", "A4 Browser")
    _, materials, _ = catalog_and_materials()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, authoring_server, token, project["project_id"], "operators")
        page.get_by_role("tab", name="接入材料", exact=False).click()
        expect(page.get_by_role("heading", name="提交接入材料")).to_be_visible(timeout=20_000)
        expect(page.get_by_role("heading", name="评审记录与部署匹配")).to_be_visible()
        page.get_by_label("接入材料标识").fill("browser-registration")
        page.get_by_label("受限材料 JSON").fill(json.dumps(materials, ensure_ascii=False))
        page.get_by_role("button", name="创建材料草稿").click()
        expect(page.get_by_role("status")).to_contain_text("不会安装或执行", timeout=10_000)
        expect(page.locator("section.proposal-center")).to_contain_text("DRAFT")
        expect(page.locator("section.proposal-center")).to_contain_text("NOT_APPROVED")
        page.get_by_role("button", name="提交维护者评审").click()
        expect(page.get_by_role("status")).to_contain_text("不能自行审核或部署", timeout=10_000)
        expect(page.locator("section.proposal-center")).to_contain_text("SUBMITTED")
        assert "安装算子" not in page.locator("section.proposal-center").inner_text()
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert token not in page.url
        browser.close()
