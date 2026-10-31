import json
from contextlib import contextmanager

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.admin import export_run
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.telemetry import configure_telemetry
from platform_api.app import create_app
from platform_api.composition import PlatformComposition

from tests.agent.kernel.test_kernel import PlanDecision


def test_agent_sse_uses_authorization_and_resumable_cursor(model_agent_setup):
    headers = {"Authorization": "Bearer " + model_agent_setup["token"]}
    path = (
        f"/api/v1/projects/{model_agent_setup['project_id']}"
        f"/tasks/{model_agent_setup['task']['task_id']}/agent/events"
    )
    with TestClient(create_app(model_agent_setup["database"])) as client:
        assert client.get(path).status_code == 401
        response = client.get(path + "?after=-1", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.text.splitlines()
        cursor = int(next(value[4:] for value in lines if value.startswith("id: ")))
        payload = json.loads(next(value[6:] for value in lines if value.startswith("data: ")))
        assert payload["agent_run_id"] == model_agent_setup["agent_run_id"]
        assert payload["wake_revision"] == cursor

        heartbeat = client.get(path + f"?after={cursor}", headers=headers)
        body = json.loads(
            next(value[6:] for value in heartbeat.text.splitlines() if value.startswith("data: "))
        )
        assert body == {"heartbeat": True, "wake_revision": cursor}


def test_ui_is_unified_and_legacy_static_route_is_removed(tmp_path):
    studio = tmp_path / "studio"
    (studio / "assets").mkdir(parents=True)
    (studio / "index.html").write_text(
        '<!doctype html><html><body><div id="root">Groundwater V7</div></body></html>',
        encoding="utf-8",
    )
    with TestClient(
        create_app(composition=PlatformComposition.for_test(studio_root=studio))
    ) as client:
        page = client.get("/")
        legacy = client.get("/ui/app.js")
    assert "Groundwater V7" in page.text
    assert legacy.status_code == 404


def test_admin_export_is_read_only_and_contains_recovery_ledgers(model_agent_setup):
    value = export_run(model_agent_setup["database"], model_agent_setup["agent_run_id"])
    assert value["run"]["agent_run_id"] == model_agent_setup["agent_run_id"]
    assert set(value) == {
        "run",
        "plans",
        "events",
        "wakeups",
        "wait_dependencies",
        "runtime_recoveries",
        "delivery_attempts",
    }


def test_optional_telemetry_is_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("V7_AGENT_OTEL_ENDPOINT", raising=False)
    assert configure_telemetry() is not None


def test_agent_span_contains_only_identity_and_control_attributes(agent_setup):
    class Span:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, key, value):
            self.attributes[key] = value

    class Tracer:
        def __init__(self):
            self.span = Span()

        @contextmanager
        def start_as_current_span(self, name):
            assert name == "agent.process_one"
            yield self.span

    tracer = Tracer()
    runtime = LangGraphRuntime(PlanDecision(), InMemorySaver())
    assert KernelWorker(
        agent_setup["database"], runtime, "otel-worker", tracer=tracer
    ).process_one()
    assert set(tracer.span.attributes) == {
        "agent.run.id",
        "agent.runtime.epoch",
        "agent.worker.generation",
        "task.id",
        "agent.proposal.kind",
        "agent.proposal.id",
    }
    assert not any(
        fragment in key
        for key in tracer.span.attributes
        for fragment in ("token", "payload", "prompt", "content", "credential")
    )
