"""R4.4 role-split worker acceptance with two concurrent long workflows."""

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

from benchmarks.p4.scheduler_diagnostics import distribution  # noqa: E402
from benchmarks.p4.scheduler_r44 import PROFILES, run_pair  # noqa: E402
from scripts.p2_environment import PostgreSQL  # noqa: E402

ROLE_CONFIGURATION = {
    "id": "ROLE_SPLIT",
    "workers": 10,
    "roles": ("supervisor",) + ("node",) * 8 + ("publication",),
}
BASELINE_PATH = ROOT / "docs/verification/p4-r44-worker-queue-evidence.json"
SOURCES = (
    "apps/execution-service/src/platform_execution/adapters/repository.py",
    "apps/execution-service/src/platform_execution/adapters/tables.py",
    "apps/execution-service/src/platform_execution/application/runner.py",
    "apps/execution-service/src/platform_execution/database.py",
    "apps/execution-service/src/platform_execution/worker.py",
    "apps/execution-service/src/platform_execution/migrations/versions/0004_execution_work_class.py",
    "benchmarks/p4/scheduler_r44.py",
    "benchmarks/p4/scheduler_r44_roles.py",
    "tests/performance/long_operator.Dockerfile",
    "scripts/build_p4_performance_image.sh",
    "scripts/run_p3.sh",
    "scripts/run_p4.sh",
)


def fixed_baseline():
    evidence = json.loads(BASELINE_PATH.read_text())
    assessment = evidence.get("assessment", {})
    if (
        evidence.get("kind") != "p4-r44-worker-queue-gate"
        or evidence.get("version") != "0.3.6"
        or evidence.get("profile") != "standard"
        or evidence.get("status") != "PASS"
        or assessment.get("decision") != "IMPLEMENT_WORK_CLASS"
    ):
        raise RuntimeError("Accepted R4.4 trigger evidence is unavailable")
    generic = assessment["configurations"]["FOUR_WORKERS"]
    return {
        "source": str(BASELINE_PATH.relative_to(ROOT)),
        "two_worker_queue_p95_max_ms": assessment["configurations"]["TWO_WORKERS"][
            "node_queue_p95_ms"
        ]["max"],
        "generic_four_worker_queue_p95_max_ms": generic["node_queue_p95_ms"]["max"],
        "generic_four_worker_seconds_median": generic["concurrent_seconds"]["median"],
    }


def summarize(runs):
    return {
        "runs": len(runs),
        "concurrent_seconds": distribution([item["concurrent_seconds"] for item in runs]),
        "node_queue_p95_ms": distribution([item["node_queue_ms"]["p95"] for item in runs]),
        "node_submit_posts": distribution([item["node_submit_posts"] for item in runs]),
        "process_group_warnings": distribution([item["process_group_warnings"] for item in runs]),
        "parent_saturation_samples": sum(
            item["lease_sampling"]["parent_saturation_with_ready_nodes_samples"] for item in runs
        ),
    }


def validate(evidence):
    profile = evidence["profile_configuration"]
    runs = evidence["runs"]
    summary = summarize(runs)
    baseline = evidence["trigger_baseline"]
    failures = []

    def gate(condition, message):
        if not condition:
            failures.append(message)

    gate(len(runs) == profile["repeats"], "Role-split run count differs from the profile")
    gate(
        summary["node_submit_posts"]["min"]
        == summary["node_submit_posts"]["max"]
        == profile["nodes"] * 2,
        "Each node must be submitted exactly once",
    )
    gate(summary["process_group_warnings"]["max"] == 0, "Process-group warnings remain")
    for run in runs:
        gate(
            run["work_class_counts"]
            == {
                "NODE_EXECUTION": profile["nodes"] * 2,
                "PUBLICATION_RECONCILE": 2,
            },
            "Durable work classes do not match two roots and their nodes",
        )
        for workflow in run["workflows"]:
            timeline = workflow["workflow"]["container_seconds"]["timeline"]
            gate(
                len(timeline) == profile["nodes"]
                and all(item["duration"] >= 25 for item in timeline),
                "A fixed long node was missing or shorter than 25 seconds",
            )
            budget = workflow["workflow"]["budget"]
            gate(
                budget["used"] == profile["nodes"]
                and budget["reserved"] == 0
                and budget["in_flight"] == 0,
                "A workflow budget did not settle exactly once",
            )
            gate(
                workflow["trace"]["required_event_completeness"] == 1,
                "Required trace completeness is below 100 percent",
            )
            gate(workflow["engine_strategy"] == "flat_v1", "flat_v1 was not retained")
            gate(workflow["engine"]["failed_workers"] == 0, "Toil reported failed workers")
    if evidence["profile"] == "standard":
        gate(summary["node_queue_p95_ms"]["max"] <= 500, "Node queue p95 exceeds 500ms")
        gate(
            summary["concurrent_seconds"]["median"]
            <= baseline["generic_four_worker_seconds_median"] * 1.05,
            "Role split regressed concurrent wall time by more than five percent",
        )
    evidence["summary"] = summary
    evidence["gates"] = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    evidence["status"] = evidence["gates"]["status"]
    if failures:
        raise AssertionError(failures)


def report(evidence):
    summary = evidence["summary"]
    baseline = evidence["trigger_baseline"]
    return (
        "\n".join(
            [
                "# P4.1 R4.4 Worker 队列拆分验收",
                "",
                f"日期：{evidence['finished_at']}。状态：**{evidence['status']}**。",
                "",
                "部署为 1 个 supervisor、8 个 node 和 1 个 publication Worker；supervisor 保留",
                "publication 对账能力。同时运行两条",
                "flat_v1 工作流，每条包含固定 30 CPU 秒节点。公开 API、预算和节点身份不变。",
                "",
                "| 指标 | 拆分前 2 Worker | 拆分前 4 个通用 Worker | 角色拆分 |",
                "| --- | ---: | ---: | ---: |",
                f"| 叶子领取 p95 最差值 | {baseline['two_worker_queue_p95_max_ms']:.3f}ms | "
                f"{baseline['generic_four_worker_queue_p95_max_ms']:.3f}ms | "
                f"{summary['node_queue_p95_ms']['max']:.3f}ms |",
                "| 两工作流总耗时中位数 | — | "
                f"{baseline['generic_four_worker_seconds_median']:.3f}s | "
                f"{summary['concurrent_seconds']['median']:.3f}s |",
                f"| 节点 POST | — | 16 | {summary['node_submit_posts']['median']:.0f} |",
                f"| 进程组警告 | — | 0 | {summary['process_group_warnings']['max']:.0f} |",
                "",
                "work_class 是 Execution 自有的持久列和领取索引；Worker 不解析 JSON 来选择工作。",
                "P3 仍可使用默认 all 角色，P4 的启动脚本固定使用角色布局。",
            ]
        )
        + "\n"
    )


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "cwl_image": (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip(),
        "performance_operator_image": (ROOT / ".p4-runtime/performance-operator-image.txt")
        .read_text()
        .strip(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    suffix = "-quick" if args.profile == "quick" else ""
    args.output = args.output or ROOT / (
        f"docs/verification/p4-r44-role-split{suffix}-evidence.json"
    )
    args.report = args.report or ROOT / (f"docs/verification/p4-r44-role-split{suffix}-report.md")
    profile = PROFILES[args.profile]
    runs = []
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r44-roles-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for repeat in range(1, profile["repeats"] + 1):
                directory = Path(temp) / f"roles-{repeat}"
                directory.mkdir()
                value = run_pair(
                    postgres,
                    platform_database,
                    admin,
                    directory,
                    ROLE_CONFIGURATION,
                    profile,
                    repeat,
                )
                runs.append(value)
                print(
                    "PASS:",
                    f"repeat={repeat}",
                    f"queue_p95={value['node_queue_ms']['p95']:.3f}ms",
                    f"seconds={value['concurrent_seconds']:.3f}",
                    flush=True,
                )
        finally:
            platform_database.close()
            admin.dispose()
    evidence = {
        "schema_version": 1,
        "kind": "p4-r44-role-split",
        "version": "0.3.8",
        "status": "PENDING",
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "profile_configuration": profile,
        "roles": list(ROLE_CONFIGURATION["roles"]),
        "trigger_baseline": fixed_baseline(),
        "environment": environment(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
        },
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
    args.report.write_text(report(evidence))
    print(f"{evidence['status']}: evidence={args.output}", flush=True)
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
