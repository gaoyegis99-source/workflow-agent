"""Thin workflow execution coordinator."""

from pathlib import Path

from platform_contracts.runtime_context import (
    ContainerIdentity,
    DeploymentContext,
    ExecutionRuntimeConfig,
)
from platform_contracts.service import ensure

from platform_execution.application.components import (
    WorkflowArtifactPublisher,
    WorkflowExecutionDriver,
    WorkflowInputMaterializer,
    WorkflowLifecycle,
    WorkflowOutputCollector,
    WorkflowServices,
)
from platform_execution.application.execution_policies import (
    WorkflowPermitPolicy,
    WorkflowTracePolicy,
)
from platform_execution.application.execution_runtime import ExecutionRuntime
from platform_execution.application.execution_state import WORKFLOW_EXECUTION_STATE


class WorkflowRunner:
    def __init__(self, repository, docker, data, platform, execution_url, *, runtime_config=None):
        self.repo, self.docker = repository, docker
        self.data, self.platform = data, platform
        ensure(
            execution_url.startswith(("http://", "https://")),
            "INVALID_BINDING",
            "Workflow callback URL is required",
        )
        self.execution_url = execution_url.rstrip("/")
        # The optional construction is retained for unit test adapters only;
        # it never reads process environment.  Executable composition roots
        # inject the complete deployment runtime config.
        self.runtime_config = runtime_config or ExecutionRuntimeConfig(
            work_root=Path("/tmp/v7-workflow-test"),
            workflow_engine_callback_url=self.execution_url,
            park_manifest_path=None,
            container_identity=ContainerIdentity(),
            deployment=DeploymentContext(),
        )
        if self.runtime_config.workflow_engine_callback_url != self.execution_url:
            raise ValueError("EXECUTION_CALLBACK_CONFIGURATION_MISMATCH")
        self.execution = ExecutionRuntime(
            repository,
            platform,
            trace_policy=WorkflowTracePolicy(),
            permit_policy=WorkflowPermitPolicy(),
            changed_message="Frozen workflow file changed",
            permit_source="PLATFORM_CLIENT",
            state_machine=WORKFLOW_EXECUTION_STATE,
        )
        services = WorkflowServices(
            repository=repository,
            docker=docker,
            data=data,
            platform=platform,
            execution=self.execution,
            runtime_config=self.runtime_config,
        )
        self.input_materializer = WorkflowInputMaterializer(services)
        self.output_collector = WorkflowOutputCollector(services)
        self.lifecycle = WorkflowLifecycle(services)
        self.artifact_publisher = WorkflowArtifactPublisher(services, self.lifecycle)
        self.driver = WorkflowExecutionDriver(
            services,
            input_materializer=self.input_materializer,
            output_collector=self.output_collector,
            artifact_publisher=self.artifact_publisher,
            lifecycle=self.lifecycle,
        )

    def bind_lease(self, guard) -> None:
        self.execution.bind_lease(guard)

    def step(self, row, root):
        return self.driver.run(row, root)
