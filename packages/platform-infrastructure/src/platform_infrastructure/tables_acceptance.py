"""Controlled aggregate for the Acceptance table declarations."""

from .tables_acceptance_core import acceptances, completions, deliverables
from .tables_acceptance_evidence import (
    acceptance_evidence,
    check_envelopes,
    check_runs,
    completion_validity,
    coverage_items,
    coverage_plans,
    human_reviews,
    knowledge_lifecycle_cursors,
    permits,
)

__all__ = (
    "acceptance_evidence",
    "acceptances",
    "check_envelopes",
    "check_runs",
    "completion_validity",
    "completions",
    "coverage_items",
    "coverage_plans",
    "deliverables",
    "human_reviews",
    "knowledge_lifecycle_cursors",
    "permits",
)
