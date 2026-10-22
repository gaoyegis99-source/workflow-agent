from playwright.sync_api import expect, sync_playwright

from tests.authoring.a6.test_release_execution import execution_request, published_release
from tests.authoring.browser_helpers import connect_workspace


def test_browser_lists_exact_releases_separately_from_scientific_verification(
    authoring_server, database, owner
):
    actor, token = owner
    _, project, _, _, release = published_release(database, owner)
    task, _, _ = execution_request(database, actor, project, release)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, authoring_server, token, project, "releases")

        expect(page.get_by_role("status")).to_contain_text("个正式版本", timeout=20_000)
        library = page.get_by_role("heading", name="项目工作流版本库").locator("..")
        expect(library).to_contain_text("A3 · 1.0.0")
        expect(library).to_contain_text("SAMPLE_VERIFIED")
        expect(page.get_by_text("科学 VERIFIED", exact=False).first).to_be_visible()
        expect(page.get_by_label("正式执行 DIRECT Task")).to_contain_text(task["task_id"])
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert token not in page.url
        browser.close()
