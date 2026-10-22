"""P4 workflow envelopes; CWL content is validated asynchronously by the worker."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from platform_contracts.research import ResearchExecutionContext
from platform_contracts.v1 import (
    Contract,
    Identifier,
    OperatorTarget,
    PackageRef,
    PositiveRevision,
    WorkflowTarget,
)


class WorkflowPackageCreate(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    entrypoint: Identifier
    documents: Annotated[dict[str, dict[str, Any]], Field(min_length=1, max_length=100)]
    implementations: Annotated[dict[str, OperatorTarget], Field(min_length=1, max_length=32)]
    profile: Literal["v7-cwl-p4-v1"] = "v7-cwl-p4-v1"

    @model_validator(mode="after")
    def entry_exists(self):
        if self.entrypoint not in self.documents:
            raise ValueError("Entrypoint must be included in documents")
        return self


class WorkflowPackageReceipt(Contract):
    package_id: Identifier
    status: Literal["QUEUED", "VALIDATING", "READY", "REJECTED"]
    status_url: str


class WorkflowPackageView(WorkflowPackageReceipt):
    name: str
    profile: Literal["v7-cwl-p4-v1"]
    package_ref: PackageRef | None = None
    error: dict[str, Any] | None = None
    source: dict[str, Any]
    compiled: dict[str, Any] | None = None


class WorkflowPackagePage(Contract):
    items: list[WorkflowPackageView]
    next_cursor: str | None = None


class WorkflowExecutionCreate(Contract):
    task_id: Identifier
    expected_spec_revision: PositiveRevision
    target: WorkflowTarget
    inputs: dict[str, Any]
    max_node_executions: Annotated[int, Field(ge=1, le=1000)]
    research_context: ResearchExecutionContext | None = None


class WorkflowOutputSelection(Contract):
    execution_id: Identifier
    output: Identifier
    path: Annotated[list[str | int], Field(max_length=32)] = Field(default_factory=list)


class WorkflowStepView(Contract):
    node_id: Identifier
    execution_id: Identifier | None = None
    call_path: list[str]
    scatter_indices: list[int]
    state: str
    executed: bool
    attempt_id: Identifier | None = None
    engine_job_id: str | None = None


class WorkflowStepPage(Contract):
    items: list[WorkflowStepView]
    next_cursor: str | None = None
