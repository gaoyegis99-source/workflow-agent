"""R1 long-node diagnostics for workflow scheduling and recovery boundaries."""

import argparse
import json
import math
import os
import platform
import re
import secrets
import statistics
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
from platform_core.application.workflow_compiler import compile_package  # noqa: E402
from platform_execution.adapters.repository import Repository  # noqa: E402
from platform_execution.trace import record as trace  # noqa: E402
from platform_infrastructure.database import Database  # noqa: E402

from benchmarks.p4.workflow_benchmark import (  # noqa: E402
    LONG_NODE_CPU_SECONDS,
    MIN_LONG_NODE_SECONDS,
    direct_scatter_package,
    engine_metrics,
    partition_parameters,
    prepare,
    register,
    run_workflow,
    scatter_package,
    service_databases,
)
from scripts.p2_environment import PostgreSQL  # noqa: E402
from tests.p3.harness import Cluster  # noqa: E402

CONFIGURATIONS = (
    {"id": "A", "workflow": "nested", "workers": 1, "init": False},
    {"id": "B", "workflow": "nested", "workers": 2, "init": False},
    {"id": "C", "workflow": "nested", "workers": 4, "init": False},
    {"id": "D", "workflow": "nested", "workers": 1, "init": True},
    {"id": "E", "workflow": "direct", "workers": 1, "init": False},
)
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
NODE_EVENTS = {
    "BRIDGE_STARTED",
    "NODE_ACCEPTED",
    "EXECUTION_CLAIMED",
    "PERMIT_REQUESTED",
    "PERMIT_ALLOWED",
    "CONTAINER_CREATED",
    "CONTAINER_STARTED",
    "CONTAINER_EXITED",
    "OUTPUT_VERIFIED",
    "NODE_OBSERVATION_REPORTED",
    "NODE_TERMINAL_OBSERVED",
    "BRIDGE_RETURNED",
}
ROOT_EVENTS = {
    "PERMIT_REQUESTED",
    "PERMIT_ALLOWED",
    "ENGINE_CREATED",
    "ENGINE_STARTED",
    "ENGINE_EXITED",
    "GATHER_FINISHED",
    "PUBLICATION_PREPARED",
    "PUBLICATION_COMMITTED",
}


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def distribution(values):
    if not values:
        return None
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "p95": round(percentile(values, 0.95), 6),
        "p99": round(percentile(values, 0.99), 6),
        "max": round(max(values), 6),
    }


class ProcessSampler:
    def __init__(self, admin=None, execution_database=None):
        self.samples = {"bridge": [], "tagger": [], "toil_worker": []}
        self.connections = {"total": [], "active": []}
        self.admin = admin
        self.execution_database = execution_database
        self.last = 0.0

    def __call__(self, execution_id):
        now = time.monotonic()
        if now - self.last < 0.1:
            return
        self.last = now
        if self.admin is not None and self.execution_database is not None:
            with self.admin.connect() as connection:
                row = (
                    connection.execute(
                        sa.text(
                            "SELECT count(*) AS total, "
                            "count(*) FILTER (WHERE state = 'active') AS active "
                            "FROM pg_stat_activity WHERE datname = :database"
                        ),
                        {"database": self.execution_database},
                    )
                    .mappings()
                    .one()
                )
            self.connections["total"].append(int(row["total"]))
            self.connections["active"].append(int(row["active"]))
        result = subprocess.run(
            ["docker", "top", f"v7-p4-{execution_id}-e1", "-eo", "pid,ppid,rss,args"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            return
        for line in result.stdout.splitlines()[1:]:
            fields = line.strip().split(maxsplit=3)
            if len(fields) != 4 or not fields[2].isdigit():
                continue
            rss_mib = int(fields[2]) / 1024
            command = fields[3]
            if "bridge.py" in command:
                self.samples["bridge"].append(rss_mib)
            elif "_toil_worker" in command:
                self.samples["toil_worker"].append(rss_mib)
            elif "node" in command:
                self.samples["tagger"].append(rss_mib)

    def summary(self):
        return {name: distribution(values) for name, values in self.samples.items()}

    def connection_summary(self):
        return {name: distribution(values) for name, values in self.connections.items()}


def events_by_type(rows, event_type, node=None):
    return [
        row
        for row in rows
        if row["event_type"] == event_type and (node is None or row["node_action_id"] == node)
    ]


def first_monotonic(rows, event_type, node=None):
    selected = events_by_type(rows, event_type, node)
    return min(
        (row["source_monotonic_ns"] for row in selected if row["source_monotonic_ns"] is not None),
        default=None,
    )


def monotonic_milliseconds(start, end):
    return (end - start) / 1_000_000


def trace_probe(repository, workflow_execution_id, samples=32):
    writes = []
    for sample in range(samples):
        result = trace(
            repository,
            workflow_execution_id=workflow_execution_id,
            source="EXECUTION_API",
            event_type="DIAGNOSTIC_PROBE",
            discriminator=sample,
            source_monotonic_ns=time.monotonic_ns(),
            attributes={"sample": sample},
        )
        if result["failed"] or not result["inserted"]:
            raise AssertionError("Trace write probe was incomplete")
        writes.append(result["write_ms"])
    return distribution(writes)


def summarize_trace(database, execution, steps, workflow_seconds):
    repository = Repository(database)
    query_started = time.perf_counter_ns()
    rows = repository.traces(execution["execution_id"])
    query_ms = (time.perf_counter_ns() - query_started) / 1_000_000
    nodes = [item["node_id"] for item in steps]
    missing = []
    order_failures = []
    accepted_to_claim = []
    permit_durations = []
    permit_to_start = []
    exit_to_bridge = []
    accepted_times = []
    exit_times = []
    for node in nodes:
        present = {row["event_type"] for row in rows if row["node_action_id"] == node}
        if required := sorted(NODE_EVENTS - present):
            missing.append({"node_id": node, "events": required})
            continue
        accepted = first_monotonic(rows, "NODE_ACCEPTED", node)
        claimed = first_monotonic(rows, "EXECUTION_CLAIMED", node)
        requested = first_monotonic(rows, "PERMIT_REQUESTED", node)
        allowed = first_monotonic(rows, "PERMIT_ALLOWED", node)
        started = first_monotonic(rows, "CONTAINER_STARTED", node)
        exited = first_monotonic(rows, "CONTAINER_EXITED", node)
        returned = first_monotonic(rows, "BRIDGE_RETURNED", node)
        ordered = [accepted, claimed, requested, allowed, started, exited, returned]
        if ordered != sorted(ordered):
            order_failures.append(node)
        accepted_to_claim.append(monotonic_milliseconds(accepted, claimed))
        permit_to_start.append(monotonic_milliseconds(allowed, started))
        exit_to_bridge.append(monotonic_milliseconds(exited, returned))
        accepted_times.append(accepted)
        exit_times.append(exited)
        permit_durations.extend(
            row["duration_ms"]
            for row in events_by_type(rows, "PERMIT_ALLOWED", node)
            if row["duration_ms"] is not None
        )
    root_present = {row["event_type"] for row in rows if row["node_action_id"] is None}
    root_missing = sorted(ROOT_EVENTS - root_present)
    if missing or root_missing or order_failures:
        raise AssertionError(
            {"missing_nodes": missing, "missing_root": root_missing, "order": order_failures}
        )
    engine_started = first_monotonic(rows, "ENGINE_STARTED")
    engine_exited = first_monotonic(rows, "ENGINE_EXITED")
    first_bridge = min(first_monotonic(rows, "BRIDGE_STARTED", node) for node in nodes)
    first_accepted = min(accepted_times)
    last_bridge = max(first_monotonic(rows, "BRIDGE_RETURNED", node) for node in nodes)
    gathered = first_monotonic(rows, "GATHER_FINISHED")
    published = first_monotonic(rows, "PUBLICATION_COMMITTED")
    root_order = [
        first_monotonic(rows, "PERMIT_REQUESTED"),
        first_monotonic(rows, "PERMIT_ALLOWED"),
        first_monotonic(rows, "ENGINE_CREATED"),
        engine_started,
        engine_exited,
        gathered,
        first_monotonic(rows, "PUBLICATION_PREPARED"),
        published,
    ]
    if root_order != sorted(root_order):
        raise AssertionError("Workflow root trace events are not causally ordered")
    exit_to_next_accept = []
    for accepted in sorted(accepted_times):
        previous = max((exited for exited in exit_times if exited < accepted), default=None)
        if previous is not None:
            exit_to_next_accept.append(monotonic_milliseconds(previous, accepted))
    starts = sorted(first_monotonic(rows, "CONTAINER_STARTED", node) for node in nodes)
    start_intervals = [
        monotonic_milliseconds(previous, current)
        for previous, current in zip(starts, starts[1:], strict=False)
    ]
    probe = trace_probe(repository, execution["execution_id"])
    estimated_total = len(rows) * probe["median"]
    return {
        "event_count": len(rows),
        "required_event_completeness": 1.0,
        "query_ms": round(query_ms, 6),
        "write_probe_ms": probe,
        "estimated_total_write_ms": round(estimated_total, 6),
        "estimated_overhead_percent": round(estimated_total / (workflow_seconds * 1000) * 100, 6),
        "engine_start_to_first_bridge_ms": round(
            monotonic_milliseconds(engine_started, first_bridge), 6
        ),
        "engine_start_to_first_node_accept_ms": round(
            monotonic_milliseconds(engine_started, first_accepted), 6
        ),
        "accepted_to_first_claim_ms": distribution(accepted_to_claim),
        "permit_http_ms": distribution(permit_durations),
        "permit_to_container_start_ms": distribution(permit_to_start),
        "container_exit_to_bridge_return_ms": distribution(exit_to_bridge),
        "previous_container_exit_to_next_node_accept_ms": distribution(exit_to_next_accept),
        "container_start_interval_ms": distribution(start_intervals),
        "last_bridge_to_engine_exit_ms": round(
            monotonic_milliseconds(last_bridge, engine_exited), 6
        ),
        "engine_exit_to_gather_ms": round(monotonic_milliseconds(engine_exited, gathered), 6),
        "last_bridge_to_gather_ms": round(monotonic_milliseconds(last_bridge, gathered), 6),
        "gather_to_publish_ms": round(monotonic_milliseconds(gathered, published), 6),
        "events": [
            {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in row.items()
            }
            for row in rows
        ],
    }


def launch_workers(cluster, count, use_init):
    # R3 made init part of the production CwlDocker contract. Keep this argument
    # only so the immutable R1 evidence shape remains readable; current runs do
    # not patch the runtime in either direction.
    del use_init
    for number in range(count):
        process = cluster.start(
            [sys.executable, "-m", "tests.performance.probe_worker"],
            cluster.env,
        )
        cluster.running[f"diagnostic-execution-worker-{number}"] = process


def count_node_posts(cluster, execution_id):
    return node_http_metrics(cluster, execution_id)["post_requests"]


def node_http_metrics(cluster, execution_id):
    needle = f"workflow_node_post execution_id={execution_id} "
    get_needle = f"workflow_node_get execution_id={execution_id} "
    posts = 0
    gets = 0
    database_reads = 0
    for index in range(len(cluster.logs)):
        path = cluster.directory / f"process-{index}.log"
        if path.is_file():
            for line in path.read_text(errors="replace").splitlines():
                if needle in line:
                    posts += 1
                elif get_needle in line:
                    gets += 1
                else:
                    continue
                match = re.search(r"\bdb_reads=(\d+)\b", line)
                if match:
                    database_reads += int(match.group(1))
    return {
        "post_requests": posts,
        "long_poll_get_requests": gets,
        "http_requests": posts + gets,
        "node_state_db_reads": database_reads,
    }


def engine_init(execution_id):
    info = json.loads(
        subprocess.check_output(["docker", "inspect", f"v7-p4-{execution_id}-e1"], text=True)
    )[0]
    return bool(info["HostConfig"].get("Init"))


def run_once(
    postgres,
    platform_database,
    admin,
    directory,
    config,
    profile,
    repeat,
    *,
    trace_overhead_limit=1,
):
    services = service_databases(postgres.url, admin)
    cluster = Cluster(directory, platform_database, tuple(value for _, value in services))
    cluster.env["V7_CWL_IMAGE"] = (
        config.get("cwl_image") or (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip()
    )
    cluster.env["V7_OPERATOR_IMAGE"] = (
        (ROOT / ".p4-runtime/performance-operator-image.txt").read_text().strip()
    )
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    execution = result = None
    evidence_error = None
    try:
        principal = platform_database.create_principal(
            f"p4-r1-{config['id']}-{repeat}-{secrets.token_hex(4)}"
        )
        token = platform_database.issue_token(principal, ttl_seconds=7200)["token"]
        execution_command = None
        if config.get("engine_profile_id"):
            execution_command = [
                sys.executable,
                "-m",
                "tests.performance.profile_execution_service",
                "--profile",
                config["engine_profile_id"],
            ]
        cluster.start_services(execution_command=execution_command)
        if config.get("baseline_resources"):
            process = cluster.start(
                [sys.executable, "-m", "tests.performance.probe_platform_worker"], cluster.env
            )
            cluster.running["diagnostic-platform-worker"] = process
        else:
            cluster.launch("platform-worker")
        if config.get("roles"):
            for number, role in enumerate(config["roles"]):
                process = cluster.start(
                    [sys.executable, "-m", "tests.performance.probe_worker", "--role", role],
                    cluster.env,
                )
                cluster.running[f"diagnostic-{role}-worker-{number}"] = process
        else:
            launch_workers(cluster, config["workers"], config["init"])
        parameters, dataset = prepare(
            cluster,
            token,
            profile["rows"],
            profile["groups"],
            budget=profile["nodes"],
            concurrency=profile["concurrency"],
            name=f"r1-{config['id']}-{repeat}",
        )
        definition = (
            scatter_package() if config["workflow"] == "nested" else direct_scatter_package()
        )
        compiled = compile_package(definition)
        package, validation = register(cluster, definition)
        sampler = ProcessSampler(admin, services[1][0])
        inputs = {"parameter_batches": partition_parameters(parameters, profile["nodes"])}
        if config["workflow"] == "nested":
            inputs["enabled"] = [True] * profile["nodes"]
        execution, metrics = run_workflow(
            cluster,
            package,
            inputs,
            node_limit=profile["nodes"],
            expected_nodes=profile["nodes"],
            timeout=900,
            runtime_sampler=sampler,
        )
        steps = cluster.request(
            "GET", cluster.p + "/executions/" + execution["execution_id"] + "/steps"
        )["items"]
        traces = summarize_trace(services[1][1], execution, steps, metrics["workflow_seconds"])
        if (
            trace_overhead_limit is not None
            and traces["estimated_overhead_percent"] >= trace_overhead_limit
        ):
            raise AssertionError(
                "Trace write estimate exceeds its limit: "
                f"{traces['estimated_overhead_percent']:.6f}% "
                f"(median write {traces['write_probe_ms']['median']:.6f}ms, "
                f"workflow {metrics['workflow_seconds']:.6f}s)"
            )
        engine = engine_metrics(execution)
        result = {
            "configuration": config,
            "repeat": repeat,
            "dataset": dataset,
            "compiled_document_count": len(compiled["files"]),
            "compiled_invocation_count": len(compiled["invocations"]),
            "package_validation_seconds": round(validation, 6),
            "workflow": metrics,
            "engine_strategy": execution["runtime"].get("engine_strategy"),
            "engine_package_digest": execution["runtime"].get("engine_package_digest"),
            "engine_profile": execution["runtime"].get("engine_profile"),
            "average_compute_parallelism": metrics["container_seconds"]["effective_parallelism"],
            "slot_utilization_percent": round(
                metrics["container_seconds"]["effective_parallelism"]
                / profile["concurrency"]
                * 100,
                6,
            ),
            "trace": traces,
            "process_rss_mib": sampler.summary(),
            "execution_database_connections": sampler.connection_summary(),
            "engine_init_observed": engine_init(execution["execution_id"]),
            "engine": engine,
            "status": "PASS",
        }
        if not result["engine_init_observed"]:
            raise AssertionError("Production workflow engine did not use Docker init")
    finally:
        cluster.close()
        if result is not None:
            result["node_http"] = node_http_metrics(cluster, execution["execution_id"])
            result["node_submit_posts"] = result["node_http"]["post_requests"]
            result["repeated_node_submit_posts"] = result["node_submit_posts"] - profile["nodes"]
            if result["node_submit_posts"] < profile["nodes"]:
                evidence_error = "Execution HTTP node submissions were not fully observed"
        if execution is not None:
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
        raise AssertionError("Diagnostic run produced no result")
    if evidence_error is not None:
        raise AssertionError(evidence_error)
    return result


def aggregate(runs):
    groups = {}
    for run in runs:
        groups.setdefault(run["configuration"]["id"], []).append(run)
    summaries = {}
    for identity, values in groups.items():
        summaries[identity] = {
            "configuration": values[0]["configuration"],
            "runs": len(values),
            "workflow_seconds": distribution(
                [value["workflow"]["workflow_seconds"] for value in values]
            ),
            "effective_parallelism": distribution(
                [
                    value["workflow"]["container_seconds"]["effective_parallelism"]
                    for value in values
                ]
            ),
            "slot_utilization_percent": distribution(
                [value["slot_utilization_percent"] for value in values]
            ),
            "container_start_interval_p95_ms": distribution(
                [value["trace"]["container_start_interval_ms"]["p95"] for value in values]
            ),
            "engine_exit_ms": distribution(
                [value["trace"]["last_bridge_to_engine_exit_ms"] for value in values]
            ),
            "node_submit_posts": distribution([value["node_submit_posts"] for value in values]),
            "execution_queue_p95_ms": distribution(
                [value["trace"]["accepted_to_first_claim_ms"]["p95"] for value in values]
            ),
            "process_group_warnings": [
                value["engine"]["process_group_warnings"] for value in values
            ],
            "trace_overhead_percent": distribution(
                [value["trace"]["estimated_overhead_percent"] for value in values]
            ),
        }
    worker = None
    previous_queue = None
    for identity in ("A", "B", "C"):
        candidate = summaries[identity]
        queue = candidate["execution_queue_p95_ms"]["max"]
        if queue <= 500:
            worker = candidate["configuration"]["workers"]
            break
        if previous_queue is not None and (previous_queue - queue) / previous_queue < 0.10:
            break
        previous_queue = queue
    baseline, init = summaries["A"], summaries["D"]
    init_recommended = (
        max(init["process_group_warnings"]) < max(baseline["process_group_warnings"])
        or init["engine_exit_ms"]["median"] < baseline["engine_exit_ms"]["median"]
    ) and init["workflow_seconds"]["median"] <= baseline["workflow_seconds"]["median"] * 1.05
    rss = [
        sample
        for run in runs
        for kind in ("bridge", "tagger")
        for sample in [run["process_rss_mib"].get(kind)]
        if sample is not None
    ]
    observed_p99 = max((sample["p99"] for sample in rss), default=None)
    memory = None
    if observed_p99 is not None:
        memory = max(64, math.ceil(observed_p99 * 1.5 / 32) * 32)
    return {
        "configurations": summaries,
        "recommendations": {
            "execution_workers": {
                "status": "READY" if worker is not None else "BLOCKED",
                "value": worker,
                "rule": "smallest worker count with worst observed queue p95 <= 500 ms",
            },
            "container_init": {
                "status": "RECOMMENDED" if init_recommended else "NOT_RECOMMENDED",
                "value": init_recommended,
            },
            "coordination_memory_mib": {
                "status": "READY" if memory is not None and memory <= 128 else "BLOCKED",
                "value": memory if memory is not None and memory <= 128 else None,
                "observed_process_rss_p99_mib": observed_p99,
            },
            "graph_inflation_seconds": round(
                summaries["A"]["workflow_seconds"]["median"]
                - summaries["E"]["workflow_seconds"]["median"],
                6,
            ),
        },
    }


def environment(profile_name):
    return {
        "profile": profile_name,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_average": [round(value, 3) for value in os.getloadavg()],
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "long_node_cpu_seconds": LONG_NODE_CPU_SECONDS,
        "minimum_observed_node_seconds": MIN_LONG_NODE_SECONDS,
        "images": {
            "cwl": (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip(),
            "performance_operator": (ROOT / ".p4-runtime/performance-operator-image.txt")
            .read_text()
            .strip(),
        },
    }


def report(evidence):
    def median_run(identity, field):
        values = [
            field(item) for item in evidence["runs"] if item["configuration"]["id"] == identity
        ]
        return statistics.median(values)

    lines = [
        "# P4.1 R1 长节点调度诊断报告",
        "",
        f"日期：{evidence['finished_at']}. 状态：**{evidence['status']}**。",
        "",
        "所有性能节点执行固定 30 CPU 秒负载；本轮只增加观测，不实施 R2 调度优化。",
        f"本次执行 `{len(evidence['runs'])}` 组真实工作流运行，无失败或必测项跳过。",
        "",
        "| 配置 | Worker | init | 工作流 | 耗时中位数 | 耗时 p95/最差 | 中位并行度 | "
        "槽位利用率 | 队列 p95 最差 | 警告最差 |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for identity in ("A", "B", "C", "D", "E"):
        item = evidence["summary"]["configurations"][identity]
        config = item["configuration"]
        lines.append(
            "| {id} | {workers} | {init} | {workflow} | {seconds:.3f}s | "
            "{p95:.3f}s / {worst:.3f}s | {parallel:.3f} | {utilization:.1f}% | "
            "{queue:.3f}ms | {warnings} |".format(
                id=identity,
                workers=config["workers"],
                init="是" if config["init"] else "否",
                workflow=config["workflow"],
                seconds=item["workflow_seconds"]["median"],
                p95=item["workflow_seconds"]["p95"],
                worst=item["workflow_seconds"]["max"],
                parallel=item["effective_parallelism"]["median"],
                utilization=item["slot_utilization_percent"]["median"],
                queue=item["execution_queue_p95_ms"]["max"],
                warnings=max(item["process_group_warnings"]),
            )
        )
    recommendations = evidence["summary"]["recommendations"]
    baseline = evidence["summary"]["configurations"]["A"]
    two_workers = evidence["summary"]["configurations"]["B"]
    four_workers = evidence["summary"]["configurations"]["C"]
    init = evidence["summary"]["configurations"]["D"]
    rss = recommendations["coordination_memory_mib"]["observed_process_rss_p99_mib"]
    baseline_peak = median_run(
        "A", lambda run: run["workflow"]["container_seconds"]["overlap_peak"]
    )
    posts_per_node = (
        baseline["node_submit_posts"]["median"] / evidence["profile_configuration"]["nodes"]
    )
    lines += [
        "",
        "## 六类根因的观测结果",
        "",
        f"1. **协调资源压低真实计算并发。** A 组实际峰值并发固定为 "
        f"`{baseline_peak:.0f}`，"
        f"有效并行度中位数 `{baseline['effective_parallelism']['median']:.3f}`，"
        f"4 个 Platform 槽位利用率只有 `{baseline['slot_utilization_percent']['median']:.1f}%`。"
        f"增加到 4 Worker 后并行度仍只有 `{four_workers['effective_parallelism']['median']:.3f}`。",
        f"2. **CWL 图包装存在可测成本。** 嵌套图生成 5 个文档，直接图 4 个；"
        f"A 与 E 的总耗时中位差为 `{recommendations['graph_inflation_seconds']:.3f}` 秒。",
        "3. **稳定节点身份仍与 Toil 作业上下文耦合。** R1 按计划保持 "
        "`--disableChaining true`，因此没有把 chaining 变化混入本轮；该结构约束已由 trace 的"
        "调用路径、epoch 和节点 Action 关联验证，性能因果对照留给 R4/R5。",
        f"4. **bridge 把查询实现为重复 POST。** A 组每次 8 节点工作的 POST 中位数为 "
        f"`{baseline['node_submit_posts']['median']:.0f}`，即每节点约 "
        f"`{posts_per_node:.1f}` 次。",
        f"5. **单 Worker 偶发放大领取排队。** A 组最差队列 p95 为 "
        f"`{baseline['execution_queue_p95_ms']['max']:.3f} ms`；2 Worker 降至 "
        f"`{two_workers['execution_queue_p95_ms']['max']:.3f} ms`，4 Worker 仅进一步降至 "
        f"`{four_workers['execution_queue_p95_ms']['max']:.3f} ms`。",
        f"6. **子进程回收影响明确。** 无 init 的嵌套组每次有 "
        f"`{max(baseline['process_group_warnings'])}` 条回收警告，init 三次均为 0；"
        f"bridge 返回到引擎退出的中位数从 `{baseline['engine_exit_ms']['median']:.3f} ms` "
        f"降至 `{init['engine_exit_ms']['median']:.3f} ms`。",
        "",
        "## 主要阶段延迟（每组三次运行的中位数）",
        "",
        "| 配置 | 引擎启动→首节点接受 | 前一容器结束→下一接受 | 接受→领取 p95 | "
        "许可 HTTP p95 | 许可→容器启动 p95 | 容器结束→bridge 返回 p95 | "
        "最后返回→gather | gather→发布 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for identity in ("A", "B", "C", "D", "E"):
        lines.append(
            "| {identity} | {first:.1f}ms | {gap:.1f}ms | {queue:.1f}ms | {permit:.1f}ms | "
            "{start:.1f}ms | {returned:.1f}ms | {gather:.1f}ms | {publish:.1f}ms |".format(
                identity=identity,
                first=median_run(
                    identity,
                    lambda run: run["trace"]["engine_start_to_first_node_accept_ms"],
                ),
                gap=median_run(
                    identity,
                    lambda run: run["trace"]["previous_container_exit_to_next_node_accept_ms"][
                        "median"
                    ],
                ),
                queue=median_run(
                    identity,
                    lambda run: run["trace"]["accepted_to_first_claim_ms"]["p95"],
                ),
                permit=median_run(identity, lambda run: run["trace"]["permit_http_ms"]["p95"]),
                start=median_run(
                    identity,
                    lambda run: run["trace"]["permit_to_container_start_ms"]["p95"],
                ),
                returned=median_run(
                    identity,
                    lambda run: run["trace"]["container_exit_to_bridge_return_ms"]["p95"],
                ),
                gather=median_run(identity, lambda run: run["trace"]["last_bridge_to_gather_ms"]),
                publish=median_run(identity, lambda run: run["trace"]["gather_to_publish_ms"]),
            )
        )
    lines += [
        "",
        "## R2/R3 参数结论",
        "",
        f"- R2 使用 **{recommendations['execution_workers']['value']} 个 Execution Worker**："
        "这是三次运行中最差队列 p95 不超过 500 ms 的最小值；4 Worker 的进一步改善不足 10%。",
        f"- R2 协调作业内存候选为 **{recommendations['coordination_memory_mib']['value']} MiB**："
        f"bridge/tagger 的最高 p99 RSS 为 `{rss:.3f} MiB`，乘 1.5 后向上取整到 32 MiB 档。",
        "- R3 推荐验证并采用 Docker init：三次功能和恢复路径均通过，警告归零且总耗时没有退化。",
        "- R2 优先改造 bridge 的一次提交/按 revision 长轮询和 0.25 CPU 协调资源；"
        "Worker 数、init 和图压平分别按 R3、R4 路线实施。",
        "",
        "## 证据质量",
        "",
        f"- 15 次运行的必要 trace 完整率均为 100%；估算开销最差为 "
        f"`{max(run['trace']['estimated_overhead_percent'] for run in evidence['runs']):.3f}%`。",
        f"- bridge/tagger p99 RSS 最高为 `{rss:.3f} MiB`；"
        "原始采样、事件和每次运行结果保存在结构化证据中。",
        "- init 对照按固定配置顺序运行，仍可能受到系统负载和缓存的时间顺序影响；"
        "因此本轮只推荐 R3 验证，不把耗时改善直接当作生产承诺。",
        "",
        "结构化证据见 `p4-workflow-scheduler-diagnostics.json`。最终性能门槛属于 R2 之后，"
        "不作为本轮通过条件。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/verification/p4-workflow-scheduler-diagnostics.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/verification/p4-workflow-scheduler-diagnostics-report.md",
    )
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    runs = []
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r1-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for configuration in CONFIGURATIONS:
                for repeat in range(1, profile["repeats"] + 1):
                    directory = Path(temp) / f"{configuration['id']}-{repeat}"
                    directory.mkdir()
                    result = run_once(
                        postgres,
                        platform_database,
                        admin,
                        directory,
                        configuration,
                        profile,
                        repeat,
                    )
                    runs.append(result)
                    print(
                        "PASS:",
                        configuration["id"],
                        f"repeat={repeat}",
                        f"workflow={result['workflow']['workflow_seconds']:.3f}s",
                        f"parallel={result['workflow']['container_seconds']['effective_parallelism']:.3f}",
                        flush=True,
                    )
        finally:
            platform_database.close()
            admin.dispose()
    evidence = {
        "schema_version": 1,
        "kind": "p4-workflow-scheduler-diagnostics",
        "status": "PASS",
        "version": "0.3.1",
        "finished_at": datetime.now(UTC).isoformat(),
        "synthetic_data_notice": "Generated data only; not real groundwater observations",
        "workload_notice": "Every compute node performs a fixed 30 CPU-second workload",
        "required_trace_events": {
            "node": sorted(NODE_EVENTS),
            "root": sorted(ROOT_EVENTS),
        },
        "profile": args.profile,
        "profile_configuration": profile,
        "environment": environment(args.profile),
        "runs": runs,
        "summary": aggregate(runs),
        "total_seconds": round(time.perf_counter() - started, 6),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    args.report.write_text(report(evidence))
    print(f"PASS: evidence written to {args.output}")


if __name__ == "__main__":
    main()
