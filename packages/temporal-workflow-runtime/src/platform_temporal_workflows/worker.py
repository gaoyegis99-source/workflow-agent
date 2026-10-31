"""Fail-closed Temporal Worker bootstrap for the V7 general-workflow queue."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .activities import V7TemporalActivities
from .governance import GovernedActivityService
from .models import PluginManifest, WorkflowContractError
from .temporal_runtime import GeneralWorkflow, ScheduledInvocationWorkflow


@dataclass(frozen=True)
class WorkerSettings:
    address: str
    namespace: str
    task_queue: str
    plugin_manifests_path: Path
    governance_factory: str
    plugin_host_factory: str

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        path = os.environ.get("V7_TEMPORAL_PLUGIN_MANIFESTS")
        gateway = os.environ.get("V7_TEMPORAL_GOVERNANCE_FACTORY")
        host = os.environ.get("V7_TEMPORAL_PLUGIN_HOST_FACTORY")
        if not path or not gateway or not host:
            raise WorkflowContractError(
                "V7_TEMPORAL_PLUGIN_MANIFESTS, V7_TEMPORAL_GOVERNANCE_FACTORY, and "
                "V7_TEMPORAL_PLUGIN_HOST_FACTORY are required"
            )
        return cls(
            address=os.environ.get("V7_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            namespace=os.environ.get("V7_TEMPORAL_NAMESPACE", "default"),
            task_queue=os.environ.get("V7_TEMPORAL_TASK_QUEUE", "v7-general-workflows"),
            plugin_manifests_path=Path(path),
            governance_factory=gateway,
            plugin_host_factory=host,
        )


def load_manifests(path: Path) -> dict[tuple[str, str, str], PluginManifest]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowContractError("could not read V7 Temporal plugin manifest file") from error
    if not isinstance(raw, list):
        raise WorkflowContractError("V7 Temporal plugin manifests must be a JSON list")
    manifests = [PluginManifest.from_mapping(item) for item in raw]
    result = {
        (item.plugin.plugin_id, item.plugin.version, item.plugin.package_digest): item
        for item in manifests
    }
    if len(result) != len(manifests):
        raise WorkflowContractError("V7 Temporal plugin manifests have duplicate exact references")
    return result


def _factory(reference: str) -> Callable[[], object]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise WorkflowContractError("worker factory must use MODULE:CALLABLE")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise WorkflowContractError(f"could not load worker factory {reference}") from error
    if not callable(factory):
        raise WorkflowContractError("worker factory is not callable")
    return factory


async def run_worker(settings: WorkerSettings) -> None:
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except (
        ImportError
    ) as error:  # pragma: no cover - dependency check happens in packaging verification
        raise RuntimeError("temporalio is required to run the V7 Temporal worker") from error
    manifests = load_manifests(settings.plugin_manifests_path)
    ledger = _factory(settings.governance_factory)()
    plugins = _factory(settings.plugin_host_factory)()
    if not _has_methods(
        ledger,
        (
            "action",
            "authorize",
            "mark_dispatched",
            "record_outcome",
            "record_unknown",
            "workflow_event",
        ),
    ):
        raise WorkflowContractError("governance factory did not return a GovernanceLedger")
    if not _has_methods(plugins, ("invoke", "reconcile")):
        raise WorkflowContractError("plugin host factory did not return a PluginExecutionPort")
    service = GovernedActivityService(ledger, plugins, manifests)
    activities = V7TemporalActivities(service, ledger)
    client = await Client.connect(settings.address, namespace=settings.namespace)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[GeneralWorkflow, ScheduledInvocationWorkflow],
        activities=[activities.execute_governed_node, activities.record_workflow_event],
    )
    await worker.run()


def _has_methods(value: object, names: tuple[str, ...]) -> bool:
    return all(callable(getattr(value, name, None)) for name in names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v7-temporal-worker")
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = WorkerSettings.from_environment()
        manifests = load_manifests(settings.plugin_manifests_path)
        if args.validate_config:
            print(
                json.dumps(
                    {
                        "address": settings.address,
                        "namespace": settings.namespace,
                        "task_queue": settings.task_queue,
                        "plugin_count": len(manifests),
                    },
                    sort_keys=True,
                )
            )
            return 0
        asyncio.run(run_worker(settings))
        return 0
    except (OSError, RuntimeError, ValueError, WorkflowContractError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
