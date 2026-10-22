from playwright.sync_api import expect, sync_playwright

from tests.authoring.a6.test_release_execution import published_release
from tests.authoring.browser_helpers import connect_workspace


def test_owner_can_disable_and_reenable_release_from_studio(authoring_server, database, owner):
    _, token = owner
    _, project, _, _, _ = published_release(database, owner)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, authoring_server, token, project, "releases")
        expect(page.get_by_role("status")).to_contain_text("个正式版本", timeout=20_000)

        page.get_by_role("button", name="A3 · 1.0.0", exact=False).click()
        page.get_by_role("button", name="停用新执行").click()
        page.get_by_label("原因").fill("A7 browser disable")
        page.get_by_role("button", name="确认操作").click()
        expect(page.get_by_role("status")).to_contain_text("DISABLED", timeout=20_000)
        library = page.get_by_role("heading", name="项目工作流版本库").locator("..")
        expect(library).to_contain_text("DISABLED · 生命周期 r2")

        page.get_by_role("button", name="恢复新执行").click()
        page.get_by_label("原因").fill("A7 browser re-enable")
        page.get_by_role("button", name="确认操作").click()
        expect(page.get_by_role("status")).to_contain_text("ACTIVE", timeout=20_000)
        expect(library).to_contain_text("ACTIVE · 生命周期 r3")
        page.get_by_text("生命周期历史", exact=True).click()
        expect(page.get_by_text("A7 browser disable", exact=False)).to_be_visible()
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert token not in page.url
        browser.close()
