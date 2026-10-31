"""R2 long-node before/after benchmark with fixed two-worker execution pools."""

import argparse
import json
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


def old_image():
    evidence = json.loads(
        (P4_RUNTIME_ROOT / "verification/p4-workflow-scheduler-diagnostics.json").read_text()
    )
    image = evidence["environment"]["images"]["cwl"]
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
    )
    if inspected.returncode:
        raise RuntimeError("R1 immutable CWL image is unavailable; cannot make an exact comparison")
    return image


def configurations():
    return (
        {
            "id": "R1_BASELINE",
            "workflow": "nested",
            "workers": 2,
            "init": False,
            "baseline_resources": True,
            "cwl_image": old_image(),
        },
        {
            "id": "R2_OPTIMIZED",
            "workflow": "nested",
            "workers": 2,
            "init": False,
            "baseline_resources": False,
            "cwl_image": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
        },
    )


def summarize(values):
    concurrency = 4

    def eligible_start_delays(value):
        timeline = value["workflow"]["container_seconds"]["timeline"]
        starts = [item["start_offset"] for item in timeline]
        finishes = [item["start_offset"] + item["duration"] for item in timeline]
        delays = starts[:concurrency]
        for index in range(concurrency, len(starts)):
            available = sorted(finishes[:index])[index - concurrency]
            delays.append(max(0, starts[index] - available))
        return [delay * 1000 for delay in delays]

    start_delays = [delay for value in values for delay in eligible_start_delays(value)]
    return {
        "runs": len(values),
        "workflow_seconds": distribution(
            [value["workflow"]["workflow_seconds"] for value in values]
        ),
        "compute_window_seconds": distribution(
            [value["workflow"]["container_seconds"]["compute_window"] for value in values]
        ),
        "effective_parallelism": distribution(
            [value["workflow"]["container_seconds"]["effective_parallelism"] for value in values]
        ),
        "slot_utilization_percent": distribution(
            [value["slot_utilization_percent"] for value in values]
        ),
        "overlap_peak": distribution(
            [value["workflow"]["container_seconds"]["overlap_peak"] for value in values]
        ),
        "container_start_interval_p95_ms": distribution(
            [value["trace"]["container_start_interval_ms"]["p95"] for value in values]
        ),
        "eligible_container_start_delay_ms": distribution(start_delays),
        "execution_queue_p95_ms": distribution(
            [value["trace"]["accepted_to_first_claim_ms"]["p95"] for value in values]
        ),
        "post_requests": distribution([value["node_http"]["post_requests"] for value in values]),
        "long_poll_get_requests": distribution(
            [value["node_http"]["long_poll_get_requests"] for value in values]
        ),
        "http_requests": distribution([value["node_http"]["http_requests"] for value in values]),
        "node_state_db_reads": distribution(
            [value["node_http"]["node_state_db_reads"] for value in values]
        ),
        "execution_database_connection_peak": distribution(
            [value["execution_database_connections"]["total"]["max"] for value in values]
        ),
        "execution_database_active_peak": distribution(
            [value["execution_database_connections"]["active"]["max"] for value in values]
        ),
        "process_group_warnings": distribution(
            [value["engine"]["process_group_warnings"] for value in values]
        ),
    }


def validate(evidence):
    profile = evidence["profile_configuration"]
    groups = {key: summarize(value) for key, value in evidence["runs"].items()}
    optimized = groups["R2_OPTIMIZED"]
    failures = []
    performance_failures = []

    def gate(condition, message):
        if not condition:
            failures.append(message)

    gate(
        optimized["post_requests"]["min"] == optimized["post_requests"]["max"] == profile["nodes"],
        "normal execution must submit each node exactly once",
    )
    gate(optimized["long_poll_get_requests"]["min"] > 0, "revision long-poll was not observed")
    gate(
        optimized["overlap_peak"]["min"] >= profile["concurrency"],
        "four-way compute overlap missing",
    )
    if evidence["profile"] == "standard":
        performance_checks = (
            (optimized["compute_window_seconds"]["max"] <= 76.5, "compute window exceeds 76.5s"),
            (optimized["effective_parallelism"]["min"] >= 3.2, "parallelism is below 3.2"),
            (optimized["slot_utilization_percent"]["min"] >= 80, "slot utilization is below 80%"),
            (
                optimized["eligible_container_start_delay_ms"]["p95"] <= 3000,
                "eligible container start delay p95 exceeds 3s",
            ),
            (
                optimized["execution_queue_p95_ms"]["max"] <= 500,
                "Execution queue p95 exceeds 500ms",
            ),
        )
        performance_failures = [message for passed, message in performance_checks if not passed]
    baseline = groups["R1_BASELINE"]
    gate(
        optimized["http_requests"]["median"] < baseline["http_requests"]["median"],
        "R2 did not reduce node HTTP requests",
    )
    gate(
        optimized["node_state_db_reads"]["median"] < baseline["node_state_db_reads"]["median"],
        "R2 did not reduce node-state database reads",
    )
    gate(
        optimized["execution_database_connection_peak"]["max"]
        <= baseline["execution_database_connection_peak"]["max"] + 1,
        "R2 increased the Execution database connection peak by more than one",
    )
    evidence["summary"] = groups
    evidence["r2_gates"] = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    evidence["downstream_performance_targets"] = {
        "status": "PASS" if not performance_failures else "NOT_MET",
        "failures": performance_failures,
    }
    evidence["status"] = evidence["r2_gates"]["status"]
    if failures:
        raise AssertionError(failures)


def report(evidence):
    baseline = evidence["summary"]["R1_BASELINE"]
    optimized = evidence["summary"]["R2_OPTIMIZED"]

    def row(label, key, unit=""):
        before = baseline[key]["median"]
        after = optimized[key]["median"]
        change = (after - before) / before * 100 if before else 0
        return f"| {label} | {before:.3f}{unit} | {after:.3f}{unit} | {change:+.1f}% |"

    lines = [
        "# P4.1 R2 调度与 bridge 协议验收",
        "",
        f"日期：{evidence['finished_at']}。状态：**{evidence['status']}**。",
        "",
        "两组均使用 2 个 Execution Worker、嵌套条件 scatter 和固定 30 CPU 秒节点。"
        "基线使用 R1 不可变 bridge 镜像并移除生成工具的资源声明；R2 使用一次 POST、"
        "revision 长轮询以及 0.2 core / 128 MiB 协调资源。Docker init 仍关闭。",
        "",
        "| 指标 | R1 同机基线中位数 | R2 中位数 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        row("工作流总耗时", "workflow_seconds", "s"),
        row("计算窗口", "compute_window_seconds", "s"),
        row("有效计算并行度", "effective_parallelism"),
        row("计算槽利用率", "slot_utilization_percent", "%"),
        row("节点 POST", "post_requests"),
        row("节点 HTTP 总请求", "http_requests"),
        row("节点状态数据库读取", "node_state_db_reads"),
        row("Execution DB 连接峰值", "execution_database_connection_peak"),
        "",
        "连接峰值统计 PostgreSQL `pg_stat_activity` 中 Execution 独立数据库的客户端连接；"
        "数据库读取统计节点提交/长轮询路径对根执行和节点状态的短查询，不包含 trace 写入、"
        "Worker 领取或普通公开查询。长轮询等待期间不保留数据库事务。",
        "",
        f"R2 协议与资源门槛：**{evidence['r2_gates']['status']}**。后续综合性能门槛："
        f"**{evidence['downstream_performance_targets']['status']}**。",
        "",
        *(
            ["未达到的后续性能项："]
            + [f"- {item}" for item in evidence["downstream_performance_targets"]["failures"]]
            + [""]
            if evidence["downstream_performance_targets"]["failures"]
            else []
        ),
        "R2 没有启用 Docker init、图压平或 job chaining；对应警告和尾部调度开销继续属于 R3-R5。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=P4_RUNTIME_ROOT / "verification/p4-r2-scheduler-evidence.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=P4_RUNTIME_ROOT / "verification/p4-r2-scheduler-report.md",
    )
    parser.add_argument("--only", choices=("both", "optimized"), default="both")
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    selected = configurations()
    if args.only == "optimized":
        previous = json.loads(args.output.read_text())
        if previous["profile"] != args.profile or not previous["runs"].get("R1_BASELINE"):
            raise RuntimeError("Matching standard R1 baseline evidence is required")
        runs = {"R1_BASELINE": previous["runs"]["R1_BASELINE"], "R2_OPTIMIZED": []}
        selected = tuple(item for item in selected if item["id"] == "R2_OPTIMIZED")
    else:
        runs = {config["id"]: [] for config in selected}
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r2-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for config in selected:
                for repeat in range(1, profile["repeats"] + 1):
                    directory = Path(temp) / f"{config['id']}-{repeat}"
                    directory.mkdir()
                    result = run_once(
                        postgres,
                        platform_database,
                        admin,
                        directory,
                        config,
                        profile,
                        repeat,
                    )
                    runs[config["id"]].append(result)
                    print(
                        "PASS:",
                        config["id"],
                        f"repeat={repeat}",
                        f"workflow={result['workflow']['workflow_seconds']:.3f}s",
                        f"parallel={result['average_compute_parallelism']:.3f}",
                        f"post={result['node_http']['post_requests']}",
                        f"get={result['node_http']['long_poll_get_requests']}",
                        flush=True,
                    )
        finally:
            platform_database.close()
            admin.dispose()
    evidence = {
        "schema_version": 1,
        "kind": "p4-r2-scheduler-comparison",
        "status": "PENDING",
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "profile_configuration": profile,
        "fixed_execution_workers": 2,
        "disable_chaining": True,
        "coordination_resources": {
            "bridge_cores": 0.2,
            "memory_mib": 128,
            "toil_max_cores": 2,
            "toil_max_jobs": 10,
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
    args.report.write_text(report(evidence) + "\n")
    print(f"{evidence['status']}: evidence written to {args.output}")
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
