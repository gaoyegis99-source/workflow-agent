"""R5 Toil job-chaining selection with fixed long-running compute nodes."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from platform_contracts.engine_runtime import (  # noqa: E402
    OFF_PROFILE_ID,
    ON_PROFILE_ID,
    diagnostic,
)
from platform_contracts.engine_runtime import profile as runtime_profile  # noqa: E402
from platform_infrastructure.database import Database  # noqa: E402

from benchmarks.p4.scheduler_diagnostics import run_once  # noqa: E402
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
ROLES = ("supervisor",) + ("node",) * 8 + ("publication",)
ORDER = {
    "quick": (OFF_PROFILE_ID, ON_PROFILE_ID),
    "standard": (
        OFF_PROFILE_ID,
        ON_PROFILE_ID,
        ON_PROFILE_ID,
        OFF_PROFILE_ID,
        OFF_PROFILE_ID,
        ON_PROFILE_ID,
    ),
}
SOURCES = (
    "packages/contracts/src/platform_contracts/engine_runtime.py",
    "apps/execution-service/src/platform_execution/api.py",
    "apps/execution-service/src/platform_execution/adapters/docker.py",
    "apps/execution-service/src/platform_execution/adapters/repository.py",
    "apps/execution-service/src/platform_execution/application/workflow_runner.py",
    "runtimes/cwl/engine.py",
    "benchmarks/p4/scheduler_diagnostics.py",
    "benchmarks/p4/scheduler_r5.py",
    "tests/execution/workflows/test_chaining_profiles_e2e.py",
    "tests/performance/test_scheduler_r5.py",
    "tests/performance/profile_execution_service.py",
    "scripts/checks/check_p4_scheduler_r5.sh",
)
SAFETY_REPORT = P4_RUNTIME_ROOT / "verification/r5-chaining-safety.xml"
SAFETY_TESTS = {
    "test_chaining_on_publishes_once_with_frozen_profile",
    "test_chaining_on_survives_api_and_worker_restart_without_profile_change",
    "test_chaining_on_leader_restart_uses_original_jobstore_and_nodes",
    "test_chaining_on_does_not_duplicate_sequential_node_or_budget",
    "test_chaining_on_cancel_stops_new_compute_and_aborts_publication",
}


def configuration(profile_id):
    return {
        "id": "CHAINING_ON" if profile_id == ON_PROFILE_ID else "CHAINING_OFF",
        "workflow": "nested",
        "workers": len(ROLES),
        "roles": ROLES,
        "init": True,
        "baseline_resources": False,
        "cwl_image": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
        "engine_profile_id": profile_id,
    }


def _absolute(summary):
    return {
        "workflow_median_at_most_100s": summary["workflow_seconds"]["median"] <= 100,
        "compute_median_at_most_70s": summary["compute_window_seconds"]["median"] <= 70,
        "compute_max_at_most_76_5s": summary["compute_window_seconds"]["max"] <= 76.5,
        "parallelism_each_at_least_3_2": summary["effective_parallelism"]["min"] >= 3.2,
        "utilization_each_at_least_80_percent": summary["slot_utilization_percent"]["min"] >= 80,
        "eligible_start_p95_at_most_3s": summary["eligible_container_start_delay_ms"]["p95"]
        <= 3000,
        "execution_queue_each_at_most_500ms": summary["execution_queue_p95_ms"]["max"] <= 500,
        "process_group_warnings_zero": summary["process_group_warnings"]["max"] == 0,
    }


def selection_decision(
    *,
    standard,
    safety_qualified,
    integrity_failures,
    off_absolute,
    on_absolute,
    regressions,
    total_gain,
    compute_gain,
):
    qualified = (
        standard
        and safety_qualified
        and not integrity_failures
        and all(off_absolute.values())
        and all(on_absolute.values())
        and all(regressions.values())
        and max(total_gain, compute_gain) >= 10
    )
    return "SELECT_CHAINING_ON" if qualified else "KEEP_CHAINING_OFF"


def assess(evidence):
    grouped = {
        key: [item for item in evidence["runs"] if item["configuration"]["id"] == key]
        for key in ("CHAINING_OFF", "CHAINING_ON")
    }
    summaries = {key: summarize(values) for key, values in grouped.items()}
    off, on = summaries["CHAINING_OFF"], summaries["CHAINING_ON"]
    profile_config = evidence["profile_configuration"]
    integrity = []
    expected = profile_config["nodes"]
    expected_runs = profile_config["repeats"]
    for key, values in grouped.items():
        if len(values) != expected_runs:
            integrity.append(f"{key} run count differs from the profile")
        wanted_profile = OFF_PROFILE_ID if key == "CHAINING_OFF" else ON_PROFILE_ID
        wanted_diagnostic = diagnostic(runtime_profile(wanted_profile))
        for value in values:
            timeline = value["workflow"]["container_seconds"]["timeline"]
            if len(timeline) != expected or any(item["duration"] < 25 for item in timeline):
                integrity.append(f"{key} did not run every fixed long node")
            if value.get("engine_profile") != wanted_diagnostic:
                integrity.append(f"{key} did not freeze the requested trusted profile")
            if value.get("engine_strategy") != "flat_v1":
                integrity.append(f"{key} did not use flat_v1")
            if value["node_http"]["post_requests"] != expected:
                integrity.append(f"{key} did not submit every node exactly once")
            if value["trace"]["required_event_completeness"] != 1:
                integrity.append(f"{key} trace evidence is incomplete")
            budget = value["workflow"]["budget"]
            if (budget["used"], budget["reserved"], budget["in_flight"]) != (expected, 0, 0):
                integrity.append(f"{key} budget did not settle exactly once")
            engine = value["engine"]
            if any(
                engine[name]
                for name in (
                    "process_group_warnings",
                    "failed_workers",
                    "running_containers",
                    "nonzero_container_pids",
                    "processes_after_exit",
                )
            ):
                integrity.append(f"{key} engine lifecycle did not settle cleanly")

    off_absolute = _absolute(off) if evidence["profile"] == "standard" else {}
    on_absolute = _absolute(on) if evidence["profile"] == "standard" else {}
    total_gain = (
        (off["workflow_seconds"]["median"] - on["workflow_seconds"]["median"])
        / off["workflow_seconds"]["median"]
        * 100
    )
    compute_gain = (
        (off["compute_window_seconds"]["median"] - on["compute_window_seconds"]["median"])
        / off["compute_window_seconds"]["median"]
        * 100
    )
    regressions = {
        "workflow_seconds": on["workflow_seconds"]["median"]
        <= off["workflow_seconds"]["median"] * 1.05,
        "compute_window_seconds": on["compute_window_seconds"]["median"]
        <= off["compute_window_seconds"]["median"] * 1.05,
        "effective_parallelism": on["effective_parallelism"]["median"]
        >= off["effective_parallelism"]["median"] * 0.95,
        "slot_utilization": on["slot_utilization_percent"]["median"]
        >= off["slot_utilization_percent"]["median"] * 0.95,
        "eligible_start_delay": on["eligible_container_start_delay_ms"]["p95"]
        <= off["eligible_container_start_delay_ms"]["p95"] * 1.05,
        "execution_queue": on["execution_queue_p95_ms"]["median"]
        <= off["execution_queue_p95_ms"]["median"] * 1.05,
    }
    decision = selection_decision(
        standard=evidence["profile"] == "standard",
        safety_qualified=evidence["safety_qualification"]["status"] == "PASS",
        integrity_failures=integrity,
        off_absolute=off_absolute,
        on_absolute=on_absolute,
        regressions=regressions,
        total_gain=total_gain,
        compute_gain=compute_gain,
    )
    return {
        "summaries": summaries,
        "integrity_failures": sorted(set(integrity)),
        "absolute_gates": {"CHAINING_OFF": off_absolute, "CHAINING_ON": on_absolute},
        "gain_percent": {
            "workflow_seconds": round(total_gain, 6),
            "compute_window_seconds": round(compute_gain, 6),
        },
        "no_more_than_five_percent_regression": regressions,
        "decision": decision,
    }


def validate(evidence):
    assessment = evidence["assessment"]
    failures = list(assessment["integrity_failures"])
    if evidence["profile"] == "standard" and evidence["safety_qualification"]["status"] != "PASS":
        failures.append("The chaining-on safety qualification was not run by the R5 gate")
    if evidence["profile"] == "standard" and not all(
        assessment["absolute_gates"]["CHAINING_OFF"].values()
    ):
        failures.append("The accepted chaining-off baseline no longer meets P4.1 gates")
    evidence["gates"] = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    evidence["status"] = evidence["gates"]["status"]
    if failures:
        raise AssertionError(failures)


def report(evidence):
    assessment = evidence["assessment"]
    off = assessment["summaries"]["CHAINING_OFF"]
    on = assessment["summaries"]["CHAINING_ON"]
    gains = assessment["gain_percent"]
    return (
        "\n".join(
            [
                "# P4.1 R5 Toil chaining 对照",
                "",
                f"日期：{evidence['finished_at']}。状态：**{evidence['status']}**。",
                f"正式选择：**{assessment['decision']}**。",
                "",
                "两组使用相同 flat_v1、角色 Worker 池和固定 30 CPU 秒节点；唯一变量是",
                "Toil `--disableChaining`。standard 使用平衡顺序，各运行三次。",
                "",
                "| 指标 | chaining off | chaining on | on 改善 |",
                "| --- | ---: | ---: | ---: |",
                f"| 总耗时中位数 | {off['workflow_seconds']['median']:.3f}s | "
                f"{on['workflow_seconds']['median']:.3f}s | {gains['workflow_seconds']:+.3f}% |",
                f"| 计算窗口中位数 | {off['compute_window_seconds']['median']:.3f}s | "
                f"{on['compute_window_seconds']['median']:.3f}s | "
                f"{gains['compute_window_seconds']:+.3f}% |",
                f"| 并行度中位数 | {off['effective_parallelism']['median']:.3f} | "
                f"{on['effective_parallelism']['median']:.3f} | — |",
                f"| 槽位利用率中位数 | {off['slot_utilization_percent']['median']:.3f}% | "
                f"{on['slot_utilization_percent']['median']:.3f}% | — |",
                f"| 领取 p95 最差值 | {off['execution_queue_p95_ms']['max']:.3f}ms | "
                f"{on['execution_queue_p95_ms']['max']:.3f}ms | — |",
                f"| 进程组警告 | {off['process_group_warnings']['max']:.0f} | "
                f"{on['process_group_warnings']['max']:.0f} | — |",
                "",
                "只有总耗时或计算窗口改善至少 10%、其他指标不退化超过 5%，且绝对门槛和",
                "恢复测试全部通过时才选择 chaining-on。没有足够收益时保持 chaining-off。",
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
        "cwl_image": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
        "performance_operator_image": (P4_RUNTIME_ROOT / "performance-operator-image.txt")
        .read_text()
        .strip(),
    }


def safety_qualification(profile_name):
    command = "uv run --locked pytest -q tests/execution/workflows/test_chaining_profiles_e2e.py"
    if profile_name == "quick":
        return {"status": "NOT_REQUIRED", "command": command}
    if not SAFETY_REPORT.is_file():
        return {"status": "NOT_RUN", "command": command}
    tree = ET.parse(SAFETY_REPORT)
    cases = tree.findall(".//testcase")
    names = {case.attrib.get("name") for case in cases}
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))
    }
    passed = not any(counts.values()) and SAFETY_TESTS <= names
    return {
        "status": "PASS" if passed else "FAIL",
        "command": command,
        "tests": len(cases),
        **counts,
        "report_sha256": hashlib.sha256(SAFETY_REPORT.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    suffix = "-quick" if args.profile == "quick" else ""
    args.output = (
        args.output or P4_RUNTIME_ROOT / f"verification/p4-r5-chaining{suffix}-evidence.json"
    )
    args.report = args.report or P4_RUNTIME_ROOT / f"verification/p4-r5-chaining{suffix}-report.md"
    profile_config = PROFILES[args.profile]
    counters = {OFF_PROFILE_ID: 0, ON_PROFILE_ID: 0}
    runs = []
    started = time.perf_counter()
    with PostgreSQL() as postgres, tempfile.TemporaryDirectory(prefix="v7-p4-r5-") as temp:
        platform_database = Database(postgres.url)
        platform_database.migrate()
        platform_database.check_schema()
        admin = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
        try:
            for sequence, profile_id in enumerate(ORDER[args.profile], 1):
                counters[profile_id] += 1
                directory = Path(temp) / f"run-{sequence}"
                directory.mkdir()
                value = run_once(
                    postgres,
                    platform_database,
                    admin,
                    directory,
                    configuration(profile_id),
                    profile_config,
                    counters[profile_id],
                    trace_overhead_limit=None,
                )
                value["sequence"] = sequence
                runs.append(value)
                print(
                    "PASS:",
                    f"sequence={sequence}",
                    f"profile={profile_id}",
                    f"workflow={value['workflow']['workflow_seconds']:.3f}s",
                    flush=True,
                )
        finally:
            platform_database.close()
            admin.dispose()
    evidence = {
        "schema_version": 1,
        "kind": "p4-r5-chaining-selection",
        "version": "0.3.8",
        "status": "PENDING",
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "profile_configuration": profile_config,
        "execution_worker_roles": list(ROLES),
        "run_order": list(ORDER[args.profile]),
        "safety_qualification": safety_qualification(args.profile),
        "environment": environment(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
        },
        "runs": runs,
        "total_seconds": round(time.perf_counter() - started, 6),
    }
    evidence["assessment"] = assess(evidence)
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
