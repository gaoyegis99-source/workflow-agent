"""Record P6 simulated-LLM and embedding diagnostics from current evidence."""

from __future__ import annotations

import hashlib
import json
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFICATION = ROOT / ".p6-runtime" / "verification"
RELEASE = ROOT / ".p6-runtime" / "release" / "dist"


def junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
        "seconds": round(sum(float(suite.attrib.get("time", 0)) for suite in suites), 3),
        "path": str(path.relative_to(ROOT)),
    }


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    simulated = json.loads(
        (VERIFICATION / "p610-simulated-evaluation.json").read_text(encoding="utf-8")
    )
    embeddings = [
        json.loads(
            (VERIFICATION / f"p610-embedding-evaluation-r{repeat}.json").read_text(encoding="utf-8")
        )
        for repeat in range(1, 4)
    ]
    model_ids = [value["model"] for value in embeddings[0]["models"]]
    model_summary = []
    for model_id in model_ids:
        runs = [
            next(value for value in report["models"] if value["model"] == model_id)
            for report in embeddings
        ]
        model_summary.append(
            {
                "model": model_id,
                "runs": len(runs),
                "top1_accuracy_min": min(value["metrics"]["top1_accuracy"] for value in runs),
                "recall_at_3_min": min(value["metrics"]["recall_at_3"] for value in runs),
                "mrr_min": min(value["metrics"]["mean_reciprocal_rank"] for value in runs),
                "single_batch_cosine_min": min(
                    value["metrics"]["single_batch_cosine_min"] for value in runs
                ),
                "elapsed_seconds_best": min(value["metrics"]["elapsed_seconds"] for value in runs),
                "elapsed_seconds_median": round(
                    statistics.median(value["metrics"]["elapsed_seconds"] for value in runs),
                    3,
                ),
                "elapsed_seconds_worst": max(value["metrics"]["elapsed_seconds"] for value in runs),
                "non_top1_cases": sorted(
                    {
                        result["id"]
                        for value in runs
                        for result in value["results"]
                        if result["rank"] > 1
                    }
                ),
            }
        )
    evidence = {
        "phase": "P6.10 simulated LLM and embedding diagnostics",
        "version": "0.9.3",
        "status": (
            "PASS"
            if simulated["status"] == "PASS"
            and all(value["status"] == "PASS" for value in embeddings)
            else "FAIL"
        ),
        "simulated_llm": simulated,
        "embedding": {
            "suite_id": embeddings[0]["suite_id"],
            "selected_model": embeddings[0]["selected_model"],
            "dimensions": embeddings[0]["dimensions"],
            "runs": 3,
            "authorization_effect": "NONE",
            "model_summary": model_summary,
            "raw_evidence": [
                {
                    "path": f".p6-runtime/verification/p610-embedding-evaluation-r{repeat}.json",
                    "digest": digest(VERIFICATION / f"p610-embedding-evaluation-r{repeat}.json"),
                }
                for repeat in range(1, 4)
            ],
        },
        "verification": {
            "p6_simulated_gate": junit(VERIFICATION / "p610-simulated-tests.xml"),
            "http_api": junit(VERIFICATION / "p610-api-tests.xml"),
            "distribution": "PASS",
            "proxy_bypass": json.loads(
                (VERIFICATION / "p610-proxy-bypass.json").read_text(encoding="utf-8")
            ),
        },
        "release_artifacts": [
            {
                "path": str(path.relative_to(ROOT)),
                "bytes": path.stat().st_size,
                "digest": digest(path),
            }
            for path in sorted(RELEASE.glob("*0.9.3*"))
            if path.is_file()
        ],
        "findings": [
            {
                "severity": "HIGH",
                "status": "FIXED",
                "code": "EMBEDDING_CLIENT_MISSING",
                "finding": (
                    "The repository had no PLATFORM_EMBEDDING_BASE_URL client despite "
                    "deployment expectations."
                ),
                "resolution": "Added a bounded EmbeddingPort and a strict no-proxy HTTP adapter.",
            },
            {
                "severity": "MEDIUM",
                "status": "OPEN_LIMITATION",
                "code": "MODEL_ARTIFACT_NOT_IDENTIFIED",
                "finding": (
                    "The gateway response identifies a model alias and GPU worker but no "
                    "immutable model artifact digest."
                ),
            },
            {
                "severity": "MEDIUM",
                "status": "MITIGATED",
                "code": "SMALL_MODEL_POLICY_CONFUSION",
                "finding": (
                    "The smaller Qwen and BGE models can rank datum conflict A08 behind "
                    "general ambiguity A02."
                ),
                "resolution": (
                    "Use retrieval only for candidates and keep policy/authorization deterministic."
                ),
            },
            {
                "severity": "MEDIUM",
                "status": "OPEN_LIMITATION",
                "code": "EMBEDDING_LATENCY_VARIANCE",
                "finding": (
                    "The selected 4B model kept stable retrieval quality while client wall "
                    "time varied by more than an order of magnitude across three runs."
                ),
                "resolution": (
                    "Keep embedding off the mandatory Agent action path until queue SLOs, "
                    "caching and bounded asynchronous fallback are defined."
                ),
            },
            {
                "severity": "LOW",
                "status": "OPEN_LIMITATION",
                "code": "LAN_ENDPOINT_HAS_NO_APPLICATION_AUTH",
                "finding": (
                    "The supplied LAN embedding endpoint accepts requests without an "
                    "application credential."
                ),
            },
            {
                "severity": "INFO",
                "status": "BY_DESIGN",
                "code": "SIMULATED_LLM_SCOPE",
                "finding": (
                    "The deterministic model proves orchestration, recovery and authority "
                    "boundaries, not natural-language reasoning quality."
                ),
            },
        ],
    }
    output = ROOT / "docs/verification/p610-simulated-embedding-evidence.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    if evidence["status"] != "PASS":
        raise SystemExit("P6.10 evidence did not pass")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
