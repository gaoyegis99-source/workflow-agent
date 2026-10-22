from platform_core.application.control import ControlService
from playwright.sync_api import expect, sync_playwright


def test_browser_creates_and_revises_draft_without_persisting_token(
    authoring_server, database, owner
):
    actor, token = owner
    project = ControlService(database).create_project(actor, "studio-project", "Studio")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(authoring_server + "/")
        page.get_by_label("临时 Bearer Token").fill(token)
        page.get_by_role("button", name="连接并加载项目").click()
        page.locator(".project-picker select").select_option(project["project_id"])
        page.get_by_role("button", name="进入工作区").click()
        page.get_by_role("link", name="工作流", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("11 个算子", timeout=20_000)
        expect(
            page.locator("select").get_by_role("option", name="table-aggregate@1.0.0")
        ).to_be_attached()
        page.get_by_label("稳定键").fill("browser-draft")
        page.get_by_label("标题").fill("Browser draft")
        page.get_by_role("button", name="新建草稿").click()
        expect(page.get_by_role("status")).to_contain_text("revision 1", timeout=10_000)
        page.get_by_label("标题").fill("Browser draft revised")
        page.get_by_role("button", name="添加输入").click()
        page.get_by_label("算子").select_option("table-profile@1.0.0")
        page.get_by_role("button", name="添加算子").click()
        page.get_by_role("button", name="添加最终输出").click()
        page.get_by_role("button", name="保存修订").click()
        expect(page.get_by_role("status")).to_contain_text("revision 2", timeout=10_000)
        page.get_by_role("button", name="源码", exact=True).click()
        expect(page.get_by_text("冻结规范源码（只读）")).to_be_visible()

        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert token not in page.url
        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        browser.close()


def test_desktop_breakpoint_and_supported_widths_have_no_horizontal_overflow(
    authoring_server, database, owner
):
    actor, token = owner
    project = ControlService(database).create_project(actor, "layout-project", "Layout")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        narrow = browser.new_page(viewport={"width": 1099, "height": 800})
        narrow.goto(authoring_server + "/")
        expect(narrow.get_by_role("heading", name="请使用桌面浏览器")).to_be_visible()
        expect(narrow.get_by_label("临时 Bearer Token")).to_be_hidden()
        narrow.close()

        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(authoring_server + "/")
        page.get_by_label("临时 Bearer Token").fill(token)
        page.get_by_role("button", name="连接并加载项目").click()
        page.locator(".project-picker select").select_option(project["project_id"])
        page.get_by_role("button", name="进入工作区").click()
        expect(page.locator(".app-frame")).to_be_visible(timeout=20_000)
        for width in (1280, 1440, 1920):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            expect(page.locator(".desktop-warning")).to_be_hidden()
        browser.close()
