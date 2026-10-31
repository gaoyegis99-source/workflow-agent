"""Explicit dependency records for concrete execution components."""

from dataclasses import dataclass

from platform_contracts.runtime_context import ExecutionRuntimeConfig


@dataclass(slots=True)
class ActionServices:
    repository: object
    root: object
    docker: object
    data: object
    platform: object
    execution: object
    remote_factory: object
    table_inspector: object


class ActionComponent:
    def __init__(self, services: ActionServices):
        self.repo = services.repository
        self.root = services.root
        self.execution = services.execution
        self.docker = self.execution.guard("docker", services.docker)
        self.data = self.execution.guard("data", services.data)
        self.platform = self.execution.guard("platform", services.platform)
        self.remote_factory = services.remote_factory
        self.table_inspector = services.table_inspector


@dataclass(slots=True)
class WorkflowServices:
    repository: object
    docker: object
    data: object
    platform: object
    execution: object
    runtime_config: ExecutionRuntimeConfig


class WorkflowComponent:
    def __init__(self, services: WorkflowServices):
        self.repo = services.repository
        self.execution = services.execution
        self.docker = self.execution.guard("docker", services.docker)
        self.data = self.execution.guard("data", services.data)
        self.platform = self.execution.guard("platform", services.platform)
        self.runtime_config = services.runtime_config
        self.execution_url = services.runtime_config.workflow_engine_callback_url
