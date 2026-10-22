"""Compose the production P6 Agent worker without exposing model or service secrets."""

from __future__ import annotations

import argparse
import logging
import os
import time
from contextlib import ExitStack
from importlib.metadata import version
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_contracts.release import production_mode, production_release
from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.agent_waiting import AgentWaitReconciler
from platform_core.application.catalog import configured_catalog
from platform_core.application.checker_catalog import load_catalog as load_checker_catalog
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.embedding_catalog import load_embedding_catalog
from platform_core.application.model_catalog import load_catalog, selected_profile
from platform_core.application.read_tools import ReadToolGateway
from platform_core.application.tool_catalog import load_tool_catalog
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import ExecutionClient, Remote

from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.checkpoint import CheckpointDatabase
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import provider_from_environment
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
    operator_catalog=None,
    checker_catalog=None,
):
    validate(profile)
    operator_catalog = operator_catalog or configured_catalog()
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


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main():
    parser = argparse.ArgumentParser(description="Run the V7 ledger-owned Agent worker")
    parser.parse_args()
    database = Database(_required("V7_DATABASE_URL"))
    checkpoint = CheckpointDatabase(_required("V7_AGENT_CHECKPOINT_DATABASE_URL"))
    profile = load_deployment_profile(_required("V7_AGENT_RUNTIME_PROFILE"))
    embedding_digest = profile.get("embedding_catalog_digest")
    if embedding_digest is not None:
        embedding_catalog = load_embedding_catalog(_required("V7_EMBEDDING_CATALOG"))
        if embedding_digest != embedding_catalog["catalog_digest"]:
            raise RuntimeError("Embedding catalog differs from the Agent runtime profile")
    elif production_mode():
        raise RuntimeError("Production Agent runtime profile has no embedding gate binding")
    models = load_catalog(_required("V7_MODEL_CATALOG"))
    model_profile = selected_profile(models)
    tool_catalog = load_tool_catalog(_required("V7_AGENT_TOOL_CATALOG"))
    operator_catalog = configured_catalog()
    checker_path = os.environ.get("V7_CHECKER_CATALOG")
    checker_catalog = load_checker_catalog(checker_path) if checker_path else None
    if production_mode():
        if tool_catalog.get("catalog_id") not in {
            "p7-action-tools-v2",
            "p7-science-tools-v2",
        }:
            raise RuntimeError("Production Agent workers require a P7 v2 tool catalogue")
        production_release(
            operator_path=_required("V7_OPERATOR_CATALOG"),
            operator_catalog=operator_catalog,
            tool_path=_required("V7_AGENT_TOOL_CATALOG"),
            tool_catalog=tool_catalog,
            catalogs={
                "checker_catalog": (
                    _required("V7_CHECKER_CATALOG"),
                    checker_catalog,
                )
            },
            version=version("groundwater-agent-runtime"),
        )
    provider = provider_from_environment(model_profile)
    tracer = configure_telemetry()
    remotes = {
        "data": Remote(_required("V7_DATA_URL"), _required("V7_PLATFORM_DATA_TOKEN")),
        "knowledge": Remote(
            _required("V7_KNOWLEDGE_URL"), _required("V7_PLATFORM_KNOWLEDGE_TOKEN")
        ),
        "execution": ExecutionClient(
            _required("V7_EXECUTION_URL"), _required("V7_PLATFORM_EXECUTION_TOKEN")
        ),
    }
    database.check_schema()
    checkpoint.check_schema()
    touch_worker_heartbeat()
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
            os.environ.get("V7_AGENT_WORKER_ID", "agent-worker-" + uuid4().hex),
            tracer,
            operator_catalog,
            checker_catalog,
        )
        try:
            while True:
                try:
                    active = worker.process_one()
                except (OSError, StorageError):
                    logging.warning("Agent dependency unavailable category=transient")
                    active = False
                touch_worker_heartbeat()
                if not active:
                    time.sleep(0.5)
        finally:
            for remote in remotes.values():
                remote.close()
            database.close()
