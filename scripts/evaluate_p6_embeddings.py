"""Run the frozen P6 embedding diagnostics against a configured LAN service."""

import argparse
import json
import os
from pathlib import Path

from platform_agent_runtime.embedding_evaluation import evaluate_embedding_suite


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("PLATFORM_EMBEDDING_BASE_URL"))
    parser.add_argument(
        "--models",
        nargs="+",
        default=["qwen3-embedding-4b-q4", "qwen3-embedding-0.6b-q8", "bge-m3"],
    )
    parser.add_argument("--selected-model", default="qwen3-embedding-4b-q4")
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or PLATFORM_EMBEDDING_BASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    report = evaluate_embedding_suite(
        args.base_url,
        root / "evaluations/p6/embedding_cases.json",
        args.models,
        selected_model=args.selected_model,
        dimensions=args.dimensions,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
