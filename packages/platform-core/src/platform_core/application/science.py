"""Stable assembly surface for the split Science application service."""

from platform_core.application.science_acceptance import ScienceAcceptanceFlow
from platform_core.application.science_execution import ScienceExecutionFlow
from platform_core.application.science_support import ScienceDependencies


class Science(ScienceExecutionFlow, ScienceAcceptanceFlow):
    """Compose execution/publication and candidate/acceptance flows.

    The public class and constructor remain stable for HTTP adapters and tests;
    all business algorithms live in the responsibility-specific flow modules.
    """

    def __init__(self, store, data, execution, *, operator_catalog, checker_catalog=None):
        super().__init__(
            ScienceDependencies(
                store=store,
                data=data,
                execution=execution,
                operator_catalog=operator_catalog,
                checker_catalog=checker_catalog,
            )
        )


__all__ = ("Science",)
