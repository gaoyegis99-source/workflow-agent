"""Stable public composition façade for P2 Control operations."""

from __future__ import annotations

from platform_core.application.control_admission import ControlAdmission
from platform_core.application.control_commands import ControlCommands
from platform_core.application.control_contracts import (
    compatible_control_request,
    digest,
    require_current_acceptance_contract,
)
from platform_core.application.control_grants import ControlGrants
from platform_core.application.control_queries import ControlQueries
from platform_core.application.control_views import grant_view, project_view, task_view
from platform_core.application.ports import Record, Store
from platform_core.application.research_context import ResearchContextPort, StoreResearchContext


class ControlService(ControlQueries, ControlCommands, ControlGrants, ControlAdmission):
    """Assemble stable public Control operations over one Store transaction boundary."""

    def __init__(
        self,
        store: Store,
        *,
        agent_runtime_profile: Record | None = None,
        agent_model_catalog: Record | None = None,
        agent_tool_catalog: Record | None = None,
        operator_catalog: Record | None = None,
        research_context: ResearchContextPort | None = None,
    ):
        self.store = store
        self.agent_runtime_profile = agent_runtime_profile
        self.agent_model_catalog = agent_model_catalog
        self.agent_tool_catalog = agent_tool_catalog
        self.operator_catalog = operator_catalog
        self.research_context = research_context or StoreResearchContext()


__all__ = (
    "ControlService",
    "compatible_control_request",
    "digest",
    "grant_view",
    "project_view",
    "require_current_acceptance_contract",
    "task_view",
)
