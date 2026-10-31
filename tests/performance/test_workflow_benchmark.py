import hashlib
import importlib.util
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from platform_core.application.catalog import build_catalog
from platform_core.application.workflow_compiler import compile_package

SOURCE = Path(__file__).resolve().parents[2] / "benchmarks/p4/workflow_benchmark.py"
SPEC = importlib.util.spec_from_file_location("p4_workflow_benchmark", SOURCE)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)

MAX_BYTES = BENCHMARK.MAX_BYTES
PROFILES = BENCHMARK.PROFILES
TARGETS = BENCHMARK.TARGETS
parallel_package = BENCHMARK.parallel_package
direct_scatter_package = BENCHMARK.direct_scatter_package
partition_parameters = BENCHMARK.partition_parameters
percentile = BENCHMARK.percentile
scatter_package = BENCHMARK.scatter_package
summarize_intervals = BENCHMARK.summarize_intervals
synthetic_csv = BENCHMARK.synthetic_csv

LONG_OPERATOR_SOURCE = Path(__file__).with_name("long_operator.py")
LONG_OPERATOR_SPEC = importlib.util.spec_from_file_location(
    "p4_performance_long_operator", LONG_OPERATOR_SOURCE
)
assert LONG_OPERATOR_SPEC is not None and LONG_OPERATOR_SPEC.loader is not None
LONG_OPERATOR = importlib.util.module_from_spec(LONG_OPERATOR_SPEC)
LONG_OPERATOR_SPEC.loader.exec_module(LONG_OPERATOR)

DIAGNOSTIC_SOURCE = SOURCE.with_name("scheduler_diagnostics.py")
DIAGNOSTIC_SPEC = importlib.util.spec_from_file_location(
    "p4_scheduler_diagnostics", DIAGNOSTIC_SOURCE
)
assert DIAGNOSTIC_SPEC is not None and DIAGNOSTIC_SPEC.loader is not None
DIAGNOSTIC = importlib.util.module_from_spec(DIAGNOSTIC_SPEC)
DIAGNOSTIC_SPEC.loader.exec_module(DIAGNOSTIC)

BRIDGE_SOURCE = SOURCE.parents[2] / "runtimes/cwl/bridge.py"
BRIDGE_SPEC = importlib.util.spec_from_file_location("p4_cwl_bridge", BRIDGE_SOURCE)
assert BRIDGE_SPEC is not None and BRIDGE_SPEC.loader is not None
BRIDGE = importlib.util.module_from_spec(BRIDGE_SPEC)
BRIDGE_SPEC.loader.exec_module(BRIDGE)


def test_generator_is_deterministic_and_reaches_public_row_boundary():
    first, parameters, metadata = synthetic_csv(100_000, 1_000)
    second, _, repeated = synthetic_csv(100_000, 1_000)
    assert len(first) < MAX_BYTES
    assert first == second
    assert metadata["sha256"] == repeated["sha256"] == hashlib.sha256(first).hexdigest()
    assert len(parameters["groups"]) == 1_000
    assert first.count(b"\n") == 100_001


def test_performance_packages_compile_with_production_catalog():
    catalog = build_catalog()
    parallel = compile_package(parallel_package(), catalog=catalog)
    scatter = compile_package(scatter_package(), catalog=catalog)
    direct = compile_package(direct_scatter_package(), catalog=catalog)
    assert len(parallel["invocations"]) == 2
    assert len(scatter["invocations"]) == len(direct["invocations"]) == 1
    assert len(direct["files"]) < len(scatter["files"])
    assert all(
        item["target"] in TARGETS
        for compiled in (parallel, scatter, direct)
        for item in compiled["invocations"].values()
    )


def test_batches_cover_groups_once_and_percentile_uses_nearest_rank():
    _, parameters, _ = synthetic_csv(300, 30)
    batches = partition_parameters(parameters, 8)
    flattened = [group for batch in batches for group in batch["groups"]]
    assert sorted(flattened) == parameters["groups"]
    assert percentile([1, 5, 2, 4, 3], 0.5) == 3
    assert percentile([1, 5, 2, 4, 3], 0.95) == 5


def test_primary_profiles_only_use_real_long_compute_nodes():
    assert all(set(profile) == {"boundary", "scatter"} for profile in PROFILES.values())
    assert BENCHMARK.LONG_NODE_CPU_SECONDS == LONG_OPERATOR.CPU_SECONDS == 30.0
    assert BENCHMARK.MIN_LONG_NODE_SECONDS == 25.0


def test_long_node_cpu_work_is_executable_and_deterministic_in_shape():
    iterations, digest = LONG_OPERATOR.burn_cpu(0.01)
    assert iterations > 0
    assert len(digest) == 64
    int(digest, 16)


def test_interval_summary_measures_sustained_parallelism_and_peak():
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    summary = summarize_intervals(
        [
            (origin, origin + timedelta(seconds=30)),
            (origin + timedelta(seconds=10), origin + timedelta(seconds=40)),
            (origin + timedelta(seconds=40), origin + timedelta(seconds=70)),
        ]
    )
    assert summary["sum"] == 90
    assert summary["compute_window"] == 70
    assert summary["effective_parallelism"] == round(90 / 70, 6)
    assert summary["overlap_peak"] == 2
    assert [item["start_offset"] for item in summary["timeline"]] == [0, 10, 40]


def test_diagnostic_counts_only_matching_structured_node_posts(tmp_path):
    (tmp_path / "process-0.log").write_text(
        "INFO workflow_node_post execution_id=target node_id=one\n"
        "INFO workflow_node_post execution_id=other node_id=two\n"
        "INFO workflow_node_post execution_id=target node_id=one\n"
    )
    cluster = SimpleNamespace(directory=tmp_path, logs=[object()])
    assert DIAGNOSTIC.count_node_posts(cluster, "target") == 2


def test_bridge_submits_once_then_follows_revisions_with_get():
    calls = []
    observations = iter(
        [
            {"state": "QUEUED", "revision": 1},
            {"state": "QUEUED", "revision": 1},
            {"state": "RUNNING", "revision": 2},
            {"state": "SUCCEEDED", "revision": 3},
        ]
    )

    def call(method, path, body=None):
        calls.append((method, path, body))
        return json.dumps(next(observations)).encode()

    result = BRIDGE.await_terminal(call, "node_one", {"frozen": True}, "a" * 32)
    assert result == {"state": "SUCCEEDED", "revision": 3}
    assert [method for method, _, _ in calls] == ["POST", "GET", "GET", "GET"]
    assert calls[0] == ("POST", "/nodes/node_one", {"frozen": True})
    assert "after_revision=1" in calls[1][1]
    assert "after_revision=1" in calls[2][1]
    assert "after_revision=2" in calls[3][1]


def test_bridge_replays_only_unknown_post_and_keeps_get_after_query_404():
    calls = []
    post_attempts = 0
    get_attempts = 0
    sleeps = []

    def call(method, path, body=None):
        nonlocal post_attempts, get_attempts
        calls.append((method, path, body))
        if method == "POST":
            post_attempts += 1
            if post_attempts == 1:
                raise OSError("response lost")
            return json.dumps({"state": "QUEUED", "revision": 1}).encode()
        get_attempts += 1
        if get_attempts == 1:
            raise urllib.error.HTTPError(path, 404, "temporary", {}, None)
        return json.dumps({"state": "SUCCEEDED", "revision": 2}).encode()

    result = BRIDGE.await_terminal(
        call,
        "node_one",
        {"frozen": True},
        "b" * 32,
        sleep=sleeps.append,
    )
    assert result["state"] == "SUCCEEDED"
    assert [method for method, _, _ in calls] == ["POST", "POST", "GET", "GET"]
    assert calls[0][2] == calls[1][2] == {"frozen": True}
    assert sleeps == [2, 2]
