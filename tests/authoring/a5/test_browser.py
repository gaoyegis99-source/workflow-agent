import json

from platform_core.application.authoring import AuthoringService
from platform_core.application.control import ControlService
from playwright.sync_api import expect, sync_playwright

from tests.authoring.a5.helpers import advanced_source
from tests.authoring.browser_helpers import connect_workspace


def test_browser_safely_imports_and_explicitly_saves_advanced_source(
    authoring_server, database, owner
):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a5-browser", "A5 browser")
    AuthoringService(database).create(
        actor,
        project["project_id"],
        "a5-browser-definition",
        {
            "expected_revision": 0,
            "stable_key": "a5-browser-flow",
            "metadata": {"title": "A5 browser flow"},
            "layout": {},
        },
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, authoring_server, token, project["project_id"], "workflows")
        expect(page.get_by_role("status")).to_contain_text("1 个草稿", timeout=20_000)
        page.locator("aside.sidebar button").filter(has_text="a5-browser-flow").click()
        page.get_by_role("button", name="源码", exact=True).click()
        expect(page.get_by_role("heading", name="受限源码编辑与导入")).to_be_visible()
        source = page.get_by_label("工作流源码")
        source.click(force=True)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(json.dumps(advanced_source()))
        page.get_by_role("button", name="安全解析并投影").click()
        expect(page.get_by_role("status")).to_contain_text("源码已安全解析", timeout=20_000)
        page.get_by_role("button", name="可视化", exact=True).click()
        expect(page.locator(".flow-node").filter(has_text="SUBWORKFLOW")).to_be_visible()
        expect(page.locator(".flow-node").filter(has_text="nested_crossproduct")).to_have_count(0)
        expect(page.locator(".flow-node").filter(has_text="dotproduct")).to_be_visible()
        expect(page.locator(".flow-node").filter(has_text="条件 $(inputs.enabled)")).to_be_visible()
        expect(page.locator("aside.sidebar button.selected")).to_contain_text("r1")
        page.get_by_role("button", name="保存修订").click()
        expect(page.get_by_role("status")).to_contain_text("revision 2", timeout=20_000)
        expect(page.locator("aside.sidebar button.selected")).to_contain_text("r2")
        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        assert token not in page.url
        browser.close()
