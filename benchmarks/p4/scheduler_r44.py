"""R4.4 conditional gate: concurrent workflows and Execution worker starvation."""

import argparse
import hashlib
import json
import platform
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from platform_core.application.catalog import build_catalog  # noqa: E402
from platform_core.application.workflow_compiler import compile_package  # noqa: E402
from platform_infrastructure.database import Database  # noqa: E402

from benchmarks.p4.scheduler_diagnostics import (  # noqa: E402
    distribution,
    launch_workers,
    node_http_metrics,
    summarize_trace,
)
from benchmarks.p4.workflow_benchmark import (  # noqa: E402
    engine_metrics,
    partition_parameters,
    prepare,
    register,
    run_workflow,
    scatter_package,
    service_databases,
)
from scripts.configure.p2_environment import PostgreSQL  # noqa: E402
from scripts.lib.runtime_layout import P4_RUNTIME_ROOT  # noqa: E402
from tests.platform.services.harness import Cluster  # noqa: E402

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
CONFIGURATIONS = (
    {"id": "TWO_WORKERS", "workers": 2},
    {"id": "FOUR_WORKERS", "workers": 4},
)
SOURCES = (
    "apps/execution-service/src/platform_execution/application/runner.py",
    "apps/execution-service/src/platform_execution/adapters/repository.py",
    "benchmarks/p4/scheduler_r44.py",
    "scripts/checks/check_p4_scheduler_r44.sh",
)


class WorkflowContext:
    """Freeze one project/task view while two workflows share the same HTTP services."""

    def __init__(self, cluster):
        self.cluster = cluster
        self.p = cluster.p
        self.task = cluster.task
        self.selection = cluster.selection

    def request(self, *args, **kwargs):
        return self.cluster.request(*args, **kwargs)


class QueueSampler:
    """Observe worker leases without changing the production worker or public API."""

    def __init__(self, database):
        self.database = database
        self.lock = threading.Lock()
        self.samples = []

    def __call__(self, _execution_id):
        with self.lock, self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.text(
                        """
                    SELECT
                      count(*) FILTER (
                        WHERE parent_execution_id IS NULL AND owner IS NOT NULL
                          AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')
                      ) AS leased_roots,
                      count(*) FILTER (
                        WHERE parent_execution_id IS NOT NULL AND owner IS NOT NULL
                          AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')
                      ) AS leased_nodes,
                      count(*) FILTER (
                        WHERE parent_execution_id IS NOT NULL AND owner IS NULL
                          AND available_at <= clock_timestamp()
                          AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')
                      ) AS ready_nodes
                    FROM executions
                    """
                    )
                )
                .mappings()
                .one()
            )
            self.samples.append({key: int(value) for key, value in row.items()})

    def summary(self, worker_count):
        starved = [
            item
            for item in self.samples
            if item["leased_roots"] >= worker_count
            and item["ready_nodes"] > 0
            and item["leased_nodes"] == 0
        ]
        return {
            "samples": len(self.samples),
            "max_leased_roots": max((item["leased_roots"] for item in self.samples), default=0),
            "max_leased_nodes": max((item["leased_nodes"] for item in self.samples), default=0),
            "max_ready_nodes": max((item["ready_nodes"] for item in self.samples), default=0),
            "parent_saturation_with_ready_nodes_samples": len(starved),
        }


def accepted_to_claim_ms(trace):
    events = trace["events"]
    nodes = {item["node_action_id"] for item in events if item["node_action_id"]}
    values = []
    for node in nodes:
        accepted = [
            item["source_monotonic_ns"]
            for item in events
            if item["node_action_id"] == node
            and item["event_type"] == "NODE_ACCEPTED"
            and item["source_monotonic_ns"] is not None
        ]
        claimed = [
            item["source_monotonic_ns"]
            for item in events
            if item["node_action_id"] == node
            and item["event_type"] == "EXECUTION_CLAIMED"
            and item["source_monotonic_ns"] is not None
        ]
        if accepted and claimed:
            values.append((min(claimed) - min(accepted)) / 1_000_000)
    return values


def frozen_context(cluster):
    return WorkflowContext(cluster)


def launch_role_workers(cluster, roles):
    for number, role in enumerate(roles):
        process = cluster.start(
            [
                sys.executable,
                "-m",
                "tests.performance.probe_worker",
                "--role",
                role,
            ],
            cluster.env,
        )
        cluster.running[f"r44-{role}-worker-{number}"] = process


def run_pair(postgres, platform_database, admin, directory, config, profile, repeat):
    services = service_databases(postgres.url, admin)
    cluster = Cluster(directory, platform_database, tuple(value for _, value in services))
    cluster.env["V7_CWL_IMAGE"] = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
    cluster.env["V7_OPERATOR_IMAGE"] = (
        (P4_RUNTIME_ROOT / "performance-operator-image.txt").read_text().strip()
    )
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    executions = []
    result = None
    try:
        principal = platform_database.create_principal(
            f"p4-r44-{config['id']}-{repeat}-{secrets.token_hex(4)}"
        )
        token = platform_database.issue_token(principal, ttl_seconds=7200)["token"]
        cluster.start_services()
        cluster.launch_platform_worker_lanes()
        if config.get("roles"):
            launch_role_workers(cluster, config["roles"])
        else:
            launch_workers(cluster, config["workers"], True)
        definition = scatter_package()
        compiled = compile_package(definition, catalog=build_catalog())
        contexts = []
        packages = []
        datasets = []
        parameters = []
        for workflow_number in range(2):
            values, dataset = prepare(
                cluster,
                token,
                profile["rows"],
                profile["groups"],
                budget=profile["nodes"],
                concurrency=profile["concurrency"],
                name=f"r44-{config['id']}-{repeat}-{workflow_number}",
            )
            context = frozen_context(cluster)
            package, validation = register(context, definition)
            contexts.append(context)
            packages.append(package)
            datasets.append(dataset | {"package_validation_seconds": round(validation, 6)})
            parameters.append(values)

        sampler = QueueSampler(services[1][1])
        barrier = threading.Barrier(2)

        def execute(index):
            inputs = {
                "parameter_batches": partition_parameters(parameters[index], profile["nodes"]),
                "enabled": [True] * profile["nodes"],
            }
            barrier.wait()
            return run_workflow(
                contexts[index],
                packages[index],
                inputs,
                node_limit=profile["nodes"],
                expected_nodes=profile["nodes"],
                timeout=900,
                runtime_sampler=sampler,
            )

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed = list(pool.map(execute, range(2)))
        concurrent_seconds = time.perf_counter() - started
        workflows = []
        delays = []
        warnings = 0
        posts = 0
        for index, (execution, metrics) in enumerate(completed):
            executions.append(execution)
            steps = contexts[index].request(
                "GET", contexts[index].p + "/executions/" + execution["execution_id"] + "/steps"
            )["items"]
            traces = summarize_trace(services[1][1], execution, steps, metrics["workflow_seconds"])
            node_http = node_http_metrics(cluster, execution["execution_id"])
            engine = engine_metrics(execution)
            delays.extend(accepted_to_claim_ms(traces))
            warnings += engine["process_group_warnings"]
            posts += node_http["post_requests"]
            workflows.append(
                {
                    "execution_id": execution["execution_id"],
                    "workflow": metrics,
                    "trace": traces,
                    "node_http": node_http,
                    "engine": engine,
                    "dataset": datasets[index],
                    "engine_strategy": execution["runtime"].get("engine_strategy"),
                }
            )
        result = {
            "configuration": config,
            "repeat": repeat,
            "compiled_document_count": len(compiled["files"]),
            "compiled_invocation_count": len(compiled["invocations"]),
            "concurrent_seconds": round(concurrent_seconds, 6),
            "node_queue_ms": distribution(delays),
            "lease_sampling": sampler.summary(config["workers"]),
            "node_submit_posts": posts,
            "process_group_warnings": warnings,
            "workflows": workflows,
            "work_class_counts": {},
            "status": "PASS",
        }
        with services[1][1].engine.connect() as connection:
            result["work_class_counts"] = {
                work_class: int(count)
                for work_class, count in connection.execute(
                    sa.text("SELECT work_class, count(*) FROM executions GROUP BY work_class")
                ).all()
            }
    finally:
        cluster.close()
        for execution in executions:
            for epoch in range(1, 5):
                subprocess.run(
                    ["docker", "rm", "-f", f"v7-p4-{execution['execution_id']}-e{epoch}"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
        for name, service in reversed(services):
            service.close()
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
                connection.exec_driver_sql(f"DROP ROLE {name}")
    if result is None:
        raise AssertionError("Concurrent workflow run produced no result")
    return result


def assess_trigger(runs, profile):
    groups = {
        config["id"]: [item for item in runs if item["configuration"]["id"] == config["id"]]
        for config in CONFIGURATIONS
    }
    summary = {}
    for identity, values in groups.items():
        queue_values = [item["node_queue_ms"]["p95"] for item in values]
        summary[identity] = {
            "runs": len(values),
            "concurrent_seconds": distribution([item["concurrent_seconds"] for item in values]),
            "node_queue_p95_ms": distribution(queue_values),
            "node_submit_posts": distribution([item["node_submit_posts"] for item in values]),
            "process_group_warnings": distribution(
                [item["process_group_warnings"] for item in values]
            ),
            "parent_saturation_samples": sum(
                item["lease_sampling"]["parent_saturation_with_ready_nodes_samples"]
                for item in values
            ),
        }
    two = summary["TWO_WORKERS"]
    four = summary["FOUR_WORKERS"]
    before = two["node_queue_p95_ms"]["max"]
    after = four["node_queue_p95_ms"]["max"]
    improvement = 0 if before <= 0 else (before - after) / before * 100
    checks = {
        "two_worker_queue_p95_exceeds_500ms": before > 500,
        "parents_saturate_workers_while_nodes_wait": two["parent_saturation_samples"] > 0,
        "ordinary_workers_materially_reduce_queue": improvement >= 20 and before - after >= 100,
    }
    trigger = all(checks.values())
    expected_posts = profile["nodes"] * 2
    integrity_failures = []
    for identity, value in summary.items():
        if (
            value["node_submit_posts"]["min"] != expected_posts
            or value["node_submit_posts"]["max"] != expected_posts
        ):
            integrity_failures.append(f"{identity} did not submit each node exactly once")
        if value["process_group_warnings"]["max"] != 0:
            integrity_failures.append(f"{identity} emitted process-group warnings")
    return {
        "configurations": summary,
        "worker_queue_improvement_percent": round(improvement, 6),
        "trigger_checks": checks,
        "decision": "IMPLEMENT_WORK_CLASS" if trigger else "CANCEL_R44",
        "integrity_failures": integrity_failures,
    }


def validate(evidence):
    profile = evidence["profile_configuration"]
    expected_runs = profile["repeats"] * len(CONFIGURATIONS)
    failures = list(evidence["assessment"]["integrity_failures"])
    if len(evidence["runs"]) != expected_runs:
        failures.append("Concurrent diagnostic run count differs from the profile")
    for run in evidence["runs"]:
        if len(run["workflows"]) != 2:
            failures.append("A configuration did not complete two concurrent workflows")
        if run["node_queue_ms"]["count"] != profile["nodes"] * 2:
            failures.append("Node queue evidence is incomplete")
        for workflow in run["workflows"]:
            timeline = workflow["workflow"]["container_seconds"]["timeline"]
            if len(timeline) != profile["nodes"] or any(item["duration"] < 25 for item in timeline):
                failures.append("A fixed long compute node was missing or too short")
            if workflow["engine_strategy"] != "flat_v1":
                failures.append("Concurrent diagnostic did not use flat_v1")
            budget = workflow["workflow"]["budget"]
            if budget["used"] != profile["nodes"] or budget["reserved"] or budget["in_flight"]:
                failures.append("A workflow budget did not settle exactly once")
            if workflow["trace"]["required_event_completeness"] != 1:
                failures.append("Required trace completeness is below 100 percent")
    evidence["gates"] = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    evidence["status"] = evidence["gates"]["status"]
    if failures:
        raise AssertionError(failures)


def report(evidence):
    assessment = evidence["assessment"]
    two = assessment["configurations"]["TWO_WORKERS"]
    four = assessment["configurations"]["FOUR_WORKERS"]
    decision = assessment["decision"]
    checks = assessment["trigger_checks"]
    lines = [
        "# P4.1 R4.4 Worker 队列拆分条件验收",
        "",
        f"日期：{evidence['finished_at']}。诊断状态：**{evidence['status']}**。",
        f"实施决策：**{decision}**。",
        "",
        "每组同时运行两条冻结 flat_v1 工作流；每条工作流使用固定 30 CPU 秒节点，",
        "并分别保留 4 个 Platform 计算槽。2 Worker 与 4 Worker 使用相同工作流图。",
        "",
        "| 指标 | 2 Worker | 4 Worker |",
        "| --- | ---: | ---: |",
        f"| 叶子领取 p95 最差值 | {two['node_queue_p95_ms']['max']:.3f}ms | "
        f"{four['node_queue_p95_ms']['max']:.3f}ms |",
        f"| 两工作流总耗时中位数 | {two['concurrent_seconds']['median']:.3f}s | "
        f"{four['concurrent_seconds']['median']:.3f}s |",
        f"| 父记录占满 Worker 且叶子等待的采样 | {two['parent_saturation_samples']} | "
        f"{four['parent_saturation_samples']} |",
        f"| 节点 POST 中位数 | {two['node_submit_posts']['median']:.0f} | "
        f"{four['node_submit_posts']['median']:.0f} |",
        f"| 进程组警告最差值 | {two['process_group_warnings']['max']:.0f} | "
        f"{four['process_group_warnings']['max']:.0f} |",
        "",
        "触发规则要求三项同时成立：",
        f"- 2 Worker 下叶子领取 p95 超过 500ms：`{checks['two_worker_queue_p95_exceeds_500ms']}`。",
        "- 父工作流占满 Worker 时存在可领取叶子："
        f"`{checks['parents_saturate_workers_while_nodes_wait']}`。",
        "- 增加普通 Worker 使 p95 至少下降 20% 且绝对下降 100ms："
        f"`{checks['ordinary_workers_materially_reduce_queue']}`。",
        "",
        f"4 Worker 相对 2 Worker 的最差领取 p95 改善为 "
        f"`{assessment['worker_queue_improvement_percent']:.3f}%`。",
    ]
    if decision == "CANCEL_R44":
        lines += [
            "",
            "既定条件未全部出现，因此不新增 work_class、不迁移数据库、不改变 Worker 部署。",
            "当前版本保持 0.3.6，下一轮仍从冻结的 R4.3 基线评估 R5 chaining。",
        ]
    else:
        lines += [
            "",
            "条件已满足；下一步在同一 R4.4 轮次实施显式 work_class、领取索引和角色 Worker，",
            "然后用本诊断矩阵重新验收。",
        ]
    return "\n".join(lines) + "\n"


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    suffix = "-quick" if args.profile == "quick" else ""
    args.output = (
        args.output or P4_RUNTIME_ROOT / f"verification/p4-r44-worker-queue{suffix}-evidence.json"
    )
    args.report = (
        args.report or P4_RUNTIME_ROOT / f"verification/p4-r44-worker-queue{suffix}-report.md"
    )
    profile = PROFILES[args.profile]
    runs = []
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r44-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for config in CONFIGURATIONS:
                for repeat in range(1, profile["repeats"] + 1):
                    directory = Path(temp) / f"{config['id']}-{repeat}"
                    directory.mkdir()
                    value = run_pair(
                        postgres, platform_database, admin, directory, config, profile, repeat
                    )
                    runs.append(value)
                    print(
                        "PASS:",
                        config["id"],
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
        "kind": "p4-r44-worker-queue-gate",
        "version": "0.3.6",
        "status": "PENDING",
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "profile_configuration": profile,
        "workflows_per_run": 2,
        "disable_chaining": True,
        "environment": environment(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
        },
        "runs": runs,
        "total_seconds": round(time.perf_counter() - started, 6),
    }
    evidence["assessment"] = assess_trigger(runs, profile)
    error = None
    try:
        validate(evidence)
    except AssertionError as problem:
        error = problem
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    args.report.write_text(report(evidence))
    print(
        f"{evidence['status']}: decision={evidence['assessment']['decision']} "
        f"evidence={args.output}",
        flush=True,
    )
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
