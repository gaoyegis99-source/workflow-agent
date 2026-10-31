"""R4.2 immutable ExecutionPlan benchmark using fixed long-running compute nodes."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from platform_infrastructure.database import Database  # noqa: E402

from benchmarks.p4.scheduler_diagnostics import distribution, run_once  # noqa: E402
from benchmarks.p4.scheduler_r2 import summarize  # noqa: E402
from scripts.configure.p2_environment import PostgreSQL  # noqa: E402
from scripts.lib.runtime_layout import P4_RUNTIME_ROOT  # noqa: E402

PROFILES = {
    "quick": {"rows": 20_000, "groups": 200, "nodes": 4, "concurrency": 4, "repeats": 1},
    "standard": {
        "rows": 50_000,
        "groups": 500,
        "nodes": 8,
        "concurrency": 4,
        "repeats": 3,
    },
}
BASELINE_PATH = P4_RUNTIME_ROOT / "verification/p4-r41-engine-binding-evidence.json"
PLAN_SOURCES = (
    "runtimes/cwl/bridge.py",
    "runtimes/cwl/engine.py",
    "apps/execution-service/src/platform_execution/api.py",
    "apps/execution-service/src/platform_execution/trace.py",
    "apps/execution-service/src/platform_execution/adapters/docker.py",
    "apps/execution-service/src/platform_execution/application/workflow_runner.py",
    "packages/platform-core/src/platform_core/domain/workflows.py",
    "packages/platform-core/src/platform_core/application/workflow_plan.py",
    "packages/platform-core/src/platform_core/application/workflows.py",
    "packages/contracts/src/platform_contracts/workflows.py",
    "benchmarks/p4/scheduler_r42.py",
    "tests/performance/probe_worker.py",
    "scripts/checks/check_p4_scheduler_r42.sh",
)


def fixed_r41_baseline():
    evidence = json.loads(BASELINE_PATH.read_text())
    if (
        evidence.get("kind") != "p4-r41-engine-binding"
        or evidence.get("profile") != "standard"
        or evidence.get("version") != "0.3.4"
        or evidence.get("status") != "PASS"
    ):
        raise RuntimeError("The accepted R4.1 standard evidence is unavailable")
    summary = evidence["summary"]
    median = summary["workflow_seconds"]["median"]
    if abs(median - 96.704482) > 0.001:
        raise RuntimeError("The accepted R4.1 baseline changed")
    return {
        "source": str(BASELINE_PATH.relative_to(ROOT)),
        "workflow_seconds_median": median,
        "maximum_r42_median": round(median * 1.05, 6),
        "process_group_warnings": summary["process_group_warnings"]["median"],
    }


def validate(evidence):
    profile = evidence["profile_configuration"]
    values = evidence["runs"]
    summary = summarize(values)
    summary["trace_overhead_percent"] = distribution(
        [value["trace"]["estimated_overhead_percent"] for value in values]
    )
    engines = [value["engine"] for value in values]
    failures = []

    def gate(condition, message):
        if not condition:
            failures.append(message)

    gate(len(values) == profile["repeats"], "R4.2 run count differs from the profile")
    gate(all(value["engine_init_observed"] for value in values), "Docker init was not observed")
    gate(summary["process_group_warnings"]["max"] == 0, "process-group warnings remain")
    gate(all(value["failed_workers"] == 0 for value in engines), "Toil reported failed workers")
    gate(all(value["running_containers"] == 0 for value in engines), "engine still running")
    gate(all(value["nonzero_container_pids"] == 0 for value in engines), "engine PID remains")
    gate(all(value["processes_after_exit"] == 0 for value in engines), "engine process remains")
    gate(
        summary["post_requests"]["min"] == summary["post_requests"]["max"] == profile["nodes"],
        "each workflow node must be submitted exactly once",
    )
    gate(
        summary["overlap_peak"]["min"] >= profile["concurrency"],
        "the configured compute concurrency was not observed",
    )
    gate(
        all(
            value["workflow"]["budget"]["used"] == profile["nodes"]
            and value["workflow"]["budget"]["reserved"] == 0
            and value["workflow"]["budget"]["in_flight"] == 0
            for value in values
        ),
        "node budget did not settle exactly once",
    )
    if evidence["profile"] == "standard":
        gate(
            summary["execution_queue_p95_ms"]["max"] <= 500,
            "Execution queue p95 exceeds 500ms",
        )
        gate(
            summary["workflow_seconds"]["median"] <= evidence["r41_baseline"]["maximum_r42_median"],
            "workflow median regressed by more than five percent from R4.1",
        )
    evidence["summary"] = summary
    evidence["gates"] = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    evidence["status"] = evidence["gates"]["status"]
    if failures:
        raise AssertionError(failures)


def report(evidence):
    summary = evidence["summary"]
    baseline = evidence["r41_baseline"]
    lines = [
        "# P4.1 R4.2 最小 ExecutionPlan 验收",
        "",
        f"日期：{evidence['finished_at']}。状态：**{evidence['status']}**。",
        "",
        "本轮使用生产 CwlDocker、2 个 Execution Worker、嵌套条件 scatter 和固定 30 CPU 秒节点。",
        "工作流图、预算、恢复协议和 Toil 调度参数保持 R4.1 不变；"
        "ExecutionPlan 参与真实节点校验，Toil 仍运行原 CWL 图。",
        "",
        "| 指标 | R4.1 固定基线 | R4.2 |",
        "| --- | ---: | ---: |",
        f"| 工作流总耗时中位数 | {baseline['workflow_seconds_median']:.3f}s | "
        f"{summary['workflow_seconds']['median']:.3f}s |",
        f"| 计算窗口中位数 | — | {summary['compute_window_seconds']['median']:.3f}s |",
        f"| 有效计算并行度中位数 | — | {summary['effective_parallelism']['median']:.3f} |",
        f"| 槽位利用率中位数 | — | {summary['slot_utilization_percent']['median']:.3f}% |",
        f"| Execution 排队 p95 最差值 | — | {summary['execution_queue_p95_ms']['max']:.3f}ms |",
        f"| 节点 POST | — | {summary['post_requests']['median']:.0f} |",
        f"| 进程组警告 | {baseline['process_group_warnings']:.0f} | "
        f"{summary['process_group_warnings']['max']:.0f} |",
        f"| trace 估算开销中位数 | — | {summary['trace_overhead_percent']['median']:.3f}% |",
        "",
        f"R4.2 允许的总耗时中位数上限为 `{baseline['maximum_r42_median']:.3f}s`。",
        "ExecutionPlan 已参与节点准入；工作流图、chaining 和图压平不属于本轮。",
        "",
    ]
    return "\n".join(lines)


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "cwl_image": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
        "performance_operator_image": (P4_RUNTIME_ROOT / "performance-operator-image.txt")
        .read_text()
        .strip(),
    }


def source_fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PLAN_SOURCES}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument(
        "--output",
        type=Path,
    )
    parser.add_argument(
        "--report",
        type=Path,
    )
    args = parser.parse_args()
    if args.output is None:
        suffix = "-quick" if args.profile == "quick" else ""
        args.output = P4_RUNTIME_ROOT / f"verification/p4-r42-execution-plan{suffix}-evidence.json"
    if args.report is None:
        suffix = "-quick" if args.profile == "quick" else ""
        args.report = P4_RUNTIME_ROOT / f"verification/p4-r42-execution-plan{suffix}-report.md"
    profile = PROFILES[args.profile]
    configuration = {
        "id": "R42_EXECUTION_PLAN",
        "workflow": "nested",
        "workers": 2,
        "init": True,
        "baseline_resources": False,
        "cwl_image": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
    }
    runs = []
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r42-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for repeat in range(1, profile["repeats"] + 1):
                directory = Path(temp) / f"r42-{repeat}"
                directory.mkdir()
                result = run_once(
                    postgres,
                    platform_database,
                    admin,
                    directory,
                    configuration,
                    profile,
                    repeat,
                    trace_overhead_limit=None,
                )
                runs.append(result)
                print(
                    "PASS:",
                    f"repeat={repeat}",
                    f"workflow={result['workflow']['workflow_seconds']:.3f}s",
                    f"warnings={result['engine']['process_group_warnings']}",
                    flush=True,
                )
        finally:
            platform_database.close()
            admin.dispose()
    evidence = {
        "schema_version": 1,
        "kind": "p4-r42-execution-plan",
        "version": "0.3.5",
        "status": "PENDING",
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "profile_configuration": profile,
        "r41_baseline": fixed_r41_baseline(),
        "fixed_execution_workers": 2,
        "disable_chaining": True,
        "environment": environment(),
        "source_sha256": source_fingerprints(),
        "runs": runs,
        "total_seconds": round(time.perf_counter() - started, 6),
    }
    error = None
    try:
        validate(evidence)
    except AssertionError as problem:
        error = problem
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    args.report.write_text(report(evidence) + "\n")
    print(f"{evidence['status']}: evidence written to {args.output}")
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
