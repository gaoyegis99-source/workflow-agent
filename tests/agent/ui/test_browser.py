import json
import os
import sys
import threading
import time

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_agent_runtime.profiles import profile
from platform_contracts import DeploymentContext
from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure.services import ExecutionClient, Remote
from playwright.sync_api import expect, sync_playwright

from tests.agent.control.test_agent_autonomy import decision
from tests.agent.kernel.test_kernel import QuestionDecision, run_once
from tests.authoring.browser_helpers import connect_task, create_workspace, upload_asset
from tests.platform.database.processes import Processes, eventually
from tests.platform.services.harness import free_port


def test_browser_restores_agent_state_and_submits_structured_answer(model_agent_setup, tmp_path):
    run_once(model_agent_setup, QuestionDecision())
    runtime_path = tmp_path / "runtime-profile.json"
    catalog_path = tmp_path / "model-catalog.json"
    runtime_path.write_text(json.dumps(profile()), encoding="utf-8")
    catalog_path.write_text(json.dumps(model_agent_setup["catalog"]), encoding="utf-8")

    database_url = model_agent_setup["database"].engine.url.render_as_string(hide_password=False)
    processes = Processes(tmp_path, database_url)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = processes.start(
        [sys.executable, "-m", "platform_api", "--port", str(port)],
        {
            "V7_AGENT_RUNTIME_PROFILE": os.fspath(runtime_path),
            "V7_MODEL_CATALOG": os.fspath(catalog_path),
        },
    )
    client = httpx.Client(base_url=base_url, trust_env=False)

    def ready():
        if process.poll() is not None:
            raise AssertionError("Platform API exited during browser test")
        try:
            return client.get("/api/v1/ready", timeout=1).status_code == 200
        except httpx.TransportError:
            return False

    eventually(ready, timeout=20)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            connect_task(
                page,
                base_url,
                model_agent_setup["token"],
                model_agent_setup["project_id"],
                model_agent_setup["task"]["task_id"],
            )
            page.get_by_role("tab", name="Agent").click()
            state = page.get_by_role("heading", name="Agent 状态").locator(
                "xpath=ancestor::section"
            )
            expect(state).to_contain_text("QUESTION", timeout=20_000)
            page.get_by_label("结构化回答（JSON）").fill('{"value":"metre"}')
            page.get_by_role("button", name="提交结构化回答").click()
            expect(state).to_contain_text("READY", timeout=20_000)
            expect(page.get_by_role("button", name="提交结构化回答")).to_have_count(0)
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            page.reload()
            expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
            browser.close()
    finally:
        client.close()
        processes.close()


def test_browser_creates_agent_task_and_observes_real_scientific_completion(
    p6_browser_cluster, owner, tmp_path
):
    cluster = p6_browser_cluster
    _, token = owner
    catalog = build_tool_catalog("actions")
    provider = DeterministicModelProvider(
        [
            {
                "status": "COMPLETED",
                "executed": True,
                "response_factory": decision,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "reasoning_tokens": 0,
                    "cost_microunits": 1,
                },
            }
            for _ in range(24)
        ]
    )
    driver = ModelDecisionDriver(cluster.database, provider, cluster.model_profile)
    data = Remote(
        cluster.env["V7_DATA_URL"],
        cluster.env["V7_PLATFORM_DATA_TOKEN"],
        deployment=DeploymentContext(),
    )
    execution = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"],
        cluster.env["V7_PLATFORM_EXECUTION_TOKEN"],
        deployment=DeploymentContext(),
    )
    worker = KernelWorker(
        cluster.database,
        LangGraphRuntime(driver, InMemorySaver()),
        "p68-browser-agent",
        model_decision=driver,
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
        action_tools=ActionToolGateway(
            cluster.database,
            catalog,
            PlatformActionTools(
                cluster.database,
                data=data,
                execution=execution,
                operator_catalog=cluster.operator_catalog,
                checker_catalog=cluster.checker_catalog,
            ),
        ),
        action_reconciler=AgentActionReconciler(cluster.database),
    )
    stop = threading.Event()
    failures = []

    def drive():
        while not stop.is_set():
            try:
                worker.process_one()
            except Exception as error:  # surfaced to the owning thread below
                failures.append(error)
                return
            time.sleep(0.05)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        project_id = create_workspace(
            page, cluster.env["V7_PLATFORM_URL"], token, "Browser Agent", "assets"
        )
        cluster.client.headers["Authorization"] = "Bearer " + token
        cluster.project = project_id
        cluster.p = "/api/v1/projects/" + project_id
        source = tmp_path / "agent-browser.csv"
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
            '{"timezone":"UTC","value_unit":"m","quantity":"water_level_elevation"}'
        )
        page.get_by_role("button", name="保存修订").click()

        page.get_by_role("link", name="任务", exact=True).click()
        page.get_by_role("button", name="创建任务").click()
        page.get_by_label("任务模板").select_option("TIMESERIES")
        page.get_by_label("研究目标").fill("Agent scientific completion")
        page.get_by_label("执行模式").select_option("AGENT")
        page.get_by_role("button", name="下一步").click()
        page.get_by_label("观测资产").select_option(index=1)
        expect(page.get_by_label("分组字段")).to_be_visible(timeout=20_000)
        page.get_by_label("分组值（JSON）").fill('["A", "absent"]')
        page.get_by_label("允许部分不可评估").select_option("yes")
        page.get_by_role("button", name="下一步").click()
        page.get_by_label("执行次数").fill("3")
        page.get_by_role("dialog", name="创建任务").get_by_role(
            "button", name="创建任务", exact=True
        ).click()
        expect(page.get_by_role("tab", name="范围")).to_be_visible(timeout=20_000)
        cluster.task = page.url.rstrip("/").split("/")[-1]
        page.get_by_role("tab", name="Agent").click()
        expect(
            page.get_by_role("heading", name="Agent 状态").locator("xpath=ancestor::section")
        ).to_contain_text("READY", timeout=20_000)
        page.get_by_role("tab", name="授权").click()
        page.get_by_role("button", name="创建授权").click()
        expect(page.locator(".toast")).to_contain_text("授权已创建", timeout=20_000)

        platform_workers = cluster.launch_platform_worker_lanes()
        execution_worker = cluster.launch("execution-worker")
        thread = threading.Thread(target=drive, name="p68-browser-agent", daemon=True)
        thread.start()
        try:

            def poll(path):
                try:
                    return cluster.request("GET", path)
                except httpx.TimeoutException:
                    # The browser, SSE stream, Agent kernel, and action workers all
                    # share this real test API.  A loaded full suite can delay one
                    # read beyond the client's per-request timeout; the outer
                    # eventually deadline remains the authoritative bound.
                    return None

            def completed():
                if failures:
                    raise AssertionError(repr(failures[0]))
                if execution_worker.poll() is not None:
                    raise AssertionError("Execution worker exited during Agent completion")
                if platform_workers.poll() is not None:
                    raise AssertionError("Platform worker lane exited during Agent completion")
                task = poll(cluster.p + f"/tasks/{cluster.task}")
                return task if task and task["lifecycle"] == "COMPLETED" else None

            try:
                # Docker can keep processing a create after the bounded CLI call
                # times out.  The deterministic name then reserves the external
                # effect until inspect can observe it; CREATE_INTENT recovery must
                # not submit a duplicate.  A loaded full suite has demonstrated
                # that this daemon-side reconciliation can outlive 180 seconds,
                # so keep the end-to-end gate bounded while covering that window.
                eventually(completed, timeout=300)
            except AssertionError as error:
                agent = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/agent")
                raise AssertionError({"cause": str(error), "agent": agent}) from error
            expect(page.locator(".metric-grid")).to_contain_text("COMPLETED", timeout=20_000)
            page.get_by_role("tab", name="Agent").click()
            recent_decisions = page.get_by_role("heading", name="近期决策").locator(
                "xpath=ancestor::section"
            )
            expect(recent_decisions).to_be_visible(timeout=20_000)

            # Task completion and the Agent query are refreshed independently.
            # First wait for the durable Agent projection, then wait for the
            # browser's SSE/refetch path to render it.  Under a loaded full
            # suite the worker can finish the Task before the explanation is
            # committed, so a fixed short UI-only timeout is a race.
            def explanation_committed():
                agent = poll(cluster.p + f"/tasks/{cluster.task}/agent")
                return (
                    agent
                    if agent
                    and any(
                        item.get("kind") == "EXPLANATION"
                        for item in agent.get("recent_decisions", [])
                    )
                    else None
                )

            eventually(explanation_committed, timeout=90)

            def explanation_visible():
                return "EXPLANATION" if "EXPLANATION" in recent_decisions.inner_text() else None

            try:
                eventually(explanation_visible, timeout=90)
            except AssertionError as error:
                agent = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/agent")
                raise AssertionError(
                    {
                        "cause": str(error),
                        "agent": agent,
                        "recent_decisions": recent_decisions.inner_text(),
                    }
                ) from error
            page.get_by_role("tab", name="验收").click()
            expect(page.get_by_role("heading", name="验收 PASS")).to_be_visible(timeout=20_000)
            page.get_by_role("tab", name="运行").click()
            expect(page.get_by_role("button", name="timeseries-completeness")).to_be_disabled()
            expect(page.get_by_role("button", name="statistics-linear-trend")).to_be_disabled()
            assert not failures
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            page.reload()
            expect(page.get_by_label("临时 Bearer Token")).to_have_value("")
        finally:
            stop.set()
            thread.join(timeout=5)
            browser.close()
            data.close()
            execution.close()
