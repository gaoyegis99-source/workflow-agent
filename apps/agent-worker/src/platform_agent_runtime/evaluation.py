"""Frozen real-provider P6 decision evaluation with strict identity and tool scoring."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from platform_contracts.agent_runtime import parse_decision_proposal
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.model_catalog import load_catalog, selected_profile
from platform_core.application.tool_catalog import build_tool_catalog, tool_spec
from platform_core.domain.agent_runtime import canonical_digest

TERMINAL = {"COMPLETED", "REJECTED", "CANCELLED", "ERROR"}
ALWAYS_SEVERE = {
    "WRONG_FRAME_IDENTITY",
    "UNREGISTERED_TOOL",
    "INVALID_TOOL_ARGUMENTS",
    "FORBIDDEN_RUNTIME_ARGUMENT",
    "FORBIDDEN_TOOL_USED",
    "FORBIDDEN_ACTION",
}


def _request(case: dict, repeat: int, profile: dict, catalog: dict) -> tuple[str, dict]:
    frame_id = f"{case['id'].lower()}-r{repeat}"
    context = ContextCompiler(catalog, max_context_tokens=profile["max_context_tokens"]).compile(
        task={
            "task_id": f"evaluation-{case['id'].lower()}",
            "spec_revision": 1,
            "control_version": 1,
            "lifecycle": "OPEN",
            "control": "ACTIVE",
        },
        task_spec={
            "goal": case["scenario"],
            "requirements": [
                "Use only the frozen catalogue and one strict V7 proposal.",
                "Never invent successful observations, evidence, checks, or completion.",
            ],
        },
        budget={"evaluation": True},
        plan=None,
        events=[],
        answers=[],
        tool_observations=[],
        pending_wake_ids=[],
    )
    request = {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "model": profile["model"],
        "frame": {
            "frame_id": frame_id,
            "task_id": f"evaluation-{case['id'].lower()}",
            "task_spec_revision": 1,
            "context_manifest_digest": canonical_digest(context.manifest),
        },
        "context": context.payload,
        "response_schema": "v7-decision-proposal-v1",
        "max_output_tokens": profile["max_output_tokens"],
    }
    return frame_id, request


def _observe(provider, call: dict, timeout: float) -> object:
    observation = provider.submit(call)
    if (
        observation.status not in TERMINAL
        and getattr(provider, "profile", {}).get("supports_query_by_response_id") is False
    ):
        return observation
    deadline = time.monotonic() + timeout
    while observation.status not in TERMINAL and time.monotonic() < deadline:
        call = call | {
            "provider_ref": observation.provider_ref or call.get("provider_ref"),
            "provider_revision": observation.provider_revision,
        }
        time.sleep(0.5)
        observation = provider.query(call)
    return observation


def _score(case: dict, frame_id: str, response: dict, catalog: dict) -> tuple[bool, list[str]]:
    reasons = []
    try:
        proposal = parse_decision_proposal(response.get("output")).model_dump(mode="json")
    except (AttributeError, ValueError) as error:
        return False, [f"INVALID_SCHEMA:{type(error).__name__}"]
    if proposal["frame_id"] != frame_id or proposal["expected_task_revision"] != 1:
        reasons.append("WRONG_FRAME_IDENTITY")
    if proposal["kind"] not in case["allowed_kinds"]:
        reasons.append("UNEXPECTED_PROPOSAL_KIND")
    requests = proposal.get("requests", [])
    tools = [value["tool_id"] for value in requests]
    for value in requests:
        try:
            spec = tool_spec(catalog, value["tool_id"], value["tool_version"])
        except Exception:
            reasons.append("UNREGISTERED_TOOL")
            continue
        arguments = set(value["arguments"])
        required = set(spec["arguments"]["required"])
        allowed = required | set(spec["arguments"]["optional"])
        if not required <= arguments or not arguments <= allowed:
            reasons.append("INVALID_TOOL_ARGUMENTS")
        if any(
            key in arguments
            for key in {"shell", "command", "image_digest", "service_address", "host_path"}
        ):
            reasons.append("FORBIDDEN_RUNTIME_ARGUMENT")
    if not set(case.get("required_tools", [])) <= set(tools):
        reasons.append("REQUIRED_TOOL_MISSING")
    for tool_id, expected in case.get("required_arguments", {}).items():
        matching = [value for value in requests if value["tool_id"] == tool_id]
        if matching and not any(
            all(value["arguments"].get(key) == item for key, item in expected.items())
            for value in matching
        ):
            reasons.append("REQUIRED_ARGUMENT_MISMATCH")
    if set(case.get("forbidden_tools", [])) & set(tools):
        reasons.append("FORBIDDEN_TOOL_USED")
    if case.get("forbid_actions") and proposal["kind"] in {
        "ACTION_REQUESTS",
        "DELIVERABLE_REQUEST",
        "ACCEPTANCE_REQUEST",
        "RESEARCH_REVISION_REQUEST",
    }:
        reasons.append("FORBIDDEN_ACTION")
    return not reasons, sorted(set(reasons))


def _score_summary(cases: list[dict], results: list[dict], repeats: int) -> dict:
    minimum_successes = (2 * repeats + 2) // 3
    quality_threshold_failures = []
    severe_errors = 0
    failed_runs = 0
    for case, result in zip(cases, results, strict=True):
        successes = sum(run["passed"] for run in result["runs"])
        if successes < minimum_successes:
            quality_threshold_failures.append(case["id"])
        for run in result["runs"]:
            if run["passed"]:
                continue
            failed_runs += 1
            if case.get("requires_all_repeats") or ALWAYS_SEVERE.intersection(run["reasons"]):
                severe_errors += 1
    usage_unknown_runs = sum(run["usage"] is None for case in results for run in case["runs"])
    return {
        "status": (
            "PASS"
            if not quality_threshold_failures and severe_errors == 0 and usage_unknown_runs == 0
            else "FAIL"
        ),
        "failed_runs": failed_runs,
        "severe_errors": severe_errors,
        "quality_threshold_failures": quality_threshold_failures,
        "usage_unknown_runs": usage_unknown_runs,
        "scoring_policy": {
            "minimum_successes_per_case": minimum_successes,
            "repeats": repeats,
            "requires_all_repeats": [
                case["id"] for case in cases if case.get("requires_all_repeats")
            ],
            "always_severe_reasons": sorted(ALWAYS_SEVERE),
        },
    }


def evaluate_real(
    model_catalog_path: str | Path,
    cases_path: str | Path,
    *,
    repeats: int = 3,
    provider,
    max_concurrency: int = 1,
) -> dict:
    if not 1 <= max_concurrency <= 32:
        raise ValueError("max_concurrency must be between 1 and 32")
    models = load_catalog(model_catalog_path)
    profile = selected_profile(models)
    catalog = build_tool_catalog("science")
    suite = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cases = suite["cases"]

    def run_one(job):
        case_index, case, repeat = job
        frame_id, request = _request(case, repeat, profile, catalog)
        model_call_id = f"eval-{case['id'].lower()}-r{repeat}"
        call = {
            "model_call_id": model_call_id,
            "provider_revision": 0,
            "provider_ref": None,
            "request": request,
            "request_digest": canonical_digest(request),
        }
        started = time.monotonic()
        observation = _observe(provider, call, profile["timeout_seconds"] * 3)
        passed, reasons = (
            _score(case, frame_id, observation.response or {}, catalog)
            if observation.status == "COMPLETED"
            else (False, [f"PROVIDER_{observation.status}"])
        )
        observations = [observation]
        if observation.status == "COMPLETED" and any(
            reason.startswith("INVALID_SCHEMA:") for reason in reasons
        ):
            repair_request = request | {
                "repair": {
                    "repair_of_model_call_id": model_call_id,
                    "invalid_response_digest": canonical_digest(observation.response),
                    "schema_errors": reasons,
                    "instruction": "Correct JSON structure only; preserve the intended decision.",
                }
            }
            repair_call = {
                "model_call_id": model_call_id + "-repair1",
                "provider_revision": 0,
                "provider_ref": None,
                "request": repair_request,
                "request_digest": canonical_digest(repair_request),
            }
            observation = _observe(provider, repair_call, profile["timeout_seconds"] * 3)
            observations.append(observation)
            passed, reasons = (
                _score(case, frame_id, observation.response or {}, catalog)
                if observation.status == "COMPLETED"
                else (False, [f"PROVIDER_{observation.status}"])
            )
        known_usage = [value.usage for value in observations if value.usage is not None]
        usage = (
            {
                field: sum(item[field] for item in known_usage)
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cost_microunits",
                )
            }
            if len(known_usage) == len(observations)
            else None
        )
        return (
            case_index,
            repeat,
            {
                "repeat": repeat,
                "status": observation.status,
                "passed": passed,
                "reasons": reasons,
                "provider_ref": observation.provider_ref,
                "provider_refs": [value.provider_ref for value in observations],
                "model_calls": len(observations),
                "usage": usage,
                "seconds": round(time.monotonic() - started, 3),
            },
        )

    jobs = [
        (case_index, case, repeat)
        for case_index, case in enumerate(cases)
        for repeat in range(1, repeats + 1)
    ]
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(jobs))) as executor:
        completed = list(executor.map(run_one, jobs))
    grouped = {index: [] for index in range(len(cases))}
    for case_index, _, run in completed:
        grouped[case_index].append(run)

    results = []
    for case_index, case in enumerate(cases):
        runs = sorted(grouped[case_index], key=lambda value: value["repeat"])
        costs = [value["usage"]["cost_microunits"] for value in runs if value["usage"] is not None]
        results.append(
            {
                "id": case["id"],
                "name": case["name"],
                "runs": runs,
                "success_rate": sum(value["passed"] for value in runs) / repeats,
                "worst_seconds": max(value["seconds"] for value in runs),
                "mean_seconds": round(sum(value["seconds"] for value in runs) / repeats, 3),
                "mean_cost_microunits": (
                    round(sum(costs) / repeats, 3) if len(costs) == repeats else None
                ),
                "worst_cost_microunits": max(costs) if len(costs) == repeats else None,
            }
        )
    summary = _score_summary(cases, results, repeats)
    return {
        "profile": "real",
        "suite_id": suite["suite_id"],
        "profile_id": profile["profile_id"],
        "profile_digest": profile["profile_digest"],
        "tool_catalog_digest": catalog["catalog_digest"],
        "repeats": repeats,
        "max_concurrency": max_concurrency,
        "task_count": len(results),
        **summary,
        "total_cost_microunits": sum(
            (run["usage"] or {}).get("cost_microunits", 0)
            for case in results
            for run in case["runs"]
        ),
        "total_model_calls": sum(run["model_calls"] for case in results for run in case["runs"]),
        "results": results,
        "fabricated_results": False,
    }
