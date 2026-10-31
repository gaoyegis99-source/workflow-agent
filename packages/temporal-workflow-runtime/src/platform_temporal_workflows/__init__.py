"""V7 Temporal orchestration contracts and governed Activity boundary.

Only deterministic value contracts are imported eagerly.  Temporal imports a
workflow's package while it builds its sandbox, so importing HTTP clients,
SQLite, subprocess hosts, or FastAPI here would make otherwise deterministic
Workflow code depend on activity-only implementation modules.  Operational
exports remain available from this public package through lazy resolution.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .models import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    NodeKind,
    NodeOutcome,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    TriggerKind,
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ActionRecord": (".governance", "ActionRecord"),
    "Authorization": (".governance", "Authorization"),
    "DispatchAdmission": (".governance", "DispatchAdmission"),
    "GovernanceError": (".governance", "GovernanceError"),
    "GovernanceLedger": (".governance", "GovernanceLedger"),
    "GovernedActivityService": (".governance", "GovernedActivityService"),
    "InMemoryGovernanceLedger": (".governance", "InMemoryGovernanceLedger"),
    "PluginExecutionPort": (".governance", "PluginExecutionPort"),
    "RegisteredPluginHost": (".governance", "RegisteredPluginHost"),
    "create_governance_router": (".governance_gateway", "create_governance_router"),
    "HttpGovernanceLedger": (".http_gateway", "HttpGovernanceLedger"),
    "http_ledger_from_environment": (".http_gateway", "http_ledger_from_environment"),
    "SQLiteGovernanceLedger": (".sqlite_ledger", "SQLiteGovernanceLedger"),
    "ProcessPluginDefinition": (".process_host", "ProcessPluginDefinition"),
    "ProcessPluginError": (".process_host", "ProcessPluginError"),
    "ProcessPluginHost": (".process_host", "ProcessPluginHost"),
    "RemotePluginDefinition": (".remote_host", "RemotePluginDefinition"),
    "RemotePluginError": (".remote_host", "RemotePluginError"),
    "RemotePluginHost": (".remote_host", "RemotePluginHost"),
    "TemporalTriggerClient": (".client", "TemporalTriggerClient"),
    "TemporalDispatchCommand": (".dispatch", "TemporalDispatchCommand"),
    "TemporalDispatchKind": (".dispatch", "TemporalDispatchKind"),
    "TemporalOutboxDispatcher": (".dispatch", "TemporalOutboxDispatcher"),
    "OutboxLease": (".development", "OutboxLease"),
    "PublishedWorkflow": (".development", "PublishedWorkflow"),
    "SQLiteDevelopmentWorkflowPlatform": (
        ".development",
        "SQLiteDevelopmentWorkflowPlatform",
    ),
    "VerifiedWebhook": (".development", "VerifiedWebhook"),
    "WebhookVerifier": (".development", "WebhookVerifier"),
    "PlatformTriggerService": (".trigger_gateway", "PlatformTriggerService"),
    "TriggerAuthorizationError": (".trigger_gateway", "TriggerAuthorizationError"),
    "TriggerConflictError": (".trigger_gateway", "TriggerConflictError"),
    "TriggerGatewayError": (".trigger_gateway", "TriggerGatewayError"),
    "TriggerNotFoundError": (".trigger_gateway", "TriggerNotFoundError"),
    "TriggerUnavailableError": (".trigger_gateway", "TriggerUnavailableError"),
    "create_trigger_router": (".trigger_gateway", "create_trigger_router"),
    "V6ImportResult": (".v6_import", "V6ImportResult"),
    "V6ImportTarget": (".v6_import", "V6ImportTarget"),
    "import_v6_bundle": (".v6_import", "import_v6_bundle"),
}


def __getattr__(name: str) -> Any:
    """Load activity-side helpers only when an application asks for them."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = (
    "ActionCommand",
    "ActionRecord",
    "Authorization",
    "DispatchAdmission",
    "create_governance_router",
    "create_trigger_router",
    "GeneralWorkflowSpec",
    "GovernanceError",
    "GovernanceFence",
    "GovernanceLedger",
    "GovernedActivityService",
    "HttpGovernanceLedger",
    "InMemoryGovernanceLedger",
    "NodeKind",
    "NodeOutcome",
    "OutboxLease",
    "OutcomeStatus",
    "PluginExecutionMode",
    "PluginExecutionPort",
    "PluginKind",
    "PluginManifest",
    "PluginReference",
    "PlatformTriggerService",
    "PublishedWorkflow",
    "ProcessPluginDefinition",
    "ProcessPluginError",
    "ProcessPluginHost",
    "RemotePluginDefinition",
    "RemotePluginError",
    "RemotePluginHost",
    "RegisteredPluginHost",
    "SQLiteGovernanceLedger",
    "SQLiteDevelopmentWorkflowPlatform",
    "TemporalTriggerClient",
    "TemporalDispatchCommand",
    "TemporalDispatchKind",
    "TemporalOutboxDispatcher",
    "TriggerKind",
    "TriggerAuthorizationError",
    "TriggerConflictError",
    "TriggerGatewayError",
    "TriggerNotFoundError",
    "TriggerUnavailableError",
    "WorkflowContractError",
    "WorkflowInvocation",
    "WorkflowNode",
    "V6ImportResult",
    "V6ImportTarget",
    "VerifiedWebhook",
    "WebhookVerifier",
    "http_ledger_from_environment",
    "import_v6_bundle",
)
