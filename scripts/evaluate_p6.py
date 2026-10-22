"""Create an auditable P6 evaluation record without pretending absent providers ran."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from platform_agent_runtime.evaluation import evaluate_real
from platform_core.application.model_catalog import load_catalog, selected_profile


def passed_nodeids(junit_path: Path) -> list[str]:
    root = ElementTree.parse(junit_path).getroot()
    passed = []
    for testcase in root.iter("testcase"):
        failed = any(
            child.tag.rsplit("}", 1)[-1] in {"failure", "error", "skipped"} for child in testcase
        )
        if failed:
            continue
        module = testcase.attrib["classname"].replace(".", "/") + ".py"
        passed.append(f"{module}::{testcase.attrib['name']}")
    return passed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("deterministic", "real"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--junit", action="append")
    parser.add_argument("--required-repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=15)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / "evaluations/p6/golden_tasks.json").read_text())
    tasks = suite["tasks"]
    if args.profile == "real":
        model_catalog = os.environ.get("V7_MODEL_CATALOG")
        if not model_catalog:
            result = {
                "profile": "real",
                "status": "NOT_RUN",
                "reason": "NO_FROZEN_PROVIDER_PROFILE",
                "task_count": len(tasks),
                "fabricated_results": False,
            }
        else:
            profile = selected_profile(load_catalog(model_catalog))
            if not os.environ.get(profile["credential_env"]):
                result = {
                    "profile": "real",
                    "status": "NOT_RUN",
                    "reason": "NO_PROVIDER_CREDENTIALS",
                    "profile_id": profile["profile_id"],
                    "profile_digest": profile["profile_digest"],
                    "task_count": len(tasks),
                    "fabricated_results": False,
                }
            else:
                result = evaluate_real(
                    model_catalog,
                    root / "evaluations/p6/real_model_cases.json",
                    repeats=3,
                    max_concurrency=args.concurrency,
                )
    else:
        if not args.junit:
            parser.error("--junit is required for the deterministic profile")
        if not 1 <= args.required_repeats <= 10:
            parser.error("--required-repeats must be between 1 and 10")
        executed = Counter(
            nodeid
            for value in args.junit
            if Path(value).exists()
            for nodeid in passed_nodeids(Path(value))
        )
        expected = {task["nodeid"] for task in tasks}
        missing = sorted(nodeid for nodeid in expected if executed[nodeid] < args.required_repeats)
        result = {
            "profile": "deterministic",
            "status": "PASS" if not missing else "FAIL",
            "suite_id": suite["suite_id"],
            "task_count": len(tasks),
            "required_repeats": args.required_repeats,
            "covered_task_ids": [
                task["id"] for task in tasks if executed[task["nodeid"]] >= args.required_repeats
            ],
            "run_counts": {nodeid: executed[nodeid] for nodeid in sorted(expected)},
            "minimum_stability": min(
                executed[nodeid] / args.required_repeats for nodeid in expected
            ),
            "missing_evidence": missing,
        }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
