"""Run reproducible P4.1 workflow workloads on disposable local services.

All generated observations are synthetic.  The benchmark exercises the public
API and real PostgreSQL, Toil and operator containers; it never reads a deployed
V7 or V6 database.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from platform_core.application.catalog import OPERATORS, target  # noqa: E402
from platform_data.database import Database as DataDatabase  # noqa: E402
from platform_execution.database import Database as ExecutionDatabase  # noqa: E402
from platform_infrastructure.database import Database  # noqa: E402

from scripts.p2_environment import PostgreSQL  # noqa: E402
from tests.p2.processes import eventually  # noqa: E402
from tests.p3.harness import Cluster  # noqa: E402

MAX_BYTES = 20 * 1024 * 1024
MIN_LONG_NODE_SECONDS = 25.0
LONG_NODE_CPU_SECONDS = 30.0
TARGETS = [
    target(value)
    for value in OPERATORS
    if value["operator_id"] in {"timeseries-completeness", "statistics-linear-trend"}
]
PARAMETERS = {
    "type": "record",
    "name": "AnalysisParameters",
    "fields": [
        {"name": "groups", "type": {"type": "array", "items": "string"}},
        {"name": "start", "type": "string"},
        {"name": "end", "type": "string"},
        {"name": "frequency", "type": "string"},
        {"name": "min_samples", "type": "int"},
    ],
}
PROFILES = {
    "quick": {
        "boundary": {"rows": 20_000, "groups": 200},
        "scatter": {"rows": 20_000, "groups": 200, "nodes": 4, "concurrency": 2},
    },
    "standard": {
        "boundary": {"rows": 100_000, "groups": 1_000},
        "scatter": {"rows": 50_000, "groups": 500, "nodes": 8, "concurrency": 4},
    },
    "max": {
        "boundary": {"rows": 100_000, "groups": 1_000},
        "scatter": {"rows": 100_000, "groups": 1_000, "nodes": 16, "concurrency": 8},
    },
}


def synthetic_csv(rows, group_count):
    """Return deterministic monitoring CSV plus its explicit analysis contract."""
    if not 3 <= group_count <= 1_000 or not group_count * 3 <= rows <= 100_000:
        raise ValueError("Synthetic dimensions exceed the P3 contract")
    started = time.perf_counter()
    groups = [f"G{number:04d}" for number in range(group_count)]
    origin = datetime(2024, 1, 1, tzinfo=UTC)
    lines = ["station,time,value\n"]
    for index in range(rows):
        group_index = index % group_count
        sample = index // group_count
        moment = origin + timedelta(hours=sample)
        # Exact deterministic linear signal with a group-specific intercept.
        value = 100 + group_index / 100 + sample / 8
        lines.append(f"{groups[group_index]},{moment.isoformat()},{value:.6f}\n")
    content = "".join(lines).encode()
    if len(content) > MAX_BYTES:
        raise ValueError("Synthetic CSV exceeds the public upload limit")
    last_sample = (rows - 1) // group_count
    end = origin + timedelta(days=last_sample // 24 + 1)
    parameters = {
        "groups": groups,
        "start": origin.isoformat(),
        "end": end.isoformat(),
        "frequency": "day",
        "min_samples": 3,
    }
    return (
        content,
        parameters,
        {
            "rows": rows,
            "groups": group_count,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "generation_seconds": round(time.perf_counter() - started, 6),
            "synthetic": True,
            "generator": "round-robin groups; hourly UTC; y=100+group/100+sample/8",
        },
    )


def parallel_package():
    outputs, steps, implementations = {}, {}, {}
    for name, operator in zip(("complete", "trend"), TARGETS, strict=True):
        implementations[name] = operator
        steps[name] = {
            "run": name,
            "in": {"observations": "observations", "parameters": "parameters"},
            "out": ["result", "table", "method"],
        }
        for part in ("result", "table", "method"):
            outputs[name + "_" + part] = {
                "type": "File",
                "outputSource": name + "/" + part,
            }
    return {
        "name": "P4 performance parallel operators",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {"observations": "File", "parameters": PARAMETERS},
                "outputs": outputs,
                "steps": steps,
            }
        },
        "implementations": implementations,
    }


def scatter_package():
    nullable_file_array = {"type": "array", "items": ["null", "File"]}
    parameters_array = {"type": "array", "items": PARAMETERS}
    conditional = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {"observations": "File", "parameters": PARAMETERS, "enabled": "boolean"},
        "outputs": {
            part: {"type": "File", "outputSource": "run/" + part}
            for part in ("result", "table", "method")
        },
        "steps": {
            "run": {
                "run": "complete",
                "in": {"observations": "observations", "parameters": "parameters"},
                "out": ["result", "table", "method"],
            }
        },
    }
    main = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {
            "observations": "File",
            "parameter_batches": {"type": parameters_array},
            "enabled": {"type": {"type": "array", "items": "boolean"}},
        },
        "outputs": {
            part + "s": {"type": nullable_file_array, "outputSource": "calculate/" + part}
            for part in ("result", "table", "method")
        },
        "steps": {
            "calculate": {
                "run": "conditional",
                "in": {
                    "observations": "observations",
                    "parameters": "parameter_batches",
                    "enabled": "enabled",
                },
                "out": ["result", "table", "method"],
                "scatter": ["parameters", "enabled"],
                "scatterMethod": "dotproduct",
                "when": "$(inputs.enabled)",
            }
        },
    }
    return {
        "name": "P4 performance conditional scatter",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": {"main": main, "conditional": conditional},
        "implementations": {"complete": TARGETS[0]},
    }


def direct_scatter_package():
    """Equivalent all-enabled long-node scatter without an additional subworkflow."""
    file_array = {"type": "array", "items": "File"}
    parameters_array = {"type": "array", "items": PARAMETERS}
    main = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {
            "observations": "File",
            "parameter_batches": {"type": parameters_array},
        },
        "outputs": {
            part + "s": {"type": file_array, "outputSource": "calculate/" + part}
            for part in ("result", "table", "method")
        },
        "steps": {
            "calculate": {
                "run": "complete",
                "in": {
                    "observations": "observations",
                    "parameters": "parameter_batches",
                },
                "out": ["result", "table", "method"],
                "scatter": "parameters",
                "scatterMethod": "dotproduct",
            }
        },
    }
    return {
        "name": "P4 performance direct scatter",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": {"main": main},
        "implementations": {"complete": TARGETS[0]},
    }


def partition_parameters(parameters, count):
    groups = parameters["groups"]
    if not 1 <= count <= len(groups):
        raise ValueError("Each computing scatter node needs a non-empty group batch")
    batches = [groups[index::count] for index in range(count)]
    return [parameters | {"groups": batch} for batch in batches]


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize_intervals(intervals):
    """Summarize actual container overlap without relying on polling samples."""
    if not intervals:
        return None
    origin = min(started for started, _ in intervals)
    last = max(finished for _, finished in intervals)
    durations = [(finished - started).total_seconds() for started, finished in intervals]
    events = sorted(
        [
            (moment, delta)
            for started, finished in intervals
            for moment, delta in ((started, 1), (finished, -1))
        ],
        key=lambda item: (item[0], item[1]),
    )
    active = peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    window = (last - origin).total_seconds()
    return {
        "min": round(min(durations), 6),
        "mean": round(sum(durations) / len(durations), 6),
        "p50": round(percentile(durations, 0.5), 6),
        "p95": round(percentile(durations, 0.95), 6),
        "max": round(max(durations), 6),
        "sum": round(sum(durations), 6),
        "compute_window": round(window, 6),
        "effective_parallelism": round(sum(durations) / window, 6),
        "overlap_peak": peak,
        "timeline": [
            {
                "start_offset": round((started - origin).total_seconds(), 6),
                "duration": round((finished - started).total_seconds(), 6),
            }
            for started, finished in sorted(intervals)
        ],
    }


def container_durations(steps):
    intervals = []
    for step in steps:
        execution = step.get("execution_id")
        if not execution:
            continue
        result = subprocess.run(
            ["docker", "inspect", "v7-p3-" + execution],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            continue
        info = json.loads(result.stdout)[0]
        started = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(info["State"]["FinishedAt"].replace("Z", "+00:00"))
        intervals.append((started, finished))
        host = info["HostConfig"]
        if not (
            info["Config"]["User"] not in {"", "0", "root"}
            and host["ReadonlyRootfs"]
            and host["NetworkMode"] == "none"
            and host["Memory"] == 1024**3
            and host["NanoCpus"] == 10**9
        ):
            raise AssertionError("Node container resource contract changed")
    return summarize_intervals(intervals)


def engine_metrics(execution):
    """Read non-secret lifecycle counters from every engine epoch."""
    epoch_count = execution["runtime"].get("engine_epoch", 1)
    durations = []
    process_group_warnings = 0
    failed_workers = 0
    running_containers = 0
    nonzero_container_pids = 0
    processes_after_exit = 0
    for epoch in range(1, epoch_count + 1):
        name = f"v7-p4-{execution['execution_id']}-e{epoch}"
        result = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            state = info["State"]
            running_containers += int(bool(state["Running"]))
            nonzero_container_pids += int(bool(state.get("Pid", 0)))
            if state["FinishedAt"] and not state["FinishedAt"].startswith("0001-"):
                started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
                finished = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
                durations.append((finished - started).total_seconds())
            top = subprocess.run(
                ["docker", "top", name, "-eo", "pid,args"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if top.returncode == 0:
                processes_after_exit += max(0, len(top.stdout.splitlines()) - 1)
        logs = subprocess.run(
            ["docker", "logs", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        combined = logs.stdout + logs.stderr
        process_group_warnings += combined.count("Processes were not reaped in groups")
        failed_workers += combined.count("Exiting the worker because of a failed job")
    return {
        "epochs": epoch_count,
        "container_seconds_total": round(sum(durations), 6),
        "process_group_warnings": process_group_warnings,
        "failed_workers": failed_workers,
        "running_containers": running_containers,
        "nonzero_container_pids": nonzero_container_pids,
        "processes_after_exit": processes_after_exit,
    }


def register(cluster, definition):
    started = time.perf_counter()
    queued = cluster.request("POST", cluster.p + "/workflow-packages", definition)

    def ready():
        value = cluster.request("GET", queued["status_url"])
        if value["status"] == "REJECTED":
            raise AssertionError(value)
        return value if value["status"] == "READY" else None

    package = eventually(ready, timeout=90)
    return package, time.perf_counter() - started


def run_workflow(
    cluster, package, inputs, node_limit, expected_nodes, timeout, runtime_sampler=None
):
    workflow_target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {"observations": cluster.selection} | inputs,
            "max_node_executions": node_limit,
        },
    )
    started = time.perf_counter()
    deadline = started + timeout
    execution_id = None
    first_node_seconds = None
    peak_in_flight = 0
    samples = 0
    while time.perf_counter() < deadline:
        receipt = cluster.request("GET", command["status_url"])
        if receipt["result"]:
            execution_id = receipt["result"]["execution_id"]
            execution = cluster.request("GET", cluster.p + "/executions/" + execution_id)
            steps = cluster.request("GET", cluster.p + "/executions/" + execution_id + "/steps")[
                "items"
            ]
            if steps and first_node_seconds is None:
                first_node_seconds = time.perf_counter() - started
            budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
            if runtime_sampler is not None:
                runtime_sampler(execution_id)
            peak_in_flight = max(peak_in_flight, budget["in_flight"])
            samples += 1
            if execution["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}:
                break
        time.sleep(0.1)
    else:
        raise TimeoutError("Workflow benchmark exceeded its scenario timeout")
    if execution["state"] != "SUCCEEDED":
        raise AssertionError(execution)
    if len(steps) != expected_nodes or any(item["state"] != "SUCCEEDED" for item in steps):
        raise AssertionError(steps)
    if len({item["node_id"] for item in steps}) != expected_nodes:
        raise AssertionError("Workflow node identities are not unique")

    budget_path = cluster.p + "/tasks/" + cluster.task + "/budget"
    final_budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", budget_path))["reserved"] == 0
            and value["in_flight"] == 0
            else None
        ),
        timeout=90,
    )
    if final_budget["used"] != expected_nodes:
        raise AssertionError(final_budget)
    receipt = execution["runtime"].get("publication_receipt", {})
    if receipt.get("state") != "COMMITTED" or len(receipt.get("assets", [])) != expected_nodes:
        raise AssertionError(receipt)
    container_seconds = container_durations(steps)
    if container_seconds is None or len(container_seconds["timeline"]) != expected_nodes:
        raise AssertionError("Completed node container metrics are unavailable")
    if container_seconds["min"] < MIN_LONG_NODE_SECONDS:
        raise AssertionError(
            f"Short compute node detected: {container_seconds['min']:.3f}s; "
            f"required >= {MIN_LONG_NODE_SECONDS:.1f}s"
        )
    seconds = time.perf_counter() - started
    return execution, {
        "workflow_seconds": round(seconds, 6),
        "first_node_seconds": round(first_node_seconds or seconds, 6),
        "nodes_started": expected_nodes,
        "peak_in_flight_observed": peak_in_flight,
        "poll_samples": samples,
        "container_seconds": container_seconds,
        "effective_parallelism_over_workflow": round(container_seconds["sum"] / seconds, 6),
        "engine": engine_metrics(execution),
        "budget": final_budget,
    }


def accept_parallel(cluster, execution):
    task_path = cluster.p + "/tasks/" + cluster.task
    started = time.perf_counter()
    deliverable = cluster.request(
        "POST",
        task_path + "/deliverables",
        {
            "expected_spec_revision": 1,
            "workflow_outputs": [
                {"execution_id": execution["execution_id"], "output": "complete_result"},
                {"execution_id": execution["execution_id"], "output": "trend_result"},
            ],
        },
    )
    receipt = cluster.request(
        "POST",
        task_path + "/acceptance",
        {"expected_spec_revision": 1, "deliverable_id": deliverable["deliverable_id"]},
    )
    report = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", receipt["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=300,
    )
    if report["status"] != "PASS":
        raise AssertionError(report)
    if cluster.request("GET", task_path)["lifecycle"] != "COMPLETED":
        raise AssertionError("Accepted task was not completed")
    budget = cluster.request("GET", task_path + "/budget")
    if budget["used"] != 3 or budget["reserved"] != 0 or budget["in_flight"] != 0:
        raise AssertionError(budget)
    return {"seconds": round(time.perf_counter() - started, 6), "budget": budget}


def prepare(cluster, token, rows, groups, budget, concurrency, name):
    raw, parameters, dataset = synthetic_csv(rows, groups)
    cluster.bootstrap(
        token,
        budget=budget,
        max_in_flight=concurrency,
        raw=raw,
        parameters=parameters,
        project_name="P4 performance " + name,
    )
    dataset["setup_seconds"] = round(cluster.bootstrap_seconds, 6)
    return parameters, dataset


def boundary_scenario(cluster, token, config):
    parameters, dataset = prepare(
        cluster,
        token,
        config["rows"],
        config["groups"],
        budget=3,
        concurrency=2,
        name="boundary",
    )
    package, validation = register(cluster, parallel_package())
    execution, metrics = run_workflow(
        cluster,
        package,
        {"parameters": parameters},
        node_limit=2,
        expected_nodes=2,
        timeout=600,
    )
    acceptance = accept_parallel(cluster, execution)
    metrics["acceptance_seconds"] = acceptance["seconds"]
    metrics["budget_after_acceptance"] = acceptance["budget"]
    metrics["logical_input_row_scans_per_second"] = round(
        dataset["rows"] * 2 / metrics["workflow_seconds"], 3
    )
    return {
        "name": "boundary_parallel_and_acceptance",
        "execution_id": execution["execution_id"],
        "dataset": dataset,
        "workflow": {"expanded_instances": 2, "expected_compute_nodes": 2},
        "package_validation_seconds": round(validation, 6),
        "metrics": metrics,
        "status": "PASS",
    }


def scatter_scenario(cluster, token, config):
    parameters, dataset = prepare(
        cluster,
        token,
        config["rows"],
        config["groups"],
        budget=config["nodes"],
        concurrency=config["concurrency"],
        name="scatter",
    )
    batches = partition_parameters(parameters, config["nodes"])
    package, validation = register(cluster, scatter_package())
    execution, metrics = run_workflow(
        cluster,
        package,
        {"parameter_batches": batches, "enabled": [True] * config["nodes"]},
        node_limit=config["nodes"],
        expected_nodes=config["nodes"],
        timeout=900,
    )
    metrics["logical_input_row_scans_per_second"] = round(
        dataset["rows"] * config["nodes"] / metrics["workflow_seconds"], 3
    )
    return {
        "name": "compute_scatter",
        "execution_id": execution["execution_id"],
        "dataset": dataset,
        "workflow": {
            "expanded_instances": config["nodes"],
            "expected_compute_nodes": config["nodes"],
            "configured_concurrency": config["concurrency"],
        },
        "package_validation_seconds": round(validation, 6),
        "metrics": metrics,
        "status": "PASS",
    }


def service_databases(postgres_url, admin):
    values = []
    for label, cls in (("data", DataDatabase), ("execution", ExecutionDatabase)):
        name = "p4_perf_" + label + "_" + secrets.token_hex(5)
        password = secrets.token_hex(24)
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
            connection.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
            connection.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
        url = (
            sa.engine.make_url(postgres_url)
            .set(database=name, username=name, password=password)
            .render_as_string(hide_password=False)
        )
        database = cls(url)
        database.migrate()
        database.check_schema()
        values.append((name, database))
    return values


def environment(profile_name):
    memory = None
    try:
        values = {
            line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in Path("/proc/meminfo").read_text().splitlines()
        }
        memory = values.get("MemTotal")
    except OSError:
        pass
    return {
        "profile": profile_name,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_average": [round(value, 3) for value in os.getloadavg()],
        "memory_total": memory,
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "images": {
            "cwl": (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip(),
            "performance_operator": (ROOT / ".p4-runtime/performance-operator-image.txt")
            .read_text()
            .strip(),
            **json.loads((ROOT / ".p3-runtime/images.json").read_text()),
        },
        "long_node_cpu_seconds": LONG_NODE_CPU_SECONDS,
        "minimum_observed_node_seconds": MIN_LONG_NODE_SECONDS,
    }


def run_benchmark(profile_name, selected):
    profile_config = PROFILES[profile_name]
    scenarios = []
    roots = []
    overall = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-performance-") as temp:
        database = Database(postgres.url)
        database.migrate()
        database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        services = service_databases(postgres.url, admin)
        cluster = Cluster(Path(temp), database, tuple(value for _, value in services))
        cluster.env["V7_CWL_IMAGE"] = (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip()
        cluster.env["V7_OPERATOR_IMAGE"] = (
            (ROOT / ".p4-runtime/performance-operator-image.txt").read_text().strip()
        )
        cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
        try:
            principal = database.create_principal("p4-performance-owner")
            token = database.issue_token(principal, ttl_seconds=7200)["token"]
            cluster.start_services()
            cluster.launch("platform-worker")
            cluster.launch("execution-worker")
            functions = {
                "boundary": boundary_scenario,
                "scatter": scatter_scenario,
            }
            for name in ("boundary", "scatter"):
                if name in selected:
                    result = functions[name](cluster, token, profile_config[name])
                    scenarios.append(result)
                    # Preserve exact root identities for owned-container cleanup only.
                    roots.append(result["execution_id"])
        finally:
            cluster.close()
            for execution in roots:
                for epoch in range(1, 5):
                    subprocess.run(
                        ["docker", "rm", "-f", f"v7-p4-{execution}-e{epoch}"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
            for name, service in reversed(services):
                service.close()
                with admin.connect() as connection:
                    connection.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
                    connection.exec_driver_sql(f"DROP ROLE {name}")
            database.close()
            admin.dispose()
    return {
        "schema_version": 1,
        "kind": "p4-workflow-performance",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "synthetic_data_notice": "Generated data only; not real groundwater observations",
        "workload_notice": (
            "Every compute node performs a fixed 30 CPU-second test workload before "
            "running the production scientific operator."
        ),
        "command_profile": profile_name,
        "environment": environment(profile_name),
        "scenarios": scenarios,
        "total_seconds": round(time.perf_counter() - overall, 6),
        "interpretation": (
            "Single-host observed timings are regression baselines for this machine, "
            "not production capacity guarantees."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, default="standard")
    parser.add_argument(
        "--only",
        default="boundary,scatter",
        help="comma-separated subset: boundary,scatter",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/verification/p4-workflow-performance-evidence.json",
    )
    args = parser.parse_args()
    selected = {item.strip() for item in args.only.split(",") if item.strip()}
    if not selected or not selected <= {"boundary", "scatter"}:
        raise SystemExit("Invalid --only selection")
    evidence = run_benchmark(args.profile, selected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    for item in evidence["scenarios"]:
        print(
            "PASS:",
            item["name"],
            f"rows={item['dataset']['rows']}",
            f"nodes={item['metrics']['nodes_started']}",
            f"workflow={item['metrics']['workflow_seconds']:.3f}s",
        )
    print(f"PASS: evidence written to {args.output}")


if __name__ == "__main__":
    main()
