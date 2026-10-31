from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    GovernedActivityService,
    InMemoryGovernanceLedger,
    NodeKind,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    RemotePluginDefinition,
    RemotePluginHost,
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "8" * 64
PLUGIN = PluginReference("remote.compute", "1", "sha256:" + "9" * 64)


class _RemotePluginServer(ThreadingHTTPServer):
    seen: list[tuple[str, dict[str, str], dict[str, object]]]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - HTTP verb is framework-defined
        body = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(body)
        self.server.seen.append((self.path, dict(self.headers), payload))  # type: ignore[attr-defined]
        action = payload["action"]
        if self.path == "/reconcile" and action["inputs"].get("known") is not True:
            response: object = None
        else:
            response = {
                "action_id": action["action_id"],
                "status": "SUCCEEDED",
                "output": {"echo": action["inputs"]},
                "error_code": None,
                "external_reference": "remote-task",
            }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _: str, *args: object) -> None:
        del args


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.CONTAINER,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("compute.run",),
        execution_mode=PluginExecutionMode.CONTAINER,
        timeout_seconds=5,
        max_attempts=2,
        credential_ref="runner_credential",
    )


def _invocation(inputs: dict[str, object]) -> WorkflowInvocation:
    manifest = _manifest()
    return WorkflowInvocation(
        "run-remote",
        GeneralWorkflowSpec(
            "remote-host",
            "1",
            RELEASE,
            (manifest,),
            (
                WorkflowNode(
                    "call", NodeKind.CONTAINER, {"plugin": PLUGIN.to_mapping(), "input": inputs}
                ),
            ),
        ),
        {},
        GovernanceFence("project", "task", "actor", 1, 1, RELEASE),
    )


def _server() -> tuple[_RemotePluginServer, Thread]:
    server = _RemotePluginServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_remote_host_uses_a_fixed_endpoint_and_stable_idempotency_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        server, thread = _server()
        try:
            credential = tmp_path / "credential"
            credential.write_text("runner-token\n", encoding="utf-8")
            origin = f"http://127.0.0.1:{server.server_port}"
            host = RemotePluginHost()
            host.register(
                RemotePluginDefinition(
                    _manifest(), origin + "/invoke", origin + "/reconcile", credential
                )
            )
            invocation = _invocation({"job": "terrain"})
            command = ActionCommand.create(invocation, "call", PLUGIN, {"job": "terrain"})
            ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
            service = GovernedActivityService(
                ledger,
                host,
                {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): _manifest()},
            )
            outcome = await service.execute(command)
            persisted = await ledger.action(command.action_id)

            assert outcome.status is OutcomeStatus.SUCCEEDED
            assert outcome.output == {"echo": {"job": "terrain"}}
            assert persisted is not None and persisted.reservation_state == "SETTLED"
            assert len(server.seen) == 1
            path, headers, payload = server.seen[0]
            assert path == "/invoke"
            assert headers["Idempotency-Key"] == command.idempotency_key
            assert headers["X-V7-Action-Id"] == command.action_id
            assert headers["Authorization"] == "Bearer runner-token"
            assert payload["operation"] == "invoke"
            assert "runner-token" not in json.dumps(persisted.to_mapping())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    asyncio.run(scenario())


def test_remote_host_reconciles_the_same_action_and_rejects_insecure_registration(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server, thread = _server()
        try:
            credential = tmp_path / "credential"
            credential.write_text("runner-token", encoding="utf-8")
            origin = f"http://127.0.0.1:{server.server_port}"
            host = RemotePluginHost()
            host.register(
                RemotePluginDefinition(
                    _manifest(), origin + "/invoke", origin + "/reconcile", credential
                )
            )
            invocation = _invocation({"known": True})
            command = ActionCommand.create(invocation, "call", PLUGIN, {"known": True})
            ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
            service = GovernedActivityService(
                ledger,
                host,
                {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): _manifest()},
            )
            assert (await ledger.authorize(command)).allowed
            assert (await ledger.mark_dispatched(command)).invoke
            outcome = await service.execute(command)
            assert outcome.status is OutcomeStatus.SUCCEEDED
            assert [item[0] for item in server.seen] == ["/reconcile"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    asyncio.run(scenario())
    with pytest.raises(WorkflowContractError, match="HTTPS"):
        RemotePluginDefinition(
            _manifest(), "http://remote.invalid/invoke", "https://remote.invalid/r"
        )
    with pytest.raises(WorkflowContractError, match="same registered origin"):
        RemotePluginDefinition(
            _manifest(),
            "https://remote.invalid/invoke",
            "https://other.invalid/reconcile",
        )
    with pytest.raises(WorkflowContractError, match="HTTPS"):
        RemotePluginDefinition(
            _manifest(),
            "https://remote.invalid/invoke?token=not-allowed",
            "https://remote.invalid/r",
        )
    with pytest.raises(WorkflowContractError, match="sensitive"):
        RemotePluginDefinition(
            _manifest(),
            "https://remote.invalid/invoke",
            "https://remote.invalid/r",
            tmp_path / "credential",
            headers={"Authorization": "x"},
        )


def test_remote_host_never_forwards_a_registered_credential_through_redirect(
    tmp_path: Path,
) -> None:
    class Capture(BaseHTTPRequestHandler):
        seen: list[dict[str, str]] = []

        def do_POST(self) -> None:  # noqa: N802 - HTTP verb is framework-defined
            type(self).seen.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _: str, *args: object) -> None:
            del args

    capture = ThreadingHTTPServer(("127.0.0.1", 0), Capture)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - HTTP verb is framework-defined
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{capture.server_port}/capture")
            self.end_headers()

        def log_message(self, _: str, *args: object) -> None:
            del args

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    capture_thread = Thread(target=capture.serve_forever, daemon=True)
    redirect_thread = Thread(target=redirect.serve_forever, daemon=True)
    capture_thread.start()
    redirect_thread.start()
    try:

        async def scenario() -> None:
            credential = tmp_path / "credential"
            credential.write_text("runner-token", encoding="utf-8")
            origin = f"http://127.0.0.1:{redirect.server_port}"
            host = RemotePluginHost()
            host.register(
                RemotePluginDefinition(
                    _manifest(), origin + "/invoke", origin + "/reconcile", credential
                )
            )
            invocation = _invocation({"job": "redirect"})
            command = ActionCommand.create(invocation, "call", PLUGIN, {"job": "redirect"})
            ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
            service = GovernedActivityService(
                ledger,
                host,
                {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): _manifest()},
            )
            assert (await service.execute(command)).status is OutcomeStatus.RECONCILING

        asyncio.run(scenario())
        assert Capture.seen == []
    finally:
        redirect.shutdown()
        capture.shutdown()
        redirect.server_close()
        capture.server_close()
        redirect_thread.join(timeout=2)
        capture_thread.join(timeout=2)
