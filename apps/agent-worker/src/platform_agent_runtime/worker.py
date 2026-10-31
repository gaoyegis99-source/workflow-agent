"""Compose the production P6 Agent worker without exposing model or service secrets."""

from __future__ import annotations

import argparse
import logging
import time
from contextlib import ExitStack
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.agent_waiting import AgentWaitReconciler
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.embedding_catalog import load_embedding_catalog
from platform_core.application.read_tools import ReadToolGateway
from platform_core.application.tool_catalog import load_tool_catalog
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import ExecutionClient, Remote

from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.checkpoint import CheckpointDatabase
from platform_agent_runtime.composition import AgentWorkerComposition
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import provider_from_secret
from platform_agent_runtime.profiles import load_deployment_profile, validate
from platform_agent_runtime.read_tools import PlatformReadTools
from platform_agent_runtime.telemetry import configure_telemetry


def compose_worker(
    database,
    saver,
    profile,
    model_profile,
    provider,
    tool_catalog,
    remotes,
    owner,
    tracer=None,
    *,
    operator_catalog,
    checker_catalog,
):
    validate(profile)
    driver = ModelDecisionDriver(database, provider, model_profile)
    runtime = LangGraphRuntime(driver, saver)
    compiler = ContextCompiler(
        tool_catalog,
        max_context_tokens=model_profile["max_context_tokens"],
    )
    backend = PlatformReadTools(
        database,
        data=remotes["data"],
        knowledge=remotes["knowledge"],
        execution=remotes["execution"],
        operator_catalog=operator_catalog,
        checkers=checker_catalog,
    )
    gateway = ReadToolGateway(database, tool_catalog, backend)
    action_gateway = ActionToolGateway(
        database,
        tool_catalog,
        PlatformActionTools(
            database,
            data=remotes["data"],
            execution=remotes["execution"],
            knowledge=remotes["knowledge"],
            operator_catalog=operator_catalog,
            checker_catalog=checker_catalog,
        ),
    )
    return KernelWorker(
        database,
        runtime,
        owner,
        model_decision=driver,
        context_compiler=compiler,
        read_tools=gateway,
        action_tools=action_gateway,
        action_reconciler=AgentActionReconciler(database),
        wait_reconciler=AgentWaitReconciler(database),
        tracer=tracer,
        catalog_binding={
            "tool_catalog_id": tool_catalog["catalog_id"],
            "tool_catalog_digest": tool_catalog["catalog_digest"],
            "operator_catalog_digest": operator_catalog["catalog_digest"],
        },
    )


def main():
    parser = argparse.ArgumentParser(description="Run the V7 ledger-owned Agent worker")
    parser.parse_args()
    composition = AgentWorkerComposition.from_environment()
    database = Database(composition.database_url)
    checkpoint = CheckpointDatabase(composition.checkpoint_database_url)
    profile = load_deployment_profile(composition.runtime_profile_path)
    embedding_digest = profile.get("embedding_catalog_digest")
    if embedding_digest is not None:
        if composition.embedding_catalog_path is None:
            raise RuntimeError("V7_EMBEDDING_CATALOG is required")
        embedding_catalog = load_embedding_catalog(composition.embedding_catalog_path)
        if embedding_digest != embedding_catalog["catalog_digest"]:
            raise RuntimeError("Embedding catalog differs from the Agent runtime profile")
    elif composition.deployment.is_direct:
        raise RuntimeError("Direct Agent runtime profile has no embedding catalog binding")
    model_profile = composition.model_profile
    tool_catalog = load_tool_catalog(composition.tool_catalog_path)
    operator_catalog = composition.operator_catalog
    checker_catalog = composition.checker_catalog
    if composition.deployment.is_direct:
        if tool_catalog.get("catalog_id") not in {
            "p7-action-tools-v2",
            "p7-science-tools-v2",
            "p6-evidence-tools-v1",
        }:
            raise RuntimeError("Direct Agent workers require a P7 v2 tool catalogue")
    provider = provider_from_secret(model_profile, composition.model_provider_secret)
    tracer = configure_telemetry(composition.telemetry_endpoint)
    remotes = {
        "data": Remote(
            composition.data.url,
            composition.data.token,
            deployment=composition.deployment,
        ),
        "knowledge": Remote(
            composition.knowledge.url,
            composition.knowledge.token,
            deployment=composition.deployment,
        ),
        "execution": ExecutionClient(
            composition.execution.url,
            composition.execution.token,
            deployment=composition.deployment,
        ),
    }
    database.check_schema()
    checkpoint.check_schema()
    touch_worker_heartbeat(composition.heartbeat_file)
    with ExitStack() as stack:
        saver = stack.enter_context(checkpoint.saver())
        worker = compose_worker(
            database,
            saver,
            profile,
            model_profile,
            provider,
            tool_catalog,
            remotes,
            composition.worker_id or "agent-worker-" + uuid4().hex,
            tracer,
            operator_catalog=operator_catalog,
            checker_catalog=checker_catalog,
        )
        try:
            while True:
                try:
                    active = worker.process_one()
                except (OSError, StorageError):
                    logging.warning("Agent dependency unavailable category=transient")
                    active = False
                touch_worker_heartbeat(composition.heartbeat_file)
                if not active:
                    time.sleep(0.5)
        finally:
            for remote in remotes.values():
                remote.close()
            database.close()
