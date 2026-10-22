"""Export the implemented DTO schemas without inventing business HTTP endpoints."""

import json

from pydantic import TypeAdapter

from platform_contracts import CONTRACT_VERSION
from platform_contracts.agent_runtime import AgentFrame, AgentRuntimeProfile, DecisionProposal
from platform_contracts.control import (
    BudgetLimits,
    BudgetView,
    ControlRequest,
    GrantCreate,
    GrantPage,
    GrantView,
    ProjectCreate,
    ProjectPage,
    ProjectView,
    Readiness,
    ReviseTask,
    TaskCreate,
    TaskPage,
    TaskSpec,
    TaskView,
)
from platform_contracts.knowledge import (
    EvidenceRead,
    EvidenceRef,
    KnowledgeImportCreate,
    KnowledgeImportReceipt,
    KnowledgeSearch,
    KnowledgeSearchResult,
    KnowledgeVersionView,
    KnowledgeWithdraw,
)
from platform_contracts.research import (
    AnalysisRegistrationView,
    AnalysisRevisionCreate,
    AnalysisRevisionView,
    ClaimRevisionCreate,
    ClaimRevisionView,
    PolicyDecisionView,
    RegisterAnalysis,
    ResearchExecutionContext,
    ResearchView,
)
from platform_contracts.science import (
    AcceptanceCreate,
    AcceptanceReceipt,
    AcceptanceSpec,
    AnalysisParameters,
    DeliverableCreate,
    ExecutionCreate,
    InputSelection,
    SemanticRevision,
    Semantics,
    UploadCreate,
    UploadFinalize,
    UploadReceipt,
)
from platform_contracts.v1 import (
    AssetRef,
    CommandReceipt,
    ComputationTarget,
    ErrorEnvelope,
    HealthResponse,
    TaskRef,
)

CONTRACTS = {
    "TaskRef": TaskRef,
    "AssetRef": AssetRef,
    "ComputationTarget": ComputationTarget,
    "CommandReceipt": CommandReceipt,
    "ErrorEnvelope": ErrorEnvelope,
    "HealthResponse": HealthResponse,
    "AgentFrame": AgentFrame,
    "DecisionProposal": DecisionProposal,
    "AgentRuntimeProfile": AgentRuntimeProfile,
}
CONTRACTS.update(
    {
        model.__name__: model
        for model in (
            ProjectCreate,
            ProjectView,
            ProjectPage,
            TaskSpec,
            TaskCreate,
            TaskView,
            TaskPage,
            ControlRequest,
            ReviseTask,
            GrantCreate,
            GrantView,
            GrantPage,
            BudgetLimits,
            BudgetView,
            Readiness,
            AcceptanceCreate,
            AcceptanceReceipt,
            AcceptanceSpec,
            AnalysisParameters,
            DeliverableCreate,
            ExecutionCreate,
            InputSelection,
            SemanticRevision,
            Semantics,
            UploadCreate,
            UploadFinalize,
            UploadReceipt,
            KnowledgeImportCreate,
            KnowledgeImportReceipt,
            KnowledgeSearch,
            KnowledgeSearchResult,
            KnowledgeVersionView,
            KnowledgeWithdraw,
            EvidenceRef,
            EvidenceRead,
            AnalysisRevisionCreate,
            AnalysisRevisionView,
            AnalysisRegistrationView,
            RegisterAnalysis,
            ClaimRevisionCreate,
            ClaimRevisionView,
            PolicyDecisionView,
            ResearchExecutionContext,
            ResearchView,
        )
    }
)


def export_schemas() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "schemas": {name: TypeAdapter(model).json_schema() for name, model in CONTRACTS.items()},
    }


if __name__ == "__main__":
    print(json.dumps(export_schemas(), ensure_ascii=False, indent=2))
