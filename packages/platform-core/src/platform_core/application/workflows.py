"""Stable assembly surface for split workflow application services."""

from platform_core.application.workflow_nodes import WorkflowNodeFlow
from platform_core.application.workflow_packages import WorkflowPackageAdmissionFlow, package_view
from platform_core.application.workflow_processor import PackageProcessor, PackageValidator
from platform_core.application.workflow_submission import WorkflowSubmissionFlow
from platform_core.application.workflow_support import WorkflowDependencies


class Workflows(
    WorkflowPackageAdmissionFlow,
    WorkflowSubmissionFlow,
    WorkflowNodeFlow,
):
    """Keep the existing public API while composing focused workflow flows."""

    def __init__(self, store, data=None, execution=None, *, operator_catalog):
        super().__init__(
            WorkflowDependencies(
                store=store,
                data=data,
                execution=execution,
                operator_catalog=operator_catalog,
            )
        )


__all__ = ("PackageProcessor", "PackageValidator", "Workflows", "package_view")
