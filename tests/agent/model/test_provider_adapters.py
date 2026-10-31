import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from platform_agent_runtime.model_providers import (
    DeterministicModelProvider,
    OpenAIResponsesProvider,
    RecordReplayModelProvider,
    provider_from_secret,
)
from platform_contracts.agent_runtime import decision_proposal_json_schema

from tests.agent.model.conftest import model_profile


def call_record():
    return {
        "model_call_id": "call-http-1",
        "provider_revision": 0,
        "request_digest": "sha256:" + "1" * 64,
        "request": {"frame": {"frame_id": "frame-http-1"}},
    }


def proposal():
    return {
        "kind": "PLAN_REVISION",
        "frame_id": "frame-http-1",
        "expected_task_revision": 1,
        "reason": "fixed",
        "steps": [{"step_id": "inspect", "purpose": "inspect", "depends_on": []}],
    }


def test_openai_responses_adapter_uses_real_http_and_stable_idempotency_key():
    observed = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            observed["path"] = self.path
            observed["authorization"] = self.headers["Authorization"]
            observed["idempotency"] = self.headers["Idempotency-Key"]
            observed["request"] = json.loads(self.rfile.read(length))
            body = {
                "id": "resp_http_1",
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(proposal())}]}],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            }
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        selected = model_profile("OPENAI_RESPONSES")
        selected["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
        selected["max_reasoning_tokens"] = 0
        # The test calls the adapter directly; the catalogue digest belongs to
        # deployment validation rather than the transport operation.
        adapter = OpenAIResponsesProvider(selected, "top-secret")
        result = adapter.submit(call_record())
    finally:
        server.shutdown()
        thread.join()
    assert result.status == "COMPLETED"
    assert result.provider_ref == "resp_http_1"
    assert result.response == {"output": proposal()}
    assert result.usage["input_tokens"] == 11
    assert observed["path"] == "/v1/responses"
    assert observed["authorization"] == "Bearer top-secret"
    assert observed["idempotency"] == "call-http-1"
    assert observed["request"]["reasoning"] == {"effort": "none"}
    assert "strict" not in observed["request"]["text"]["format"]
    assert "top-secret" not in json.dumps(observed["request"])
    assert "top-secret" not in repr(result)


def test_record_replay_uses_request_digest_without_a_second_provider_call(tmp_path):
    fixed = {
        "status": "COMPLETED",
        "executed": True,
        "response": {"output": proposal()},
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cost_microunits": 1,
        },
    }
    delegate = DeterministicModelProvider([fixed])
    cassette = tmp_path / "model-cassette.json"
    assert RecordReplayModelProvider(cassette, delegate=delegate).submit(call_record()).status == (
        "COMPLETED"
    )
    replay = RecordReplayModelProvider(cassette).submit(call_record())
    assert replay.status == "COMPLETED"
    assert delegate.submit_count == 1


def test_production_provider_factory_accepts_composition_secret_without_persisting_it():
    selected = model_profile("OPENAI_RESPONSES")
    provider = provider_from_secret(selected, "composition-secret")
    assert provider.api_key == "composition-secret"
    assert "composition-secret" not in json.dumps(provider.profile)
    with pytest.raises(ValueError):
        provider_from_secret(selected, None)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (urllib.error.URLError("timeout"), "PROVIDER_UNAVAILABLE"),
        (
            urllib.error.HTTPError("http://provider", 503, "down", {}, None),
            "PROVIDER_UNAVAILABLE",
        ),
        (
            urllib.error.HTTPError("http://provider", 429, "limited", {}, None),
            "RATE_LIMITED",
        ),
    ],
)
def test_provider_transport_failures_remain_reconcilable(failure, expected_code):
    def fail(_request, timeout):
        assert timeout == 5
        raise failure

    selected = model_profile("OPENAI_RESPONSES")
    result = OpenAIResponsesProvider(selected, "secret", opener=fail).submit(call_record())
    assert result.status in {"UNKNOWN", "RETRYABLE"}
    assert result.error_code == expected_code
    assert result.executed in {None, False}


def test_provider_keepalives_cannot_extend_frozen_wall_timeout(monkeypatch):
    class KeepAliveResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read1(self, _size):
            return b"\n"

    selected = model_profile("OPENAI_RESPONSES")
    selected["timeout_seconds"] = 1
    ticks = iter((0.0, 0.5, 1.1))
    monkeypatch.setattr(
        "platform_agent_runtime.model_providers.time.monotonic", lambda: next(ticks)
    )
    result = OpenAIResponsesProvider(
        selected, "secret", opener=lambda *_args, **_kwargs: KeepAliveResponse()
    ).submit(call_record())
    assert result.status == "UNKNOWN"
    assert result.error_code == "PROVIDER_UNAVAILABLE"


def test_provider_ref_temporarily_not_visible_keeps_original_identity():
    def not_visible(_request, timeout):
        raise urllib.error.HTTPError("http://provider", 404, "missing", {}, None)

    call = call_record() | {"provider_ref": "resp_stable_1", "provider_revision": 3}
    selected = model_profile("OPENAI_RESPONSES")
    result = OpenAIResponsesProvider(selected, "secret", opener=not_visible).query(call)
    assert result.status == "UNKNOWN"
    assert result.provider_ref == "resp_stable_1"
    assert result.provider_revision == 4
    assert result.error_code == "PROVIDER_REF_NOT_VISIBLE"


def test_provider_schema_expands_recursive_json_values_for_strict_services():
    schema = decision_proposal_json_schema()
    assert schema["type"] == "object" and "anyOf" in schema and "oneOf" not in schema
    value = schema["$defs"]["JsonValue"]
    assert "anyOf" in value
    assert {item.get("type") for item in value["anyOf"]} == {
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
    }
    assert value["anyOf"][-1]["additionalProperties"] == {"$ref": "#/$defs/JsonValue"}
