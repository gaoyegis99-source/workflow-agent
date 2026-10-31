"""Stable assembly surface for split Authoring Release application flows."""

from platform_core.application.authoring_candidates import AuthoringCandidateFlow
from platform_core.application.authoring_release_support import (
    AuthoringReleaseDependencies,
    semver_sort,
)
from platform_core.application.authoring_releases import AuthoringReleaseLifecycleFlow
from platform_core.application.authoring_trials import AuthoringTrialFlow
from platform_core.application.workflows import Workflows


class AuthoringReleaseService(
    AuthoringCandidateFlow,
    AuthoringTrialFlow,
    AuthoringReleaseLifecycleFlow,
):
    """Preserve the public service while assembling focused lifecycle flows."""

    def __init__(
        self,
        store,
        data=None,
        execution=None,
        *,
        descriptor=None,
        environment=None,
        operator_catalog=None,
    ):
        workflows = Workflows(
            store,
            data,
            execution,
            operator_catalog=operator_catalog,
        )
        super().__init__(
            AuthoringReleaseDependencies(
                store=store,
                data=data,
                execution=execution,
                descriptor=descriptor,
                environment=environment,
                operator_catalog=operator_catalog,
                workflows=workflows,
            )
        )


__all__ = ("AuthoringReleaseService", "semver_sort")
