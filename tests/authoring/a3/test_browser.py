import re

from platform_core.application.authoring_release import AuthoringReleaseService
from platform_core.application.catalog import build_catalog
from platform_core.application.control import ControlService
from platform_core.application.workflows import PackageProcessor
from playwright.sync_api import expect, sync_playwright

from tests.authoring.a3.test_candidate import Validator, frozen_definition
from tests.authoring.browser_helpers import connect_workspace


def test_browser_rebuilds_candidate_and_task_state_without_persisting_token(
    authoring_server, database, owner
):
    actor, token = owner
    values = frozen_definition(database, owner)
    _, project, definition, revision, validation, descriptor, environment = values
    service = AuthoringReleaseService(
        database,
        descriptor=descriptor,
        environment=environment,
        operator_catalog=build_catalog(),
    )
    candidate = service.create_candidate(
        actor,
        project,
        definition,
        "browser-candidate",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "validation_id": validation["validation_id"],
        },
    )
    assert PackageProcessor(
        database,
        Validator(),
        operator_catalog=build_catalog(),
        owner="browser-package",
    ).process_one()
    task = ControlService(database).create_task(
        actor,
        project,
        "browser-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "browser trial", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        connect_workspace(page, authoring_server, token, project, "releases")
        expect(page.get_by_role("status")).to_contain_text("个草稿", timeout=20_000)
        page.get_by_label("工作流定义").select_option(definition)
        expect(page.get_by_role("heading", name="候选")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"READY · r2"))).to_contain_text(
            candidate["candidate_id"], timeout=10_000
        )
        expect(page.get_by_label("已有 DIRECT Task")).to_contain_text(task["task_id"])
        expect(page.get_by_role("heading", name="真实试运行")).to_be_visible()
        expect(page.get_by_role("heading", name="不可变版本")).to_be_visible()
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert token not in page.url

        page.reload()
        expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        page.get_by_label("临时 Bearer Token").fill(token)
        page.get_by_role("button", name="连接并加载项目").click()
        expect(page.get_by_role("status")).to_contain_text("个草稿", timeout=20_000)
        page.get_by_label("工作流定义").select_option(definition)
        expect(page.get_by_role("button", name=re.compile(r"READY · r2"))).to_be_visible(
            timeout=10_000
        )
        browser.close()
