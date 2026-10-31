"""Trial-result observation for immutable workflow releases.

This worker owns only trial verdict observation.  Candidate and release editing
remain in :mod:`authoring_release`; keeping the worker here prevents the HTTP
authoring use case from carrying remote execution/publication polling logic.
"""

from __future__ import annotations

from datetime import timedelta

from platform_core.application.trial_coverage import at_path, coverage, matches_type
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import RuleViolation, require

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}


class TrialVerdictProcessor:
    """Observe immutable execution/publication facts without scheduling new work."""

    def __init__(self, store, data, execution, *, owner, lease_seconds=30):
        self.store = store
        self.data = data
        self.execution = execution
        self.owner = owner
        self.lease_seconds = lease_seconds

    def _finish(self, claim, verdict, result=None, category=None, retry=None):
        with self.store.transaction() as uow:
            changes = {
                "case_verdict": verdict,
                "case_result": result,
                "case_result_digest": canonical_digest(result) if result is not None else None,
                "error_category": category,
                "next_retry_at": retry,
                "finished_at": uow.now() if verdict in {"PASS", "FAIL"} or retry is None else None,
            }
            return uow.authoring.finish_trial(
                claim["trial_id"], self.owner, claim["generation"], **changes
            )

    def _retry(self, claim, category):
        with self.store.transaction() as uow:
            retry = uow.now() + timedelta(seconds=min(30, 2 ** min(claim["attempt"], 5)))
        return self._finish(claim, "ERROR", category=category, retry=retry)

    def process_one(self) -> bool:
        with self.store.transaction() as uow:
            claim = uow.authoring.claim_trial(self.owner, self.lease_seconds)
            if claim is None:
                return False
            action = uow.actions.get(claim["action_id"])
            nodes = uow.workflows.nodes(action["action_id"])
        execution_id = action.get("execution_id")
        if execution_id is None:
            with self.store.transaction() as uow:
                retry = uow.now() + timedelta(seconds=1)
            self._finish(claim, "PENDING", retry=retry)
            return True
        try:
            execution = self.execution.json(
                "GET", f"/internal/projects/{claim['project_id']}/executions/{execution_id}"
            )
            require(
                execution.get("execution_id") == execution_id
                and execution.get("action_id") == claim["action_id"],
                "INVALID_BINDING",
                "Trial execution identity changed",
            )
            if execution["state"] not in _TERMINAL:
                with self.store.transaction() as uow:
                    retry = uow.now() + timedelta(seconds=1)
                self._finish(claim, "PENDING", retry=retry)
                return True
            if execution["state"] != "SUCCEEDED":
                result = {
                    "profile": "workflow-trial-result-v1",
                    "trial_case_digest": claim["trial_case_digest"],
                    "action_id": claim["action_id"],
                    "execution_id": execution_id,
                    "execution_state": execution["state"],
                    "publication_state": "NOT_COMMITTED",
                    "started_steps": sum(bool(item["executed"]) for item in nodes),
                    "assertions": [],
                    "reasons": ["EXECUTION_" + execution["state"]],
                    "verdict": "FAIL",
                }
                self._finish(claim, "FAIL", result=result)
                return True
            publication = self.data.json(
                "GET", f"/internal/projects/{claim['project_id']}/publications/{execution_id}"
            )
            require(
                publication.get("execution_id", execution_id) == execution_id,
                "INVALID_BINDING",
                "Trial publication identity changed",
            )
            if publication.get("state") != "COMMITTED":
                if publication.get("state") == "ABORTED":
                    result = {
                        "profile": "workflow-trial-result-v1",
                        "trial_case_digest": claim["trial_case_digest"],
                        "action_id": claim["action_id"],
                        "execution_id": execution_id,
                        "execution_state": "SUCCEEDED",
                        "publication_state": "ABORTED",
                        "started_steps": sum(bool(item["executed"]) for item in nodes),
                        "assertions": [],
                        "reasons": ["PUBLICATION_ABORTED"],
                        "verdict": "FAIL",
                    }
                    self._finish(claim, "FAIL", result=result)
                    return True
                with self.store.transaction() as uow:
                    retry = uow.now() + timedelta(seconds=1)
                self._finish(claim, "PENDING", retry=retry)
                return True
            required_steps = {
                item["node_id"]: item for item in claim["trial_case"]["required_steps"]
            }
            required = set(required_steps)
            actual = {item["node_id"]: item for item in nodes}
            step_failures = [
                node_id
                for node_id in sorted(required)
                if node_id not in actual
                or not actual[node_id]["executed"]
                or (actual[node_id]["observation"] or {}).get("state") != "SUCCEEDED"
                or actual[node_id]["request"].get("call_path")
                != required_steps[node_id]["call_path"]
                or actual[node_id]["request"].get("scatter_indices")
                != required_steps[node_id]["scatter_indices"]
            ]
            outputs = publication.get("receipt", publication).get("outputs")
            outputs = publication.get("outputs", outputs)
            required_output_failures = [
                item["output"]
                for item in claim["trial_case"]["required_outputs"]
                if not isinstance(outputs, dict)
                or item["output"] not in outputs
                or not matches_type(outputs[item["output"]], item["type"])
            ]
            trial_coverage, coverage_failures = coverage(claim["trial_case"], nodes, outputs)
            assertion_results = []
            assertion_failures = []
            for expectation in claim["trial_case"]["assertions"]:
                try:
                    selected = at_path(outputs[expectation["output"]], expectation["path"])
                    require(
                        isinstance(selected, dict)
                        and set(selected) == {"asset", "representation_id"},
                        "INVALID_BINDING",
                        "Trial assertion does not resolve to a published representation",
                    )
                    ref = selected["asset"]
                    require(
                        ref.get("project_id") == claim["project_id"],
                        "INVALID_BINDING",
                        "Trial output belongs to another project",
                    )
                    asset = self.data.json(
                        "GET",
                        f"/internal/projects/{claim['project_id']}/assets/"
                        f"{ref['asset_id']}/versions/{ref['version_id']}",
                    )
                    require(asset["ref"] == ref, "INVALID_BINDING", "Published AssetRef changed")
                    representation = next(
                        (
                            item
                            for item in asset["manifest"]["representations"]
                            if item["representation_id"] == selected["representation_id"]
                        ),
                        None,
                    )
                    passed = bool(
                        representation
                        and representation.get("schema_id") == expectation["schema_id"]
                        and (
                            expectation.get("representation_digest") is None
                            or representation.get("digest") == expectation["representation_digest"]
                        )
                    )
                except RuleViolation as problem:
                    if problem.code in {"DEPENDENCY_UNAVAILABLE", "RESOURCE_NOT_FOUND"}:
                        raise
                    selected, passed = None, False
                except (KeyError, TypeError):
                    selected, passed = None, False
                assertion_results.append(
                    {
                        "output": expectation["output"],
                        "path": expectation["path"],
                        "schema_id": expectation["schema_id"],
                        "passed": passed,
                    }
                )
                if not passed:
                    assertion_failures.append(expectation["output"])
            verdict = (
                "PASS"
                if not step_failures
                and not required_output_failures
                and not assertion_failures
                and not coverage_failures
                else "FAIL"
            )
            reasons = (
                (["REQUIRED_STEP_FAILED"] if step_failures else [])
                + (["REQUIRED_OUTPUT_FAILED"] if required_output_failures else [])
                + (["OUTPUT_ASSERTION_FAILED"] if assertion_failures else [])
                + (["COVERAGE_IDENTITY_FAILED"] if coverage_failures else [])
            )
            result = {
                "profile": "workflow-trial-result-v1",
                "trial_case_digest": claim["trial_case_digest"],
                "action_id": claim["action_id"],
                "execution_id": execution_id,
                "execution_state": "SUCCEEDED",
                "publication_state": "COMMITTED",
                "publication_digest": canonical_digest(publication),
                "started_steps": sum(bool(item["executed"]) for item in nodes),
                "required_steps": {"count": len(required), "failed": step_failures},
                "required_outputs": {
                    "count": len(claim["trial_case"]["required_outputs"]),
                    "failed": required_output_failures,
                },
                "assertions": assertion_results,
                "coverage": trial_coverage,
                "coverage_failures": coverage_failures,
                "reasons": reasons,
                "verdict": verdict,
            }
            self._finish(claim, verdict, result=result)
        except RuleViolation as problem:
            if problem.code in {"DEPENDENCY_UNAVAILABLE", "RESOURCE_NOT_FOUND"}:
                self._retry(claim, "DEPENDENCY_UNAVAILABLE")
            else:
                self._finish(claim, "ERROR", category="IDENTITY_OR_PROTOCOL_CONFLICT")
        except (KeyError, TypeError, ValueError):
            self._finish(claim, "ERROR", category="IDENTITY_OR_PROTOCOL_CONFLICT")
        return True
