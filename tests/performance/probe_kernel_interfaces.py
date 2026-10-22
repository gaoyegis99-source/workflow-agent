"""Deterministic kernel/interface stress probe.

This probe deliberately stays below the database and process boundaries. It
uses the same core compiler, bridge, model provider and HTTP adapters as the
runtime, while replacing external I/O with recording fakes. Every fake checks
the parameter shape, identity and cursor/revision propagation before returning
its simulated response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_agent_runtime.model_providers import DeterministicModelProvider  # noqa: E402
from platform_core.application.catalog import operators, ports, target  # noqa: E402
from platform_core.application.workflow_compiler import compile_package  # noqa: E402
from platform_core.domain.control import RuleViolation  # noqa: E402
from platform_infrastructure.services import ExecutionClient  # noqa: E402

from runtimes.cwl.bridge import await_terminal  # noqa: E402


def percentile(values: list[float], rank: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(len(ordered) * rank + 0.999999) - 1))
    return ordered[index]


def workflow_package() -> dict:
    operator = operators()[0]
    operator_target = target(operator)
    input_ports, output_ports = ports(operator_target)
    return {
        "profile": "v7-cwl-p4-v1",
        "entrypoint": "main",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {name: value for name, value in input_ports.items()},
                "outputs": output_ports,
                "steps": {
                    "invoke": {
                        "run": "operator",
                        "in": {name: name for name in input_ports},
                        "out": list(output_ports),
                    }
                },
            }
        },
        "implementations": {"operator": operator_target},
    }


def probe_compiler(rounds: int, concurrency: int) -> dict:
    package = workflow_package()
    durations: list[float] = []
    failures: list[str] = []
    lock = Lock()

    def one(index: int) -> None:
        started = time.perf_counter()
        try:
            compiled = compile_package(deepcopy(package))
            invocation = compiled["invocations"].get("invoke")
            if not invocation or invocation["target"] != package["implementations"]["operator"]:
                raise AssertionError("compiler lost the implementation target")
            if set(invocation["inputs"]) != set(
                package["documents"]["main"]["steps"]["invoke"]["in"]
            ):
                raise AssertionError("compiler changed input parameter names")
            if not compiled["entrypoint"].startswith("v7_"):
                raise AssertionError("compiler did not return a generated entrypoint")
        except Exception as error:  # reported as a probe failure, never hidden
            with lock:
                failures.append(f"compiler[{index}]: {type(error).__name__}: {error}")
        finally:
            with lock:
                durations.append(time.perf_counter() - started)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(rounds)))
    return {
        "rounds": rounds,
        "failures": failures,
        "latency_ms": _latency(durations),
    }


def probe_bridge(rounds: int, concurrency: int) -> dict:
    durations: list[float] = []
    failures: list[str] = []
    lock = Lock()

    def one(index: int) -> None:
        identity = f"node-{index}"
        request = {
            "call_path": ["main", "invoke"],
            "scatter_indices": [index],
            "inputs": {"value": index},
            "engine_binding": {"kind": "toil", "id": f"job-{index}", "epoch": 1},
            "epoch": 1,
        }
        calls: list[tuple[str, str, dict | None]] = []
        state = {"revision": 0, "polls": 0, "post_attempts": 0}
        started = time.perf_counter()

        def call(method: str, path: str, body: dict | None = None) -> bytes:
            calls.append((method, path, body))
            if method == "POST":
                state["post_attempts"] += 1
                if state["post_attempts"] == 1:
                    raise OSError("simulated lost response")
                if body != request or path != "/nodes/" + identity:
                    raise AssertionError("bridge POST parameter propagation failed")
                state["revision"] = 1
                return json.dumps({"state": "QUEUED", "revision": 1}).encode()
            query = parse_qs(urlsplit(path).query)
            if query.get("after_revision") != [str(state["revision"])] or query.get(
                "bridge_instance_id"
            ) != [f"bridge-{index}"]:
                raise AssertionError("bridge cursor/instance propagation failed")
            state["polls"] += 1
            state["revision"] += 1
            state_name = "SUCCEEDED" if state["polls"] >= 2 else "RUNNING"
            return json.dumps({"state": state_name, "revision": state["revision"]}).encode()

        try:
            result = await_terminal(
                call,
                identity,
                request,
                f"bridge-{index}",
                sleep=lambda _seconds: None,
            )
            if result != {"state": "SUCCEEDED", "revision": 3}:
                raise AssertionError(f"unexpected bridge terminal result: {result}")
            if [method for method, _, _ in calls] != ["POST", "POST", "GET", "GET"]:
                raise AssertionError("bridge retry/query sequence changed")
        except Exception as error:
            with lock:
                failures.append(f"bridge[{index}]: {type(error).__name__}: {error}")
        finally:
            with lock:
                durations.append(time.perf_counter() - started)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(rounds)))
    return {"rounds": rounds, "failures": failures, "latency_ms": _latency(durations)}


def probe_model_provider(rounds: int, concurrency: int) -> dict:
    response = {"frame_id": "frame-1", "expected_task_revision": 1, "kind": "EXPLANATION"}
    usage = {
        "input_tokens": 12,
        "output_tokens": 8,
        "reasoning_tokens": 0,
        "cost_microunits": 1,
    }
    idempotent = DeterministicModelProvider(
        [{"status": "COMPLETED", "executed": True, "response": response, "usage": usage}]
    )
    duplicate_durations: list[float] = []
    duplicate_lock = Lock()
    call = {
        "model_call_id": "same-call",
        "provider_revision": 0,
        "request": {"frame": {"frame_id": "frame-1"}, "context": {"task": "probe"}},
    }

    def duplicate_submit(_index: int):
        started = time.perf_counter()
        try:
            return idempotent.submit(call)
        finally:
            with duplicate_lock:
                duplicate_durations.append(time.perf_counter() - started)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        observations = list(pool.map(duplicate_submit, range(concurrency * 2)))
    failures = []
    if idempotent.submit_count != 1 or len({item.digest for item in observations}) != 1:
        failures.append("same model_call_id was not idempotent under concurrent submit")

    throughput = DeterministicModelProvider(
        [
            {
                "status": "COMPLETED",
                "executed": True,
                "response": response | {"call": str(index)},
                "usage": usage,
            }
            for index in range(rounds)
        ]
    )
    throughput_durations: list[float] = []
    throughput_lock = Lock()

    def submit(index: int):
        started = time.perf_counter()
        try:
            return throughput.submit(
                {
                    "model_call_id": f"call-{index}",
                    "provider_revision": 0,
                    "request": {"frame": {"frame_id": f"frame-{index}"}, "context": {}},
                }
            )
        finally:
            with throughput_lock:
                throughput_durations.append(time.perf_counter() - started)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(submit, range(rounds)))
    if any(item.status != "COMPLETED" or item.executed is not True for item in results):
        failures.append("deterministic provider returned a non-terminal observation")
    return {
        "rounds": rounds,
        "concurrent_duplicate_submits": concurrency * 2,
        "submit_count_for_duplicate": idempotent.submit_count,
        "throughput_submit_count": throughput.submit_count,
        "failures": failures,
        "duplicate_latency_ms": _latency(duplicate_durations),
        "latency_ms": _latency(throughput_durations),
    }


def probe_execution_adapter(rounds: int, concurrency: int) -> dict:
    calls: list[dict] = []
    call_lock = Lock()
    durations: list[float] = []
    duration_lock = Lock()
    release_digest = "sha256:" + "a" * 64
    previous = os.environ.get("V7_RELEASE_DIGEST")
    os.environ["V7_RELEASE_DIGEST"] = release_digest

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        with call_lock:
            calls.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "payload": payload,
                    "authorization": request.headers.get("authorization"),
                    "release": request.headers.get("x-v7-release-digest"),
                }
            )
        if request.url.path.endswith("/cancel"):
            action_id = request.url.path.split("/")[-2]
            return httpx.Response(
                200,
                json={
                    "event_id": "cancel-event",
                    "action_id": action_id,
                    "revision": 2,
                    "state": "CANCELLED",
                    "execution_id": None,
                    "executed": False,
                },
            )
        if request.url.path.endswith("/fault-503"):
            return httpx.Response(503, json={"code": "DEPENDENCY_UNAVAILABLE"})
        action_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "event_id": "event-" + action_id,
                "action_id": action_id,
                "revision": 1,
                "state": "QUEUED",
                "execution_id": None,
                "executed": False,
            },
        )

    client = ExecutionClient("http://simulated-execution", "t" * 40)
    client.client.close()
    client.client = httpx.Client(
        base_url="http://simulated-execution",
        headers=client.headers,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    failures: list[str] = []

    def submit(index: int):
        action_id = f"action-{index}"
        request = {"task_id": f"task-{index}", "parameters": {"value": index}}
        started = time.perf_counter()
        try:
            observation = client.submit(action_id, request)
            if observation.action_id != action_id or observation.state != "QUEUED":
                raise AssertionError("execution response identity/state mismatch")
            return action_id, request
        finally:
            with duration_lock:
                durations.append(time.perf_counter() - started)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            submitted = list(pool.map(submit, range(rounds)))
        for action_id, request in submitted[: max(1, min(8, rounds))]:
            cancelled = client.cancel(action_id)
            if cancelled.state != "CANCELLED":
                raise AssertionError("cancel response state was not propagated")
            match = [item for item in calls if item["path"].endswith("/" + action_id)]
            if not match or match[0]["payload"] != request:
                raise AssertionError("execution request payload was changed by adapter")
            if match[0]["authorization"] != "Bearer " + "t" * 40:
                raise AssertionError("service authorization header was not propagated")
            if match[0]["release"] != release_digest:
                raise AssertionError("release digest header was not propagated")
        try:
            client.submit("fault-503", {"task_id": "task-503"})
        except RuleViolation as error:
            if (error.code, error.status, error.retry) != (
                "DEPENDENCY_UNAVAILABLE",
                503,
                "SAME_KEY",
            ):
                raise AssertionError("503 error contract changed") from error
        else:
            raise AssertionError("503 dependency failure was not surfaced")
    except Exception as error:
        details = (
            f" code={error.code} status={error.status} retry={error.retry}"
            if isinstance(error, RuleViolation)
            else ""
        )
        failures.append(f"execution: {type(error).__name__}:{details} {error}")
    finally:
        client.close()
        if previous is None:
            os.environ.pop("V7_RELEASE_DIGEST", None)
        else:
            os.environ["V7_RELEASE_DIGEST"] = previous
    return {
        "rounds": rounds,
        "requests_recorded": len(calls),
        "failures": failures,
        "latency_ms": _latency(durations),
    }


def probe_contract_rejections() -> dict:
    invalid = workflow_package()
    invalid["implementations"]["operator"]["operator_id"] = "not-registered"
    failures: list[str] = []
    try:
        compile_package(invalid)
    except RuleViolation as error:
        if (error.code, error.status) != ("UNSUPPORTED_TARGET", 422):
            failures.append(f"unexpected compiler rejection: {error.code}/{error.status}")
    else:
        failures.append("unregistered implementation was accepted")
    return {"rounds": 1, "failures": failures, "latency_ms": {"count": 1}}


def _latency(values: list[float]) -> dict:
    milliseconds = [value * 1000 for value in values]
    return {
        "count": len(milliseconds),
        "min": round(min(milliseconds), 4) if milliseconds else 0.0,
        "p50": round(statistics.median(milliseconds), 4) if milliseconds else 0.0,
        "p95": round(percentile(milliseconds, 0.95), 4),
        "max": round(max(milliseconds), 4) if milliseconds else 0.0,
    }


def _source_tree_digest() -> str:
    """Bind a simulation result to the source actually imported by the probe."""

    digest = hashlib.sha256()
    roots = (ROOT / "apps", ROOT / "packages", ROOT / "runtimes", ROOT / "tests/performance")
    files = sorted(
        path
        for root in roots
        if root.exists()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        relative = path.relative_to(ROOT).as_posix().encode()
        digest.update(relative + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def run(rounds: int, concurrency: int) -> dict:
    if rounds < 1 or concurrency < 1:
        raise ValueError("rounds and concurrency must be positive")
    started = time.perf_counter()
    probes = {
        "compiler": probe_compiler(rounds, concurrency),
        "bridge": probe_bridge(rounds, concurrency),
        "model_provider": probe_model_provider(rounds, concurrency),
        "execution_adapter": probe_execution_adapter(rounds, concurrency),
        "contract_rejections": probe_contract_rejections(),
    }
    failures = [failure for value in probes.values() for failure in value["failures"]]
    return {
        "status": "SIMULATION_PASS" if not failures else "SIMULATION_FAIL",
        "rounds": rounds,
        "concurrency": concurrency,
        "provenance": {
            "source_tree_digest": _source_tree_digest(),
            "python": sys.version.split()[0],
            "release_digest": os.environ.get("V7_RELEASE_DIGEST"),
            "argv": list(sys.argv),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "probes": probes,
        "failures": failures,
        "scope": {
            "database": False,
            "docker": False,
            "production_network": False,
            "external_llm": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.rounds, args.concurrency)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["status"] == "SIMULATION_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
