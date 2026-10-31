from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    InMemoryGovernanceLedger,
    NodeKind,
    NodeOutcome,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    WorkflowInvocation,
    WorkflowNode,
    create_governance_router,
)

RELEASE = "sha256:" + "7" * 64
PLUGIN = PluginReference("http.gateway", "1", "sha256:" + "8" * 64)


def _command() -> ActionCommand:
    manifest = PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("http.invoke",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=2,
    )
    workflow = GeneralWorkflowSpec(
        workflow_id="gateway",
        version="1",
        release_digest=RELEASE,
        plugins=(manifest,),
        steps=(
            WorkflowNode(
                "call",
                NodeKind.HTTP,
                {"plugin": PLUGIN.to_mapping(), "input": {"message": "hello"}},
            ),
        ),
    )
    invocation = WorkflowInvocation(
        "run-gateway",
        workflow,
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
    )
    return ActionCommand.create(invocation, "call", PLUGIN, {"message": "hello"})


def test_private_router_checks_service_identity_and_preserves_action_identity() -> None:
    ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
    app = FastAPI()
    app.include_router(create_governance_router(ledger, "private-token"))
    command = _command()
    headers = {"Authorization": "Bearer private-token"}

    with TestClient(app) as client:
        assert (
            client.post(
                "/internal/temporal/v1/actions/authorize", json=command.to_mapping()
            ).status_code
            == 401
        )
        authorization = client.post(
            "/internal/temporal/v1/actions/authorize",
            headers=headers,
            json=command.to_mapping(),
        )
        assert authorization.status_code == 200
        assert authorization.json() == {"allowed": True, "reason": None}

        dispatched = client.post(
            "/internal/temporal/v1/actions/dispatch",
            headers=headers,
            json=command.to_mapping(),
        )
        assert dispatched.status_code == 200
        assert dispatched.json()["command"]["action_id"] == command.action_id
        assert dispatched.json()["invoke"] is True

        outcome = NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={"ok": True})
        recorded = client.post(
            "/internal/temporal/v1/actions/outcome",
            headers=headers,
            json={"command": command.to_mapping(), "outcome": outcome.to_mapping()},
        )
        assert recorded.status_code == 200
        assert recorded.json()["reservation_state"] == "SETTLED"

        fetched = client.get(f"/internal/temporal/v1/actions/{command.action_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["outcome"] == outcome.to_mapping()
